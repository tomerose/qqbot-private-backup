"""Route every ordinary chat through the local Gemini provider."""

import asyncio
import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderType
from astrbot.api.star import Context, Star

try:
    from xiaoning_capabilities import match_capability
except ImportError:
    try:
        from data.plugins.xiaoning_capabilities import match_capability
    except ImportError:  # Minimal direct-module test harness.
        def match_capability(_text):
            return None

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

# Groups where questions always get a search-backed reply
_SEARCH_GROUPS = frozenset({"500009290"})

_QUESTION_RE = re.compile(
    r"[？?]$|"
    r"(?:什么|怎么|如何|为什么|为啥|谁|哪[个些种]?|多少|多久|几点|几时|"
    r"行不行|好不好|对不对|可不可以|能不能|有没有|是不是|"
    r"帮我查|帮我搜|帮我找|查一下|搜一下|找一下|"
    r"是什么意思|是什么|怎么用|怎么做|怎么办|怎么弄|怎么搞|"
    r"介绍|科普|解释|说说|讲讲|讲一下|说一下|"
    r"推荐|建议|哪个好|应该|值得)",
    re.IGNORECASE,
)


def _is_question(text: str) -> bool:
    """Heuristic question detection for auto-search routing."""
    if not text:
        return False
    if len(text) > 200:
        return False  # 长文不是简单提问
    return bool(_QUESTION_RE.search(text))


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

        target = "gemini-3.7-flash"
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
        """Apply deterministic group boundaries; never use per-user probabilities."""
        if event.is_private_chat():
            return
        if getattr(event, "is_at_or_wake_command", False):
            return
        if getattr(event, "_stop_event", False):
            return

        group_id = str(getattr(event, "get_group_id", lambda: "")() or "").strip()
        text = str(getattr(event, "get_message_str", lambda: "")() or "")
        # XiaoningCore marks an addressed continuation, explicit capability,
        # configured community event, or safety turn with this bounded flag.
        get_extra = getattr(event, "get_extra", None)
        if callable(get_extra) and bool(get_extra("xiaoning_force_group_reply", False)):
            return
        if _URGENT_LANGUAGE_RE.search(text):
            return
        # Explicit capability requests remain usable if the core plugin is
        # temporarily rolled back or unavailable.
        if match_capability(text) is not None:
            return
        # Configured search group questions are an explicit community rule.
        if group_id in _SEARCH_GROUPS and _is_question(text):
            return
        event.stop_event()
