"""Route chat to the low-cost Gemini Flash provider."""

import asyncio
import random
import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderType
from astrbot.api.star import Context, Star

# ── 群消息自动回复概率 ──────────────────────────────────────────
# 仅对未 @mention、未被其他插件拦截的群聊消息生效。设为 0 关闭。
GROUP_AUTO_REPLY_PROBABILITY = 0.15

# 高活跃群：主动回复概率更高，能帮上忙时积极展示能力
_HIGH_ENGAGEMENT_GROUPS = {"945598390": 0.40}
# 高频聊天用户：这些用户的群消息更大概率触发回复
_FREQUENT_CHATTERS = frozenset({
    "3431017350", "1410546630", "3174222673",
    "2641419881", "3220305563", "1634854415",
})
# 指定用户回复概率（覆盖群和频率设置，1.0=100%必回）
_HIGH_ENGAGEMENT_USERS = {"943560334": 1.0}

# Give ordinary chat a brief chance to finish a thought.  This is deliberately
# short: commands, @mentions, media and urgent language must still be handled
# immediately.  Later fragments are folded into the first event so the normal
# LLM/context pipeline produces one contextual reply instead of several.
_REPLY_COALESCE_DELAY_SECONDS = 1.5
_REPLY_COALESCE_MAX_CHARS = 240
_URGENT_LANGUAGE_RE = re.compile(
    r"(?:自杀|自傷|自伤|想死|不想活|活不下去|割腕|救命|紧急|报警|120|110|"
    r"kill\s*myself|suicide)",
    re.IGNORECASE,
)


class ChatRouter(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._routes: dict[str, str] = {}
        self._route_write_lock = asyncio.Lock()
        self._reply_windows: dict[tuple[str, str], dict[str, object]] = {}
        self._reply_window_lock = asyncio.Lock()

    @staticmethod
    def _reply_coalesce_text(event: AstrMessageEvent) -> str:
        """Return plain, non-urgent conversational text eligible for merging."""
        try:
            text = str(event.get_message_str() or "").strip()
        except Exception:
            return ""
        if not text or len(text) > _REPLY_COALESCE_MAX_CHARS:
            return ""
        if text.startswith(("/", "／")):
            return ""
        # AstrBot marks ordinary private messages as wake messages when a
        # private-chat wake prefix is not required.  Those are still normal
        # conversation and should get the same short follow-up window; only a
        # group @/wake command needs an immediate reply.
        is_private = bool(getattr(event, "is_private_chat", lambda: False)())
        if getattr(event, "is_at_or_wake_command", False) and not is_private:
            return ""
        if _URGENT_LANGUAGE_RE.search(text):
            return ""

        # Do not delay an image, voice, file, quote, or other structured input:
        # its original event carries metadata that cannot safely be collapsed.
        get_messages = getattr(event, "get_messages", None)
        if callable(get_messages):
            try:
                components = list(get_messages() or [])
            except Exception:
                return ""
            if components:
                names = {type(component).__name__ for component in components}
                if names - {"Plain"}:
                    return ""
        return text

    @staticmethod
    def _replace_event_text(event: AstrMessageEvent, text: str) -> None:
        """Keep the normal LLM and context plugins on the merged user thought."""
        event.message_str = text
        message_obj = getattr(event, "message_obj", None)
        if message_obj is not None:
            try:
                message_obj.message_str = text
            except Exception:
                pass
        set_extra = getattr(event, "set_extra", None)
        if callable(set_extra):
            set_extra("xiaoning_reply_coalesced", True)

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=999)
    async def coalesce_followup_messages(self, event: AstrMessageEvent):
        """Merge a short same-speaker follow-up before the ordinary reply path."""
        text = self._reply_coalesce_text(event)
        if not text:
            return

        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        sender = str(getattr(event, "get_sender_id", lambda: "")() or "").strip()
        if not umo or not sender:
            return
        key = (umo, sender)

        async with self._reply_window_lock:
            active = self._reply_windows.get(key)
            if active is not None:
                parts = active["parts"]
                assert isinstance(parts, list)
                parts.append(text)
                event.stop_event()
                return
            active = {"event": event, "parts": [text]}
            self._reply_windows[key] = active

        await asyncio.sleep(_REPLY_COALESCE_DELAY_SECONDS)
        async with self._reply_window_lock:
            if self._reply_windows.get(key) is not active:
                return
            self._reply_windows.pop(key, None)
            parts = list(active["parts"])

        if len(parts) > 1:
            self._replace_event_text(event, "\n".join(parts))

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=1000)
    async def route_provider(self, event: AstrMessageEvent):
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not umo:
            return

        group_id = str(getattr(event, "get_group_id", lambda: "")() or "").strip()
        target = "gemini-2.5-flash"
        if self._routes.get(umo) == target:
            return

        async with self._route_write_lock:
            if self._routes.get(umo) == target:
                return
            try:
                await self.context.provider_manager.set_provider(
                    target, ProviderType.CHAT_COMPLETION, umo
                )
            except Exception as exc:
                logger.warning("[ChatRouter] provider switch failed: %s", type(exc).__name__)
                return
            self._routes[umo] = target

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=-200)
    async def gate_group_reply(self, event: AstrMessageEvent):
        """Randomly skip non-@ group messages. Higher probability for engaged groups/chatters."""
        if event.is_private_chat():
            return
        if getattr(event, "is_at_or_wake_command", False):
            return
        if getattr(event, "_stop_event", False):
            return

        # ── per-group + per-user probability ──
        group_id = str(getattr(event, "get_group_id", lambda: "")() or "").strip()
        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "").strip()
        prob = float(GROUP_AUTO_REPLY_PROBABILITY)
        if sender_id in _HIGH_ENGAGEMENT_USERS:
            prob = _HIGH_ENGAGEMENT_USERS[sender_id]
        elif group_id in _HIGH_ENGAGEMENT_GROUPS:
            prob = _HIGH_ENGAGEMENT_GROUPS[group_id]
        elif sender_id in _FREQUENT_CHATTERS:
            prob = 0.35
        if prob >= 1.0:
            return
        if random.random() >= prob:
            event.stop_event()
