"""Pure input and rate-limit policy for the Pro drawing command."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Iterable


MAX_PROMPT_CHARS = 500
_QQ_ID = re.compile(r"^[1-9]\d{4,11}$")
_NATURAL_DRAW = re.compile(
    r"^(?:小柠[，,\s]*)?(?:帮我|请)?(?:"
    r"(?:画|绘制|作图)(?:一张|一个)?(?:图片|图|海报|插画|封面)?"
    r"|生成(?:一张|一个)(?:图片|图|海报|插画|封面))"
    r"[：:，,\s]*(.+)$",
    re.I,
)
_NATURAL_GENERATE_IMAGE = re.compile(
    r"^(?:小柠[，,\s]*)?(?:帮我|请)?生成(?:一张|一个)"
    r"(.+?)(?:图片|海报|插画|封面|图)$",
    re.I,
)


class DrawRequestError(ValueError):
    """A safe, user-facing drawing request validation error."""


def parse_pro_user_ids(value: object) -> tuple[str, ...]:
    """Freeze valid QQ IDs without importing another AstrBot plugin."""
    if isinstance(value, str):
        candidates = re.split(r"[\s,;]+", value)
    elif isinstance(value, Iterable):
        candidates = [str(item) for item in value]
    else:
        candidates = []
    return tuple(
        dict.fromkeys(
            candidate.strip()
            for candidate in candidates
            if _QQ_ID.fullmatch(candidate.strip())
        )
    )


def parse_draw_command(text: object) -> str | None:
    raw = str(text or "").strip()
    lowered = raw.lower()
    if lowered in {"生成图片", "帮我生成图片", "请生成图片", "画图"}:
        return "一张适合分享的高质量图片"
    prefixes = ("/draw", "/画图")
    prefix = next(
        (candidate for candidate in prefixes if lowered.startswith(candidate)), None
    )
    if prefix is None:
        natural = _NATURAL_GENERATE_IMAGE.fullmatch(raw)
        if natural is None:
            natural = _NATURAL_DRAW.fullmatch(raw)
        if natural is None:
            return None
        prompt = " ".join(natural.group(1).split())
        if not prompt:
            raise DrawRequestError("请补充画面描述。")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise DrawRequestError(f"画面描述最多 {MAX_PROMPT_CHARS} 个字符。")
        if any(ord(char) < 32 for char in prompt):
            raise DrawRequestError("画面描述包含不支持的控制字符。")
        return prompt
    if len(raw) > len(prefix) and not raw[len(prefix)].isspace():
        return None
    prompt = " ".join(raw[len(prefix) :].split())
    if not prompt:
        raise DrawRequestError("请在 /draw 后补充画面描述。")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise DrawRequestError(f"画面描述最多 {MAX_PROMPT_CHARS} 个字符。")
    if any(ord(char) < 32 for char in prompt):
        raise DrawRequestError("画面描述包含不支持的控制字符。")
    return prompt


class DrawRateLimiter:
    """Per-user monotonic cooldown with no persistence or private payload storage."""

    def __init__(
        self, cooldown_seconds: int = 75, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.cooldown_seconds = max(1, int(cooldown_seconds))
        self._clock = clock
        self._next_allowed: dict[str, float] = {}

    def try_acquire(self, sender_id: object) -> int:
        identity = str(sender_id or "").strip()
        if not identity:
            return self.cooldown_seconds
        now = self._clock()
        allowed_at = self._next_allowed.get(identity, 0.0)
        if allowed_at > now:
            return math.ceil(allowed_at - now)
        self._next_allowed[identity] = now + self.cooldown_seconds
        return 0
