"""定制图 — PRO 1次/天。需求转发给管理员，管理员回复图片后自动转发给原用户。

ponytail: single-file, no DB — in-memory req_id→user mapping. Lost on restart (acceptable)."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star
from astrbot.core.message.message_event_result import MessageChain

try:
    from draw_command.pro_access import get_tier, Tier
except ImportError:
    from data.plugins.draw_command.pro_access import get_tier, Tier

REVIEWER_ID = "1211000567"
CUSTOM_DRAW_DAILY = 1

_TRIGGER_RE = re.compile(
    r"(?:/定制图|/custom[_-]?draw|帮我定制|定制一张|定制图|人工画|人工绘制)",
    re.I,
)


class CustomDraw(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self._daily_usage: dict[str, int] = {}
        # In-memory pending requests: req_id → (original_qq, description, timestamp)
        self._pending: dict[str, tuple[str, str, float]] = {}
        self._pro_db = (
            Path(__file__).resolve().parents[2]
            / "plugin_data" / "xiaoning_pro" / "pro_members.db"
        )

    @staticmethod
    def _sender(event: AstrMessageEvent) -> str:
        g = getattr(event, "get_sender_id", None)
        return str(g() if callable(g) else "").strip()

    @staticmethod
    def _text(event: AstrMessageEvent) -> str:
        g = getattr(event, "get_message_str", None)
        return str(g() if callable(g) else "").strip()

    def _extract_description(self, text: str) -> str | None:
        """Extract the drawing description from trigger text."""
        # /定制图 <描述>
        if text.startswith("/"):
            parts = text.split(maxsplit=1)
            if len(parts) >= 2 and parts[1].strip():
                return parts[1].strip()
            return ""  # empty = show help
        # Natural: "帮我定制一张xxx" / "定制图xxx"
        m = _TRIGGER_RE.search(text)
        if m:
            desc = text[m.end():].strip()
            return desc if desc else ""
        return None

    # ── PRO user triggers custom draw ──────────────────────────────

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=965)
    async def on_custom_draw(self, event: AstrMessageEvent):
        text = self._text(event)
        if not _TRIGGER_RE.search(text):
            return
        if not (event.is_private_chat() or event.is_at_or_wake_command):
            return

        sender = self._sender(event)
        if not sender.isdigit():
            return

        tier = get_tier(sender, self._pro_db)
        if tier < Tier.PRO:
            yield event.plain_result(
                "定制图是 Pro 专属功能（1次/天），由人工绘制高品质出图。\n"
                "发送 /pro status 查看当前资格。"
            )
            event.stop_event()
            return

        desc = self._extract_description(text)
        if desc == "":
            yield event.plain_result(
                "【定制图】Pro 1次/天，人工绘制。\n"
                "用法：/定制图 <描述>\n"
                "示例：/定制图 一只穿西装的猫在喝咖啡，油画风格"
            )
            event.stop_event()
            return
        if desc is None:
            return

        # Daily limit
        today = time.strftime("%Y%m%d")
        dk = f"{sender}:{today}"
        used = self._daily_usage.get(dk, 0)
        if used >= CUSTOM_DRAW_DAILY:
            yield event.plain_result(
                f"今日定制图次数已用完（{used}/{CUSTOM_DRAW_DAILY}）。明天自动重置。"
            )
            event.stop_event()
            return

        # Create pending request
        req_id = uuid.uuid4().hex[:8].upper()
        self._pending[req_id] = (sender, desc, time.time())

        # Forward to reviewer
        origin = ""
        for inst in self.context.platform_manager.platform_insts:
            meta = getattr(inst, "metadata", None)
            if meta and hasattr(meta, "id"):
                origin = str(meta.id)
                break
        session = f"{origin}:FriendMessage:{REVIEWER_ID}" if origin else f"aiocqhttp:FriendMessage:{REVIEWER_ID}"
        msg = MessageChain([Plain(
            f"【定制图】#{req_id}\n描述: {desc}\n\n"
            f"→ 回复此消息并附上图片即可交付给用户。"
        )])
        try:
            await self.context.send_message(session, msg)
            self._daily_usage[dk] = used + 1
            yield event.plain_result(
                f"定制图需求已提交（#{req_id}），人工绘制中，请耐心等待。\n"
                f"今日剩余：{CUSTOM_DRAW_DAILY - used - 1} 次。"
            )
        except Exception:
            self._pending.pop(req_id, None)
            yield event.plain_result("定制图提交失败，请稍后重试。")
        event.stop_event()

    # ── Reviewer replies with image → forward to original user ─────

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=960)
    async def on_reviewer_reply(self, event: AstrMessageEvent):
        """Detect reviewer replying to a custom-draw request with an image."""
        sender = self._sender(event)
        if sender != REVIEWER_ID:
            return

        # Check if this is a reply to a pending request message
        # We need to check the reply-to message content for our #req_id marker
        reply_msg = getattr(event, "reply", None)
        if reply_msg is None:
            # Try alternate way — check if event has a replied message
            raw_event = getattr(event, "_event", None) or getattr(event, "raw_event", None)
            if raw_event is not None and isinstance(raw_event, dict):
                reply_msg = raw_event.get("reply") or raw_event.get("message_reply")

        if reply_msg is None:
            return

        # Extract text from reply target
        reply_text = ""
        if isinstance(reply_msg, dict):
            reply_text = str(reply_msg.get("message", "") or reply_msg.get("raw_message", "") or "")
        elif isinstance(reply_msg, str):
            reply_text = reply_msg
        else:
            reply_text = str(getattr(reply_msg, "message", "") or "")

        # Find req_id
        m = re.search(r"#([A-Z0-9]{8})", reply_text)
        if not m:
            return

        req_id = m.group(1)
        if req_id not in self._pending:
            return

        original_qq, desc, _ = self._pending.pop(req_id)

        # Check for images in the reviewer's reply
        message_chain = getattr(event, "message_chain", None)
        images: list[Image] = []
        if message_chain is not None:
            for comp in message_chain:
                if isinstance(comp, Image) or getattr(comp, "type", "") == "image":
                    images.append(comp)

        if not images:
            yield event.plain_result(f"未检测到图片（#{req_id}），请回复时附上图片。")
            event.stop_event()
            # Put back the pending request
            self._pending[req_id] = (original_qq, desc, time.time())
            return

        # Forward images to original user
        origin = ""
        for inst in self.context.platform_manager.platform_insts:
            meta = getattr(inst, "metadata", None)
            if meta and hasattr(meta, "id"):
                origin = str(meta.id)
                break
        session = f"{origin}:FriendMessage:{original_qq}" if origin else f"aiocqhttp:FriendMessage:{original_qq}"

        try:
            forward = MessageChain([Plain("【定制图交付】\n")] + images)
            await self.context.send_message(session, forward)
            yield event.plain_result(f"已交付给用户（#{req_id}）。")
            logger.info("[CustomDraw] delivered #%s to %s", req_id, original_qq)
        except Exception as exc:
            logger.warning("[CustomDraw] forward failed #%s: %s", req_id, exc)
            yield event.plain_result(f"交付失败（#{req_id}）：{exc}")
            self._pending[req_id] = (original_qq, desc, time.time())
        event.stop_event()

    # ── Cleanup stale pending requests ─────────────────────────────

    async def initialize(self):
        """Start periodic cleanup of stale pending requests (>24h)."""
        logger.info("[CustomDraw] 定制图功能已就绪")

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=950)
    async def cleanup_stale(self, event: AstrMessageEvent):
        """Periodically clean stale requests (triggered on any message, lightweight)."""
        now = time.time()
        stale = [
            rid for rid, (_, _, ts) in self._pending.items()
            if now - ts > 86400  # 24h
        ]
        for rid in stale:
            self._pending.pop(rid, None)
