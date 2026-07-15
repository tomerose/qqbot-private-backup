"""Route chat based on tier: X/Pro → Gemini, ordinary → DeepSeek. Groups: per-sender tier check."""

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderType
from astrbot.api.star import Context, Star

try:
    from draw_command.pro_access import get_tier, Tier
except ImportError:
    from data.plugins.draw_command.pro_access import get_tier, Tier


class ChatRouter(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._routes: dict[str, str] = {}
        self._pro_db = (
            __import__("pathlib").Path(__file__).resolve().parents[2]
            / "plugin_data" / "xiaoning_pro" / "pro_members.db"
        )

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=1000)
    async def route_provider(self, event: AstrMessageEvent):
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not umo:
            return

        group_id = str(getattr(event, "get_group_id", lambda: "")() or "").strip()
        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "").strip()

        # Pro groups: always Gemini. X/Pro users: Gemini. Ordinary: DeepSeek.
        if group_id:
            try:
                tier = get_tier(sender_id, self._pro_db)
                use_gemini = tier >= Tier.X
            except Exception:
                use_gemini = False
        else:
            use_gemini = True  # private chat always Gemini

        target = "gemini-2.5-flash" if use_gemini else "deepseek-chat"
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
