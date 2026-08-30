"""Create, preview and publish small web tools from QQ."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

try:
    from draw_command.pro_access import get_tier, Tier
except ImportError:
    from data.plugins.draw_command.pro_access import get_tier, Tier

try:
    from xiaoning_runtime import deliver_local_artifact, mirror_runtime_task_status
except ImportError:
    from data.plugins.xiaoning_runtime import deliver_local_artifact, mirror_runtime_task_status
try:
    from xiaoning_core.ownership import route_allows
except ImportError:
    try:
        from data.plugins.xiaoning_core.ownership import route_allows
    except ImportError:
        def route_allows(_event, _owner):
            return True

from .core import (
    PageStore,
    UnsafePageError,
    new_page_id,
    prepare_html,
    requirement_gaps,
)
from .generator import GenerationError, generate_draft, review_draft, revise_page
from .publisher import FirebasePublisher, PublishError


X_DAILY = 1
PRO_DAILY = 5
X_ACTIVE = 3
PRO_ACTIVE = 20
WEB_HELP = (
    "【小柠网页工坊】\n"
    "/web <需求> — 制作并发布网页工具\n"
    "/web list — 查看自己的页面\n"
    "/web show <ID> — 重发链接、HTML 和预览\n"
    "/web edit <ID> <修改> — 保持原链接继续修改\n"
    "/web delete <ID> confirm — 删除公开页面\n"
    "X：1次/天，最多3个页面；Pro：5次/天，最多20个页面并独立复核。\n"
    "页面会通过 HTTPS 公开；不要提交姓名、电话、账号等隐私。"
)

_PAGE_ID = re.compile(r"^[a-f0-9]{10}$", re.I)
_NATURAL_PREFIX = re.compile(
    r"^\s*(?:小柠[，,：:\s]*)?(?:(?:能不能|可以|能)\s*)?(?:请|帮我|给我)?\s*"
    r"(?:做|制作|生成|创建)(?:一个|个)?(?:网页工具|网页|页面|网站)"
    r"[，,：:\s]*(?P<request>.+?)\s*$",
    re.I,
)
_NATURAL_SUFFIX = re.compile(
    r"^\s*(?:小柠[，,：:\s]*)?(?:(?:能不能|可以|能)\s*)?(?:请|帮我|给我)?\s*"
    r"(?:做|制作|生成|创建)(?:一个|个)?(?P<request>.+?)"
    r"(?:的)?(?:网页工具|网页|页面|网站)\s*$",
    re.I,
)
_NATURAL_TOOL = re.compile(
    r"^\s*(?:小柠[，,：:\s]*)?(?:(?:能不能|可以|能)\s*)?(?:请|帮我|给我)?\s*"
    r"(?:做|制作|生成|创建)(?:一个|个)?"
    r"(?P<request>(?:整理|管理|记录|统计|清单|记账|图库|相册|倒计时|番茄钟|"
    r"计算|抽签|打卡|日程).+?)(?:的)?(?:小工具|工具|东西)"
    r"(?:吗|吧|呢)?[？?]?\s*$",
    re.I,
)
_NATURAL_EDIT_LATEST = re.compile(
    r"^\s*(?:小柠[，,：:\s]*)?(?:请|帮我|给我)?\s*(?:把)?\s*"
    r"(?:刚才|之前|上一个)?\s*(?:这个|那个|做的)?\s*"
    r"(?:网页|页面|网站|网页工具|工具)\s*(?:再)?\s*[，,：:]?\s*"
    r"(?P<request>(?:加上|增加|添加|改成|换成|删掉|删除|调整|修改).+?)\s*$",
    re.I,
)


@dataclass(frozen=True)
class WebIntent:
    action: str
    page_id: str = ""
    payload: str = ""


def parse_web_intent(text: object) -> WebIntent | None:
    value = str(text or "").strip()
    lowered = value.lower()
    if lowered == "/web":
        return WebIntent("help")
    if lowered.startswith("/web "):
        rest = value[5:].strip()
        head, _, tail = rest.partition(" ")
        action = head.lower()
        if action in {"help", "帮助"}:
            return WebIntent("help")
        if action in {"list", "列表"}:
            return WebIntent("list")
        if action in {"show", "查看", "open", "打开"}:
            return WebIntent("show", tail.strip().lower())
        if action in {"edit", "revise", "修改"}:
            page_id, _, changes = tail.strip().partition(" ")
            return WebIntent("edit", page_id.lower(), changes.strip())
        if action in {"delete", "remove", "删除"}:
            page_id, _, confirmation = tail.strip().partition(" ")
            return WebIntent("delete", page_id.lower(), confirmation.strip().lower())
        if action in {"create", "new", "制作", "创建"}:
            return WebIntent("create", payload=tail.strip())
        return WebIntent("create", payload=rest)
    latest_edit = _NATURAL_EDIT_LATEST.match(value)
    if latest_edit:
        return WebIntent("edit_latest", payload=latest_edit.group("request").strip())
    for pattern in (_NATURAL_PREFIX, _NATURAL_SUFFIX, _NATURAL_TOOL):
        match = pattern.match(value)
        if match:
            return WebIntent("create", payload=match.group("request").strip(" ，,：:"))
    return None


def _record_id(record: object) -> str:
    return str(getattr(record, "id", getattr(record, "page_id", "")))


class WebStudio(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self._data_root = Path(StarTools.get_data_dir("web_studio"))
        self._data_root.mkdir(parents=True, exist_ok=True)
        self._store = PageStore(self._data_root / "pages.db")
        self._publisher = FirebasePublisher(self._data_root)
        self._publish_lock = asyncio.Lock()
        self._pro_db = (
            Path(__file__).resolve().parents[2]
            / "plugin_data" / "xiaoning_pro" / "pro_members.db"
        )

    @staticmethod
    def _sender(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_sender_id", None)
        return str(getter() if callable(getter) else "").strip()

    @staticmethod
    def _tier_limits(tier: Tier) -> tuple[int, int]:
        return (PRO_DAILY, PRO_ACTIVE) if tier == Tier.PRO else (X_DAILY, X_ACTIVE)

    def _refund(self, owner: str, reserved_day: str | None) -> None:
        if reserved_day is None:
            return
        try:
            self._store.refund(owner, reserved_day)
        except (ValueError, sqlite3.Error) as exc:
            logger.error("[WebStudio] quota refund failed: %s", type(exc).__name__)

    async def _rollback_publish(self, page_id: str, snapshot, should_exist: bool) -> bool:
        """Restore both local artifacts and the public Hosting release."""
        try:
            self._publisher.restore(page_id, snapshot)
            await asyncio.to_thread(
                self._publisher.deploy,
                page_id,
                should_exist,
            )
            return True
        except (PublishError, OSError) as exc:
            logger.error("[WebStudio] rollback failed: %s", type(exc).__name__)
            return False

    async def _deliver(self, event: AstrMessageEvent, page_id: str) -> tuple[bool, bool]:
        html_path = self._publisher.page_path(page_id)
        preview = self._publisher.preview_path(page_id)
        html_result = await deliver_local_artifact(
            event,
            html_path,
            allowed_roots=[self._publisher.public_root],
            kind="file",
        )
        preview_delivered = False
        if preview.is_file():
            image_result = await deliver_local_artifact(
                event,
                preview,
                allowed_roots=[self._data_root],
                kind="image",
            )
            preview_delivered = image_result.delivered
        return html_result.delivered, preview_delivered

    @staticmethod
    def _attachment_status(html_sent: bool, preview_sent: bool) -> str:
        if html_sent and preview_sent:
            return "HTML 和预览已发回 QQ"
        if html_sent:
            return "HTML 已发回 QQ；预览暂未交付，文件已安全保留"
        if preview_sent:
            return "预览已发回 QQ；HTML 暂未交付，文件已安全保留"
        return "HTML 和预览暂未交付，文件已安全保留"

    @staticmethod
    async def _finalize_html(request: str, raw: str) -> tuple[str, str]:
        """Bounded safety and requirement repair; never publish a partial draft."""

        async def validate(candidate: str) -> tuple[str, str]:
            for attempt in range(3):
                try:
                    return prepare_html(candidate)
                except UnsafePageError as exc:
                    if attempt == 2:
                        raise
                    repair_request = (
                        f"{request[:900]}。当前草稿安全检查未通过：{exc}。"
                        "必须删除相关危险结构，禁止表单提交、外链、联网、跳转、"
                        "登录和支付，同时保留核心功能。"
                    )
                    candidate = await asyncio.to_thread(
                        review_draft, repair_request, candidate
                    )
            raise GenerationError("网页安全检查未通过")

        html, title = await validate(raw)
        for _ in range(2):
            gaps = requirement_gaps(request, html)
            if not gaps:
                return html, title
            repair_request = (
                f"{request[:900]}。当前草稿遗漏：{'、'.join(gaps)}，必须逐项实现。"
            )
            revised = await asyncio.to_thread(review_draft, repair_request, html)
            html, title = await validate(revised)
        raise GenerationError("网页没有覆盖用户核心需求")

    async def _show(self, event: AstrMessageEvent, owner: str, page_id: str):
        if not _PAGE_ID.fullmatch(page_id):
            yield event.plain_result("页面 ID 格式不对。发送 /web list 查看自己的页面。")
            return
        try:
            record = self._store.get(page_id, owner)
        except sqlite3.Error:
            yield event.plain_result("网页记录暂时无法读取，请稍后再试。")
            return
        if record is None or not self._publisher.page_path(page_id).is_file():
            yield event.plain_result("没有找到你的这个页面。发送 /web list 查看页面 ID。")
            return
        try:
            html_sent, preview_sent = await self._deliver(event, page_id)
        except Exception as exc:
            logger.warning("[WebStudio] QQ attachment delivery failed: %s", type(exc).__name__)
            html_sent, preview_sent = False, False
        yield event.plain_result(
            f"《{record.title}》\n{self._publisher.page_url(page_id)}\n"
            f"链接可直接打开；{self._attachment_status(html_sent, preview_sent)}。"
        )

    async def _create(self, event: AstrMessageEvent, owner: str, tier: Tier, request: str):
        reserved_day: str | None = None
        uncertain_publish = False
        page_id = new_page_id()
        task_desc = f"制作网页：{request[:160]}"
        try:
            daily_limit, active_limit = self._tier_limits(tier)
            if self._store.active_count(owner) >= active_limit:
                yield event.plain_result(
                    f"你的在用页面已到上限（{active_limit} 个）。先用 /web delete <ID> confirm 删除一个。"
                )
                return
            accepted, used, usage_day = self._store.consume(owner, daily_limit)
            if not accepted:
                yield event.plain_result(
                    f"今天的网页制作次数已用完（{used}/{daily_limit}），明天北京时间重置。"
                )
                return
            reserved_day = usage_day
            await mirror_runtime_task_status(
                owner, page_id, task_desc, "in_progress", "generation_started", owner="web"
            )
            yield event.plain_result(
                f"正在制作网页（今日 {used}/{daily_limit}）…完成后会返回预览、HTML 和 HTTPS 链接。"
            )
            raw = await asyncio.to_thread(generate_draft, request)
            if tier == Tier.PRO:
                raw = await asyncio.to_thread(review_draft, request, raw)
            html, title = await self._finalize_html(request, raw)
            async with self._publish_lock:
                if self._store.active_count(owner) >= active_limit:
                    raise RuntimeError("active limit reached")
                self._store.create(page_id, owner, title, request, tier.value)
                try:
                    self._publisher.stage(page_id, html)
                    try:
                        await asyncio.to_thread(self._publisher.render_preview, page_id)
                    except PublishError as exc:
                        logger.warning("[WebStudio] preview failed: %s", type(exc).__name__)
                    await asyncio.to_thread(self._publisher.deploy, page_id, True)
                except Exception:
                    rolled_back = await self._rollback_publish(page_id, None, False)
                    if rolled_back:
                        try:
                            if not self._store.delete(page_id, owner):
                                uncertain_publish = True
                                reserved_day = None
                        except sqlite3.Error as exc:
                            logger.error("[WebStudio] failed to remove broken record: %s", type(exc).__name__)
                            uncertain_publish = True
                            reserved_day = None
                    else:
                        uncertain_publish = True
                        reserved_day = None
                    raise
            reserved_day = None
            try:
                html_sent, preview_sent = await self._deliver(event, page_id)
            except Exception as exc:
                logger.warning("[WebStudio] QQ attachment delivery failed: %s", type(exc).__name__)
                html_sent, preview_sent = False, False
            delivered = self._attachment_status(html_sent, preview_sent)
            await mirror_runtime_task_status(
                owner,
                page_id,
                task_desc,
                "done",
                f"public_url_verified;html={int(html_sent)};preview={int(preview_sent)}",
                owner="web",
            )
            yield event.plain_result(
                f"网页做好了：{title}\n{self._publisher.page_url(page_id)}\n"
                f"页面 ID：{page_id}（以后可原址修改）\n{delivered}。"
            )
        except (
            OSError, ValueError, sqlite3.Error, GenerationError,
            UnsafePageError, PublishError, RuntimeError,
        ) as exc:
            self._refund(owner, reserved_day)
            logger.warning("[WebStudio] create failed: %s", type(exc).__name__)
            await mirror_runtime_task_status(
                owner,
                page_id,
                task_desc,
                "delivery_pending" if uncertain_publish else "failed",
                f"create_{type(exc).__name__}",
                owner="web",
            )
            if isinstance(exc, (ValueError, UnsafePageError)):
                yield event.plain_result(f"这个网页不能发布：{exc}。本次次数已退回。")
            elif uncertain_publish:
                yield event.plain_result("发布结果暂时无法确认，记录已保留在 /web list，请稍后用 /web show 检查或删除。")
            else:
                yield event.plain_result("网页制作失败，本次次数已退回。请稍后再试或把需求写得更具体。")

    async def _edit(
        self, event: AstrMessageEvent, owner: str, tier: Tier, page_id: str, changes: str
    ):
        if not _PAGE_ID.fullmatch(page_id) or not changes:
            yield event.plain_result("格式：/web edit <页面ID> <要修改的内容>")
            return
        try:
            record = self._store.get(page_id, owner)
        except sqlite3.Error:
            yield event.plain_result("网页记录暂时无法读取，请稍后再试。")
            return
        path = self._publisher.page_path(page_id)
        if record is None or not path.is_file():
            yield event.plain_result("没有找到你的这个页面。发送 /web list 查看页面 ID。")
            return
        daily_limit, _ = self._tier_limits(tier)
        accepted, used, reserved_day = self._store.consume(owner, daily_limit)
        if not accepted:
            yield event.plain_result(
                f"今天的网页制作次数已用完（{used}/{daily_limit}），明天北京时间重置。"
            )
            return
        task_desc = f"修改网页《{record.title}》：{changes[:140]}"
        await mirror_runtime_task_status(
            owner, page_id, task_desc, "in_progress", "revision_started", owner="web"
        )
        yield event.plain_result(f"正在原址修改《{record.title}》（今日 {used}/{daily_limit}）…")
        uncertain_publish = False
        try:
            previous = self._publisher.snapshot(page_id)
            existing = self._publisher.read_app(page_id)
            raw = await asyncio.to_thread(revise_page, record.prompt, existing, changes)
            combined_prompt = f"{record.prompt}；修改：{changes}"[:1200]
            if tier == Tier.PRO:
                raw = await asyncio.to_thread(review_draft, combined_prompt, raw)
            html, title = await self._finalize_html(combined_prompt, raw)
            async with self._publish_lock:
                current = self._store.get(page_id, owner)
                current_snapshot = self._publisher.snapshot(page_id)
                if (
                    current is None
                    or current.updated_at != record.updated_at
                    or current_snapshot.document != previous.document
                ):
                    raise RuntimeError("page changed while editing")
                try:
                    self._publisher.stage(page_id, html)
                    try:
                        await asyncio.to_thread(self._publisher.render_preview, page_id)
                    except PublishError as exc:
                        logger.warning("[WebStudio] preview failed: %s", type(exc).__name__)
                    await asyncio.to_thread(self._publisher.deploy, page_id, True)
                    updated = self._store.update(page_id, owner, title, combined_prompt)
                    if updated is None:
                        raise RuntimeError("page disappeared while editing")
                except Exception:
                    if not await self._rollback_publish(page_id, previous, True):
                        uncertain_publish = True
                        reserved_day = None
                    raise
            reserved_day = None
            try:
                html_sent, preview_sent = await self._deliver(event, page_id)
            except Exception as exc:
                logger.warning("[WebStudio] QQ attachment delivery failed: %s", type(exc).__name__)
                html_sent, preview_sent = False, False
            await mirror_runtime_task_status(
                owner,
                page_id,
                task_desc,
                "done",
                f"public_url_verified;html={int(html_sent)};preview={int(preview_sent)}",
                owner="web",
            )
            yield event.plain_result(
                f"已原址更新：{title}\n{self._publisher.page_url(page_id)}\n"
                f"页面 ID 仍是 {page_id}；{self._attachment_status(html_sent, preview_sent)}。"
            )
        except (
            OSError, ValueError, sqlite3.Error, GenerationError,
            UnsafePageError, PublishError, RuntimeError,
        ) as exc:
            self._refund(owner, reserved_day)
            logger.warning("[WebStudio] edit failed: %s", type(exc).__name__)
            await mirror_runtime_task_status(
                owner,
                page_id,
                task_desc,
                "delivery_pending" if uncertain_publish else "failed",
                f"edit_{type(exc).__name__}",
                owner="web",
            )
            if isinstance(exc, (ValueError, UnsafePageError)):
                yield event.plain_result(f"这次修改不能发布：{exc}。本次次数已退回。")
            elif uncertain_publish:
                yield event.plain_result("原址更新状态暂时无法确认，记录已保留；请稍后用 /web show 检查。")
            else:
                yield event.plain_result("网页修改失败，本次次数已退回，原页面保持不变。")

    async def _delete(self, event: AstrMessageEvent, owner: str, page_id: str, confirm: str):
        if not _PAGE_ID.fullmatch(page_id):
            yield event.plain_result("格式：/web delete <页面ID> confirm")
            return
        uncertain_publish = False
        try:
            record = self._store.get(page_id, owner)
        except sqlite3.Error:
            yield event.plain_result("网页记录暂时无法读取，请稍后再试。")
            return
        if record is None:
            yield event.plain_result("没有找到你的这个页面。")
            return
        if confirm not in {"confirm", "确认"}:
            yield event.plain_result(
                f"将公开删除《{record.title}》。确认请发送：/web delete {page_id} confirm"
            )
            return
        try:
            async with self._publish_lock:
                current = self._store.get(page_id, owner)
                if current is None or current.updated_at != record.updated_at:
                    raise RuntimeError("page changed before deletion")
                previous = self._publisher.remove(page_id)
                try:
                    await asyncio.to_thread(self._publisher.deploy, page_id, False)
                    if not self._store.delete(page_id, owner):
                        raise RuntimeError("page disappeared while deleting")
                except Exception:
                    if not await self._rollback_publish(page_id, previous, True):
                        uncertain_publish = True
                    raise
            yield event.plain_result(f"已删除《{record.title}》，原 HTTPS 链接不再提供页面。")
        except (OSError, sqlite3.Error, PublishError, RuntimeError) as exc:
            logger.warning("[WebStudio] delete failed: %s", type(exc).__name__)
            if uncertain_publish:
                yield event.plain_result("删除状态暂时无法确认，记录已保留；请稍后用 /web show 检查。")
            else:
                yield event.plain_result("删除发布失败，原页面已保留，请稍后重试。")

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=985)
    async def on_message(self, event: AstrMessageEvent):
        if not route_allows(event, "web_studio"):
            return
        intent = parse_web_intent(
            str(getattr(event, "get_message_str", lambda: "")() or "")
        )
        if intent is None:
            return
        event.stop_event()
        if not event.is_private_chat():
            yield event.plain_result("网页会生成公开链接，请私聊小柠后再发送这条需求。")
            return
        owner = self._sender(event)
        if not owner.isdigit():
            yield event.plain_result("暂时无法识别你的 QQ 账号，请稍后再试。")
            return
        if intent.action == "help":
            yield event.plain_result(WEB_HELP)
            return
        if intent.action == "list":
            try:
                records = self._store.list(owner)
            except sqlite3.Error:
                yield event.plain_result("网页记录暂时无法读取，请稍后再试。")
                return
            if not records:
                yield event.plain_result("你还没有网页。发送 /web <需求> 就能制作第一个。")
                return
            lines = [
                f"{index}. {_record_id(record)}｜{record.title}｜{self._publisher.page_url(_record_id(record))}"
                for index, record in enumerate(records, 1)
            ]
            yield event.plain_result("【我的网页】\n" + "\n".join(lines))
            return
        if intent.action == "show":
            async for result in self._show(event, owner, intent.page_id):
                yield result
            return
        if intent.action == "delete":
            async for result in self._delete(event, owner, intent.page_id, intent.payload):
                yield result
            return
        tier = get_tier(owner, self._pro_db)
        if tier < Tier.X:
            yield event.plain_result(
                "网页工坊需要 X 或 Pro。添加小柠为 QQ 好友即可获得 X 资格。"
            )
            return
        if intent.action == "edit":
            async for result in self._edit(
                event, owner, tier, intent.page_id, intent.payload
            ):
                yield result
            return
        if intent.action == "edit_latest":
            try:
                latest = self._store.list(owner)
            except sqlite3.Error:
                yield event.plain_result("网页记录暂时无法读取，请稍后再试。")
                return
            if not latest:
                yield event.plain_result("你还没有可修改的网页；先把要做的网页说清楚。")
                return
            async for result in self._edit(
                event, owner, tier, _record_id(latest[0]), intent.payload
            ):
                yield result
            return
        async for result in self._create(event, owner, tier, intent.payload):
            yield result
