import asyncio
import json
import os
import secrets
import time
from asyncio import Lock
from contextlib import suppress
from multiprocessing import Process, get_context
from multiprocessing.queues import Queue as MPQueue
from typing import Any, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type:ignore

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Json, Plain, Reply
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import BaseMessageComponent
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.provider.entites import LLMResponse
from astrbot.core.star.filter.event_message_type import EventMessageType

from .censor_flow import CensorFlow  # type:ignore
from .common import CensorResult, RiskLevel, admin_check, dispose_msg  # type:ignore
from .db import DBManager  # type:ignore
from .webui import run_server  # type:ignore
from .webui_control import webui_enabled


@register(
    "astrbot_plugin_aiocensor", "Raven95676", "Astrbot综合内容安全+群管插件", "v0.1.7"
)
class AIOCensor(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.web_ui_process: Process | None = None
        self.scheduler: AsyncIOScheduler | None = None
        self._mp_ctx = get_context("spawn")
        self._notification_queue: MPQueue | None = None
        self._notification_task: asyncio.Task | None = None
        self._update_lock: Lock | None = None
        self._pending_update = False

        # 初始化内容审查流
        self.censor_flow = CensorFlow(config)
        data_path = os.path.join(os.getcwd(), "data", "aiocensor")
        os.makedirs(data_path, exist_ok=True)
        self.db_mgr = DBManager(os.path.join(data_path, "censor.db"))

        # 存储 (group_id_str, user_id_str) -> expiry_ts
        self.new_member_watchlist: dict[tuple[str, str], int] = {}
        # 缓存 aiocqhttp bot 客户端，key: (platform_id, self_id)
        self._aiocqhttp_bot_cache: dict[tuple[str, str], dict[str, Any]] = {}

    async def initialize(self):
        logger.debug("初始化 AIOCensor 组件")
        start_webui = webui_enabled(self.config)
        # 生成 Web UI 密钥（如果未设置）
        if start_webui and not self.config["webui"].get("secret"):
            self.config["webui"]["secret"] = secrets.token_urlsafe(32)
            self.config.save_config()

        # 初始化数据库和审查器
        self.db_mgr.initialize()
        self._update_lock = asyncio.Lock()
        if start_webui:
            self._notification_queue = self._mp_ctx.Queue()
            self._notification_task = asyncio.create_task(self._consume_notifications())

        await self._update_censors()

        self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        # 设置定时任务，每 5 分钟清理过期的新成员监听条目
        self.scheduler.add_job(
            self._cleanup_watchlist,
            "interval",
            minutes=5,
            id="cleanup_watchlist",
            misfire_grace_time=60,
        )
        self.scheduler.start()

        # 启动 Web UI 服务
        if start_webui:
            self.web_ui_process = self._mp_ctx.Process(
                target=run_server,
                args=(
                    self.config["webui"]["secret"],
                    self.config["webui"]["password"],
                    self.config["webui"].get("host", "127.0.0.1"),
                    self.config["webui"].get("port", 9966),
                    self._notification_queue,
                ),
                daemon=True,
            )
            self.web_ui_process.start()
        else:
            logger.info("AIOCensor WebUI disabled; no child process started")

    async def _update_censors(self):
        """刷新审查器数据到最新状态"""
        if not self._update_lock:
            self._update_lock = asyncio.Lock()

        if self._update_lock.locked():
            self._pending_update = True
            return

        while True:
            async with self._update_lock:
                try:

                    def _collect(
                        fetch: Callable[[int, int], list[Any]],
                    ) -> list[Any]:
                        offset = 0
                        page_size = 500
                        items: list[Any] = []
                        while True:
                            chunk = fetch(page_size, offset)
                            if not chunk:
                                break
                            items.extend(chunk)
                            if len(chunk) < page_size:
                                break
                            offset += page_size
                        return items

                    black_list = _collect(
                        lambda limit, offset: self.db_mgr.get_blacklist_entries(
                            limit=limit, offset=offset
                        )
                    )
                    await self.censor_flow.userid_censor.build({
                        entry.identifier for entry in black_list
                    })
                    if hasattr(self.censor_flow.text_censor, "build"):
                        sensitive_words = _collect(
                            lambda limit, offset: self.db_mgr.get_sensitive_words(
                                limit=limit, offset=offset
                            )
                        )
                        await self.censor_flow.text_censor.build({
                            entry.word for entry in sensitive_words
                        })
                    logger.debug("审查器数据已更新")
                except Exception as e:
                    logger.error(f"更新审查器数据失败: {e!s}")
            if self._pending_update:
                self._pending_update = False
                continue
            break

    async def _consume_notifications(self) -> None:
        """处理 WebUI 进程发来的数据更新事件"""
        if not self._notification_queue:
            return
        while True:
            try:
                message: Any = await asyncio.to_thread(self._notification_queue.get)
            except asyncio.CancelledError:
                break
            except (EOFError, OSError):
                logger.debug("通知队列已关闭，停止监听")
                break
            if not message:
                continue
            event_type = (
                message.get("type") if isinstance(message, dict) else str(message)
            )
            if event_type == "shutdown":
                break
            if event_type in {"blacklist_updated", "sensitive_words_updated"}:
                await self._update_censors()
            elif event_type == "audit_log_dispose":
                payload: dict[str, Any] | None = (
                    message.get("payload") if isinstance(message, dict) else None
                )
                await self._handle_webui_dispose(payload)
            else:
                logger.debug(f"忽略未知的通知事件: {event_type}")

    async def _cleanup_watchlist(self):
        """定时清理过期的新成员监听条目"""
        now = int(time.time())
        items = list(self.new_member_watchlist.items())
        remove_keys = [k for k, ts in items if ts <= now]
        for k in remove_keys:
            self.new_member_watchlist.pop(k, None)

    def _cache_aiocqhttp_bot(self, event: AstrMessageEvent) -> None:
        """缓存 aiocqhttp bot 客户端以供后续手动处置使用"""
        platform_id = str(event.get_platform_id() or "")
        self_id = event.get_self_id()
        if not self_id:
            return

        key = (platform_id, str(self_id))
        self._aiocqhttp_bot_cache[key] = {
            "bot": getattr(event, "bot", None),
        }

    def _get_cached_aiocqhttp_bot(self, platform_id: str, self_id: str):
        """根据 platform_id/self_id 从缓存中获取 aiocqhttp bot"""
        search_keys = [
            (platform_id or "", self_id),
        ]
        if platform_id:
            # 兼容旧数据未记录 platform_id 的情况
            search_keys.append(("", self_id))

        for key in search_keys:
            cache_entry = self._aiocqhttp_bot_cache.get(key)
            if cache_entry:
                return cache_entry.get("bot")
        return None

    async def _handle_aiocqhttp_group_message(
        self, event: AstrMessageEvent, res: CensorResult
    ):
        """处理 aiocqhttp 平台的群消息"""
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
            AiocqhttpMessageEvent,
        )

        if not isinstance(event, AiocqhttpMessageEvent):
            return

        group_id = int(event.get_group_id())
        user_id = int(event.get_sender_id())
        self_id = int(event.get_self_id())
        message_id = int(event.message_obj.message_id)

        res.extra.update({
            "group_id": group_id,
            "user_id": user_id,
            "self_id": self_id,
            "message_id": message_id,
        })

        disposal_attempted = False
        disposal_succeeded: bool | None = None

        if res.risk_level == RiskLevel.Block:
            can_dispose = self.config.get("enable_group_msg_censor") or self.config.get(
                "enable_review_new_members"
            )
            if can_dispose and await admin_check(user_id, group_id, self_id, event.bot):
                disposal_attempted = True
                try:
                    await dispose_msg(
                        message_id=message_id,
                        group_id=group_id,
                        user_id=user_id,
                        self_id=self_id,
                        client=event.bot,
                    )
                    disposal_succeeded = True
                except Exception as e:
                    disposal_succeeded = False
                    logger.error(f"消息处置失败: {e!s}")

        await self._forward_violation_message(
            event,
            res,
            recall_status=disposal_succeeded if disposal_attempted else None,
        )

    async def _process_censor_result(
        self, event: AstrMessageEvent, res: CensorResult | None
    ) -> bool:
        """根据审查结果执行后续逻辑，返回是否需要终止处理"""
        if not res or res.risk_level == RiskLevel.Pass:
            return False

        event.stop_event()

        extra_context = {
            "user_id_str": event.get_sender_id(),
            "platform_name": event.get_platform_name(),
            "platform_id": event.get_platform_id(),
            "self_id": event.get_self_id(),
            "group_id": event.get_group_id(),
            "message_id": getattr(event.message_obj, "message_id", None),
            "unified_msg_origin": event.unified_msg_origin,
        }
        res.extra = {
            **(res.extra or {}),
            **{k: v for k, v in extra_context.items() if v is not None},
        }

        if self.config.get("enable_audit_log", True):
            self.db_mgr.add_audit_log(res)

        forward_settings = self.config.get("forward_violation_message") or {}
        should_forward = forward_settings.get("enabled") and res.risk_level in {
            RiskLevel.Review,
            RiskLevel.Block,
        }

        if event.get_platform_name() == "aiocqhttp" and event.get_group_id():
            if res.risk_level == RiskLevel.Review:
                self._cache_aiocqhttp_bot(event)
                if should_forward:
                    await self._forward_violation_message(
                        event, res, recall_status=None
                    )
            elif res.risk_level == RiskLevel.Block:
                await self._handle_aiocqhttp_group_message(event, res)
            else:
                logger.warning("非 aiocqhttp 平台的群消息，无法自动处置")
            return True

        if should_forward:
            await self._forward_violation_message(event, res, recall_status=None)

        return False

    async def _censor_texts(self, event: AstrMessageEvent, texts: list[str]) -> bool:
        """对文本内容执行审查，返回是否命中风险并终止处理"""
        seen: set[str] = set()
        for text in texts:
            normalized = text.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            res = await self.censor_flow.submit_text(
                normalized, event.unified_msg_origin
            )
            if await self._process_censor_result(event, res):
                return True
        return False

    def _extract_texts_from_components(
        self, components: list[BaseMessageComponent] | None
    ) -> list[str]:
        texts: list[str] = []
        if not components:
            return texts

        for item in components:
            if isinstance(item, Plain):
                if item.text:
                    texts.append(item.text)
            elif isinstance(item, Json):
                texts.extend(self._extract_texts_from_json(item))
            elif isinstance(item, Reply):
                texts.extend(self._extract_texts_from_reply(item))

        return texts

    def _summarize_message_components(
        self, components: list[BaseMessageComponent] | None
    ) -> str:
        if not components:
            return ""

        parts: list[str] = []
        for comp in components:
            if isinstance(comp, Plain):
                text = comp.text.strip()
                if text:
                    parts.append(text)
            elif isinstance(comp, Image):
                label = "[图片]"
                resource = comp.url or comp.file or ""
                if resource:
                    label = f"{label} {resource}"
                parts.append(label)
            elif isinstance(comp, Json):
                parts.append("[JSON]")
            elif isinstance(comp, Reply):
                parts.append("[引用消息]")
            else:
                parts.append(f"[{comp.type.value}]")

        return "\n".join(part for part in parts if part).strip()

    async def _forward_violation_message(
        self,
        event: AstrMessageEvent,
        res: CensorResult,
        recall_status: bool | None = None,
    ) -> None:
        """在启用配置时转发违规或需复审的消息，并附带必要上下文信息"""
        settings = self.config.get("forward_violation_message") or {}
        if not settings.get("enabled"):
            return

        target_umo = settings.get("target_umo")
        if not target_umo:
            logger.warning("开启违规消息转发但未设置 target_umo，已跳过转发")
            return

        lines: list[str] = ["[AIOCensor] 违规内容通知"]

        risk_level = res.risk_level.name if res.risk_level else "UNKNOWN"
        lines.append(f"风险等级: {risk_level}")

        platform_name = event.get_platform_name() or "-"
        lines.append(f"平台: {platform_name}")

        platform_id = event.get_platform_id()
        if platform_id:
            lines.append(f"平台 ID: {platform_id}")

        group_id = event.get_group_id()
        if group_id:
            lines.append(f"群号: {group_id}")

        sender_id = event.get_sender_id()
        if sender_id:
            lines.append(f"发送者: {sender_id}")

        if event.unified_msg_origin:
            lines.append(f"会话 UMO: {event.unified_msg_origin}")

        if platform_name == "aiocqhttp":
            status_map = {
                True: "自动撤回状态: 成功",
                False: "自动撤回状态: 失败",
                None: "自动撤回状态: 未执行",
            }
            lines.append(status_map[recall_status])

        if res.reason:
            reason_text = ", ".join(sorted(res.reason))
            lines.append(f"命中原因: {reason_text}")

        extra = res.extra or {}
        message_id = extra.get("message_id")
        if message_id:
            lines.append(f"原始消息 ID: {message_id}")

        summary = self._summarize_message_components(
            getattr(event.message_obj, "message", None)
        )
        if summary:
            lines.append("原始消息内容:")
            lines.append(summary)
        else:
            captured = res.message.content if res.message else None
            if captured:
                lines.append("捕获内容:")
                lines.append(captured)
            else:
                lines.append("原始消息内容: [无文本内容，可能包含非文本组件]")

        try:
            message_chain = MessageChain().message("\n".join(lines))
            await self.context.send_message(target_umo, message_chain)
        except Exception as exc:
            logger.error(f"转发违规消息失败: {exc!s}")

    def _extract_texts_from_json(self, component: Json) -> list[str]:
        texts: list[str] = []
        raw = component.data

        payload: Any | None = None
        if isinstance(raw, str):
            try:
                payload = json.loads(raw)
            except Exception as exc:
                logger.debug(f"解析JSON组件失败: {exc!s}")
        elif isinstance(raw, dict):
            payload = raw

        if payload is not None:
            texts.extend(self._collect_strings_from_json(payload))
        elif isinstance(raw, str) and raw.strip():
            texts.append(raw)

        return texts

    def _collect_strings_from_json(self, obj: Any) -> list[str]:
        collected: list[str] = []
        if isinstance(obj, dict):
            for value in obj.values():
                collected.extend(self._collect_strings_from_json(value))
        elif isinstance(obj, list):
            for item in obj:
                collected.extend(self._collect_strings_from_json(item))
        elif isinstance(obj, str):
            stripped = obj.strip()
            if stripped:
                collected.append(stripped)
        return collected

    def _extract_texts_from_reply(self, reply: Reply) -> list[str]:
        texts: list[str] = []
        if getattr(reply, "message_str", None):
            texts.append(str(reply.message_str))
        if getattr(reply, "text", None):
            texts.append(str(reply.text))

        texts.extend(self._extract_texts_from_components(getattr(reply, "chain", None)))
        texts.extend(
            self._extract_texts_from_components(getattr(reply, "message", None))
        )

        return texts

    async def _handle_webui_dispose(self, payload: dict[str, Any] | None) -> None:
        """处理 WebUI 发起的处置请求"""
        if not payload:
            logger.warning("收到空的处置请求，已忽略")
            return

        log_id = payload.get("log_id")
        actions = payload.get("actions") or []

        if not log_id:
            logger.warning("WebUI 处置请求缺少 log_id，已忽略")
            return

        if "dispose" not in actions:
            logger.debug(f"审核日志 {log_id} 未包含需要处理的 dispose 操作，已忽略")
            return

        try:
            log_entry = self.db_mgr.get_audit_log(log_id)
        except Exception as exc:
            logger.error(f"查询审核日志 {log_id} 失败: {exc!s}")
            return

        if not log_entry:
            logger.warning(f"未找到审核日志 {log_id}，无法执行处置")
            return

        extra = log_entry.result.extra or {}
        platform_name = extra.get("platform_name") or extra.get("platform")
        if platform_name != "aiocqhttp":
            logger.warning(
                f"审核日志 {log_id} 属于平台 {platform_name}，当前仅支持 aiocqhttp 手动处置"
            )
            return

        message_id = extra.get("message_id")
        group_id = extra.get("group_id")
        user_id = extra.get("user_id_str") or extra.get("user_id")
        self_id = extra.get("self_id")
        if not all([message_id, group_id, user_id, self_id]):
            logger.warning(
                f"审核日志 {log_id} 缺少处置信息(message_id/group_id/user_id/self_id)，无法执行处置"
            )
            return

        try:
            message_id_int = int(message_id)
            group_id_int = int(group_id)
            user_id_int = int(user_id)
            self_id_int = int(self_id)
        except (TypeError, ValueError):
            logger.warning(f"审核日志 {log_id} 存在无法解析的处置信息，已忽略")
            return

        platform_id = str(extra.get("platform_id") or "")
        client = self._get_cached_aiocqhttp_bot(platform_id, str(self_id_int))
        if client is None:
            logger.warning(
                f"未找到平台 {platform_name} (platform_id={platform_id}) 自身 ID {self_id_int} 的缓存客户端，无法执行审核日志 {log_id} 的处置"
            )
            return

        try:
            await dispose_msg(
                message_id=message_id_int,
                group_id=group_id_int,
                user_id=user_id_int,
                self_id=self_id_int,
                client=client,
            )
            logger.info(f"已根据 WebUI 请求处置审核日志 {log_id}")
        except Exception as exc:
            logger.error(f"执行审核日志 {log_id} 处置失败: {exc!s}")

    async def handle_message(
        self, event: AstrMessageEvent, chain: list[BaseMessageComponent]
    ):
        """核心消息内容审查逻辑"""
        try:
            # 遍历消息组件进行审计
            for comp in chain:
                res = None
                if isinstance(comp, Plain):
                    res = await self.censor_flow.submit_text(
                        comp.text, event.unified_msg_origin
                    )
                elif isinstance(comp, Image) and self.config.get("enable_image_censor"):
                    res = await self.censor_flow.submit_image(
                        comp.url, event.unified_msg_origin
                    )
                elif isinstance(comp, Reply):
                    texts = self._extract_texts_from_reply(comp)
                    if await self._censor_texts(event, texts):
                        break
                    continue
                elif isinstance(comp, Json):
                    texts = self._extract_texts_from_json(comp)
                    if await self._censor_texts(event, texts):
                        break
                    continue
                else:
                    continue

                if await self._process_censor_result(event, res):
                    break
        except Exception as e:
            logger.error(f"消息审查失败: {e!s}")

    @filter.event_message_type(EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        """检查黑名单和全输入审查"""
        if self.config.get("enable_blacklist"):
            res = await self.censor_flow.submit_userid(
                event.get_sender_id(), event.unified_msg_origin
            )
            if res.risk_level == RiskLevel.Block:
                if self.config.get("enable_audit_log", True):
                    self.db_mgr.add_audit_log(res)
                event.stop_event()
                return
        if (
            self.config.get("enable_all_input_censor")
            or self.config.get("enable_input_censor")
            and event.is_at_or_wake_command
        ):
            await self.handle_message(event, event.message_obj.message)

    @filter.event_message_type(EventMessageType.ALL)
    async def handle_group_increase_for_review(self, event: AstrMessageEvent):
        """检测 aiocqhttp 的 group_increase 通知并将新成员加入短期审查监听表"""
        if not self.config.get("enable_review_new_members"):
            return
        raw_message = event.message_obj.raw_message
        post_type = raw_message.get("post_type")
        if post_type == "notice" and raw_message.get("notice_type") == "group_increase":
            group_id = str(raw_message.get("group_id", ""))
            user_id = str(raw_message.get("user_id", ""))
            # 群组白名单判断
            group_list = self.config.get("group_list", [])
            group_list_str = [str(g) for g in group_list]
            if group_list and str(group_id) not in group_list_str:
                return
            expiry_ts = int(time.time()) + int(
                self.config.get("review_new_members_duration", 300)
            )
            self.new_member_watchlist[(group_id, user_id)] = expiry_ts
            logger.info(
                f"已将新成员{user_id}在群{group_id}登记为短期审查，直到{expiry_ts}"
            )

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def group_censor(self, event: AstrMessageEvent):
        """群消息审查"""
        group_list = self.config.get("group_list", [])
        group_id = event.get_group_id()
        group_list_str = [str(g) for g in group_list]
        if group_list and str(group_id) not in group_list_str:
            return

        # 新成员短期审查：如果发送者在监听表且未过期，则强制审查
        sender_key = (group_id, event.get_sender_id())
        should_run = False
        expiry = self.new_member_watchlist.get(sender_key)
        now_ts = int(time.time())
        if self.config.get("enable_review_new_members") and expiry and expiry > now_ts:
            # 在审查期内
            should_run = True
        elif expiry and expiry <= now_ts:
            # 已过期，清理
            self.new_member_watchlist.pop(sender_key, None)

        # 若既不在短期审查期，也未启用常规群消息审查，则直接返回
        if not should_run and not self.config.get("enable_group_msg_censor"):
            return

        await self.handle_message(event, event.message_obj.message)

    @filter.event_message_type(EventMessageType.PRIVATE_MESSAGE)
    async def private_censor(self, event: AstrMessageEvent):
        """私聊消息审查"""
        if self.config.get("enable_private_msg_censor"):
            await self.handle_message(event, event.message_obj.message)

    @filter.on_llm_response()
    async def output_censor(self, event: AstrMessageEvent, response: LLMResponse):
        """审核模型输出"""
        if self.config.get("enable_output_censor"):
            if not response.result_chain:
                res = await self.censor_flow.submit_text(
                    response.completion_text, event.unified_msg_origin
                )
                if res and res.risk_level != RiskLevel.Pass:
                    res.extra = {"user_id_str": event.get_sender_id()}
                    if self.config.get("enable_audit_log", True):
                        self.db_mgr.add_audit_log(res)
                    if res.risk_level == RiskLevel.Block:
                        if (
                            event.get_platform_name() == "aiocqhttp"
                            and event.get_group_id()
                        ):
                            await self._handle_aiocqhttp_group_message(event, res)
                        else:
                            logger.warning("非 aiocqhttp 平台的群消息，无法自动处置")
                        event.stop_event()
            elif response.result_chain:
                await self.handle_message(event, response.result_chain.chain)

    async def terminate(self):
        logger.debug("开始清理 AIOCensor 资源...")
        try:
            if self._notification_queue:
                try:
                    self._notification_queue.put_nowait({"type": "shutdown"})
                except Exception:
                    pass
            if self._notification_task:
                with suppress(asyncio.CancelledError):
                    await self._notification_task
            if self._notification_queue:
                try:
                    self._notification_queue.close()
                    self._notification_queue.join_thread()
                except Exception:
                    pass
                self._notification_queue = None
            self.db_mgr.close()
            await self.censor_flow.close()
            if self.scheduler:
                self.scheduler.shutdown()
            if self.web_ui_process:
                self.web_ui_process.terminate()
                self.web_ui_process.join(5)
                if self.web_ui_process.is_alive():
                    self.web_ui_process.kill()
                    logger.warning("web_ui_process 未在 5 秒内退出，强制终止")
        except Exception as e:
            logger.error(f"资源清理失败: {e!s}")
