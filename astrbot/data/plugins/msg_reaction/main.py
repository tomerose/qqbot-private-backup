"""Add a lightweight QQ reaction for a small set of explicit phrases."""

from __future__ import annotations

import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

REACTION_MAP: tuple[tuple[str, str], ...] = (
    ("谢谢小柠", "76"),
    ("谢谢", "76"),
    ("感谢", "76"),
    ("哈哈", "269"),
    ("笑死", "269"),
    ("晚安", "292"),
    ("早安", "293"),
    ("早上好", "293"),
    ("太强了", "311"),
    ("厉害", "311"),
    ("辛苦了", "289"),
    ("加油", "289"),
    ("好的", "124"),
    ("ok", "124"),
    ("嗯嗯", "124"),
)


def pick_reaction_emoji(text: str) -> str | None:
    value = str(text or "").strip().lower()
    for keyword, emoji in REACTION_MAP:
        kw = keyword.lower()
        if kw in value:
            # "ok" is too aggressive as substring — require word boundary
            if kw == "ok" and re.search(r"(?:^|[^a-z])ok(?:$|[^a-z])", value) is None:
                continue
            return emoji
    return None


def _get_message_id(event: AstrMessageEvent) -> str | None:
    message_obj = getattr(event, "message_obj", None)
    if message_obj is None:
        return None
    raw_message = getattr(message_obj, "raw_message", None)
    if isinstance(raw_message, dict) and raw_message.get("message_id") is not None:
        return str(raw_message["message_id"])
    message_id = getattr(message_obj, "message_id", None)
    return str(message_id) if message_id is not None else None


class MsgReaction(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)

    @filter.on_decorating_result(priority=-400)
    async def add_reaction(self, event: AstrMessageEvent) -> None:
        emoji_id = pick_reaction_emoji(
            str(getattr(event, "get_message_str", lambda: "")() or "")
        )
        message_id = _get_message_id(event)
        if emoji_id is None or message_id is None:
            return
        try:
            call_action = getattr(getattr(event, "bot", None), "call_action", None)
            if callable(call_action):
                await call_action(
                    "set_msg_emoji_like",
                    message_id=message_id,
                    emoji_id=emoji_id,
                )
        except Exception:
            logger.debug("[MsgReaction] set_msg_emoji_like failed")
