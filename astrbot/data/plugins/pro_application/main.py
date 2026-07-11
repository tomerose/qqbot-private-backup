"""Human-reviewed Gmail application entry point for limited Pro access."""

from __future__ import annotations

import time
from pathlib import Path

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
from astrbot.core.message.message_event_result import MessageChain

from .pro_store import DEFAULT_PRO_DAYS, Application, ProStore, ProStoreError


REVIEWER_ID = "1211000567"
APPLICATION_EMAIL = "portelamicheli636@gmail.com"


class ProApplication(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        data_root = Path(__file__).resolve().parents[2] / "plugin_data" / "xiaoning_pro"
        self.context = context
        self.store = ProStore(data_root / "pro_members.db", reviewer_id=REVIEWER_ID)
        self._clock = time.time

    @staticmethod
    def _text(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_message_str", None)
        if callable(getter):
            return str(getter() or "").strip()
        return str(getattr(event, "message_str", "") or "").strip()

    @staticmethod
    def _sender(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_sender_id", None)
        return str(getter() if callable(getter) else "").strip()

    @staticmethod
    def _tokens(text: str) -> list[str] | None:
        value = str(text or "").strip()
        normalized = "".join(value.lower().split())
        if normalized in {"申请pro", "申请pro资格", "开通pro"}:
            return ["apply"]
        parts = value.split()
        if not parts or parts[0].lower() != "/pro":
            return None
        return [part.strip() for part in parts[1:] if part.strip()]

    @staticmethod
    def _apply_reply(application: Application) -> str:
        return (
            f"申请编号：{application.application_id}\n"
            f"请在 72 小时内发送邮件到：{APPLICATION_EMAIL}\n"
            f"主题：【小柠 Pro 申请】{application.application_id}\n"
            "正文请写：QQ 号、用途、想使用的功能、是否接受安全规则。\n"
            f"发完后回复：/pro sent {application.application_id}\n"
            "Pro 不包含本机 Agent 权限。"
        )

    @staticmethod
    def _status_reply(application: Application | None) -> str:
        if application is None:
            return "你目前暂无 Pro 申请或有效资格。"
        states = {
            "pending_email": "待发送邮件",
            "awaiting_review": "等待人工审核",
            "approval_pending_confirm": "等待审核人二次确认",
            "awaiting_verify": "等待 QQ 验证",
            "active": "Pro 已开通",
            "denied": "申请未通过",
            "revoked": "Pro 已撤销",
            "expired": "申请已过期",
            "pro_expired": "Pro 已到期",
            "verification_expired": "验证码已过期",
            "verification_locked": "验证码已锁定",
        }
        return f"当前状态：{states.get(application.state, '未知')}。"

    @staticmethod
    def _error_reply(error: ProStoreError) -> str:
        mapping = {
            "application_pending": "你已有未完成的 Pro 申请，请先完成或等待它过期。",
            "application_owner": "该申请不属于当前 QQ。",
            "application_expired": "申请已过期，请重新申请。",
            "application_state": "当前申请状态不支持此操作。",
            "reviewer_required": "无权执行此审核操作。",
            "duration_invalid": "有效期需在 1 到 365 天之间。",
            "resend_rate_limited": "刚补发过验证码，请 1 分钟后再试。",
            "verification_invalid": "验证码无效或已失效。",
            "verification_locked": "验证码已锁定，请重新申请。",
            "qq_id_invalid": "QQ 号无效。",
        }
        return mapping.get(str(error), "操作未完成，请稍后重试。")

    async def _send_private_code(
        self, event: AstrMessageEvent, qq_id: str, code: str
    ) -> bool:
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        if ":" not in origin:
            return False
        platform = origin.split(":", 1)[0]
        session = f"{platform}:FriendMessage:{qq_id}"
        message = MessageChain(
            [Plain(f"你的 Pro 验证码：{code}\n请在 10 分钟内回复：/pro verify {code}")]
        )
        try:
            return bool(await self.context.send_message(session, message))
        except Exception:
            return False

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=970)
    async def on_message(self, event: AstrMessageEvent):
        tokens = self._tokens(self._text(event))
        if tokens is None:
            return
        event.stop_event()
        sender_id = self._sender(event)
        action = tokens[0].lower() if tokens else "status"
        now = float(self._clock())
        try:
            if action == "apply":
                application = self.store.create_application(sender_id, now=now)
                yield event.plain_result(self._apply_reply(application))
                return
            if action == "sent" and len(tokens) == 2:
                self.store.mark_sent(tokens[1], sender_id, now=now)
                yield event.plain_result("已记录，申请已进入人工审核。")
                return
            if action == "status":
                yield event.plain_result(self._status_reply(self.store.status_for(sender_id, now=now)))
                return
            if action == "verify" and len(tokens) == 2:
                self.store.verify(sender_id, tokens[1], now=now)
                yield event.plain_result("验证成功，Pro 已开通。")
                return
            if action == "pending":
                pending = self.store.pending_for_review(sender_id, now=now)
                text = "待审核申请：" + ("、".join(item.application_id for item in pending) or "无")
                yield event.plain_result(text)
                return
            if action == "approve" and len(tokens) in {2, 3}:
                days = int(tokens[2]) if len(tokens) == 3 and tokens[2].isdigit() else DEFAULT_PRO_DAYS
                target = next(
                    (
                        item
                        for item in self.store.pending_for_review(sender_id, now=now)
                        if item.application_id == tokens[1].upper()
                    ),
                    None,
                )
                if target is None:
                    yield event.plain_result("未找到可审核的申请。")
                    return
                self.store.request_approval(tokens[1], sender_id, days, now=now)
                yield event.plain_result(
                    f"已发起二次确认，请在 5 分钟内回复：/pro confirm {tokens[1].upper()}"
                )
                return
            if action == "confirm" and len(tokens) == 2:
                target_qq = self.store.delivery_target(
                    tokens[1], sender_id, "approval_pending_confirm", now=now
                )
                code = self.store.confirm_approval(tokens[1], sender_id, now=now)
                if not await self._send_private_code(event, target_qq, code):
                    self.store.reset_verification(tokens[1], sender_id, now=now)
                    yield event.plain_result("验证码暂未送达，申请尚未开通；请让申请人先私聊小柠后重新审核。")
                    return
                yield event.plain_result("二次确认完成，验证码已发送到申请人私聊。")
                return
            if action == "resend" and len(tokens) == 2:
                target_qq = self.store.delivery_target(tokens[1], sender_id, "awaiting_verify", now=now)
                code = self.store.resend_verification(tokens[1], sender_id, now=now)
                if not await self._send_private_code(event, target_qq, code):
                    yield event.plain_result("验证码暂未送达，申请保持待 QQ 验证；请 1 分钟后补发。")
                    return
                yield event.plain_result("新验证码已发送到申请人私聊，旧验证码已失效。")
                return
            if action == "audit" and len(tokens) == 2:
                events = self.store.audit_for(tokens[1], sender_id, now=now)
                details = "\n".join(
                    f"{item.event_type} | {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(item.event_at))}"
                    for item in events
                )
                yield event.plain_result(f"审核记录：\n{details or '暂无记录'}")
                return
            if action == "deny" and len(tokens) == 2:
                yield event.plain_result("已拒绝申请。" if self.store.deny(tokens[1], sender_id, now=now) else "未找到可拒绝的申请。")
                return
            if action == "revoke" and len(tokens) == 2:
                yield event.plain_result("已撤销 Pro。" if self.store.revoke(tokens[1], sender_id, now=now) else "该 QQ 当前没有有效 Pro。")
                return
            yield event.plain_result("用法：/pro apply|sent|status|verify；审核指令仅限审核人。")
        except ProStoreError as error:
            yield event.plain_result(self._error_reply(error))
        except ValueError:
            yield event.plain_result("参数格式不正确。")
