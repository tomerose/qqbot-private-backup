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
    r"(?:画|绘制|作图|做|制作)(?:一张|一个|张|个)?(?:图片|图|海报|插画|封面)?"
    r"|生成(?:一张|一个)(?:图片|图|海报|插画|封面))"
    r"[：:，,\s]*(.+)$",
    re.I,
)
_NATURAL_GENERATE_IMAGE = re.compile(
    r"^(?:小柠[，,\s]*)?(?:帮我|请)?(?:生成|做|制作)(?:一张|一个)"
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


_ASPECT_FLAGS = {
    "--1:1": "1:1", "--square": "1:1",
    "--9:16": "9:16", "--vertical": "9:16", "--portrait": "9:16",
    "--16:9": "16:9", "--horizontal": "16:9", "--landscape": "16:9",
    "--2:3": "2:3", "--3:2": "3:2",
}
_MULTI_RE = re.compile(r"\s+--(\d)\s*$", re.I)


def parse_draw_options(prompt: str) -> tuple[str, str, int]:
    """Extract (clean_prompt, aspect_ratio, n_images) from prompt text."""
    aspect = "1:1"
    for flag, ratio in _ASPECT_FLAGS.items():
        if prompt.endswith(" " + flag):
            prompt = prompt[: -len(flag)].strip()
            aspect = ratio
            break
    n = 1
    m = _MULTI_RE.search(prompt)
    if m:
        n = min(max(int(m.group(1)), 1), 4)
        prompt = prompt[: m.start()].strip()
    return prompt, aspect, n


_VIDEO_KEYWORD_CHECK = re.compile(r"视频|短片|小视频|动画|影片|sp\b|vid\b|video\b", re.I)

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
        # ── 防误判：用户说的是视频，不是画图 ──
        if _VIDEO_KEYWORD_CHECK.search(raw):
            return None
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


# ── Image editing natural language detection ──────────────────────

_DEWATERMARK_KEYWORDS = ("去水印", "去掉水印", "消除水印", "去除水印", "移除水印", "删水印", "去字幕", "消除字幕")
_DEWATERMARK_PROMPT = (
    "Remove ALL watermarks, logos, text overlays, subtitles, and timestamp stamps "
    "from this image. Keep the original image content, colors, and composition unchanged. "
    "Do NOT modify anything else — only remove the overlay elements."
)
_NATURAL_DEWATERMARK = re.compile(
    r"(?:去|去掉|消除|清除|移除|删(?:掉)?|抹掉|擦掉).{0,24}"
    r"(?:水印|logo|标志|署名|签名|字样|文字|小尾巴|字幕)"
    r"|(?:右下角|左下角|角落).{0,24}(?:@|画师|署名|水印|logo)",
    re.I,
)

_EDIT_PREFIXES = ("/edit", "/编辑图片", "/改图", "/去水印", "/dewatermark")
_NATURAL_EDIT = re.compile(
    r"^(?:小柠[，,\s]*)?(?:帮我|请|给我|帮忙|来)?"
    r"(?:把|将|给)"
    r"(?:这张|这个|那张|那个|这张图|这个图|这张图片|这张照片|这个照片|它)"
    r"(?:改[成为]?|变成?|换成?|转[换为]?[成为]?|p成|修[改为]?成)"
    r"(.+)$",
    re.I,
)


def parse_edit_command(text: object) -> str | None:
    """Detect image editing intent from natural language or /edit command."""
    raw = str(text or "").strip()
    lowered = raw.lower()
    for prefix in _EDIT_PREFIXES:
        if lowered.startswith(prefix):
            prompt = raw[len(prefix):].strip()
            return prompt if prompt else None
    m = _NATURAL_EDIT.match(raw)
    if m:
        prompt = m.group(1).strip()
        if prompt and len(prompt) <= MAX_PROMPT_CHARS:
            return prompt
    return None


def is_dewatermark_request(text: object) -> bool:
    """Recognize direct and natural-language requests to remove an image overlay."""
    normalized = " ".join(str(text or "").lower().split())
    return bool(normalized) and (
        any(keyword in normalized for keyword in _DEWATERMARK_KEYWORDS)
        or bool(_NATURAL_DEWATERMARK.search(normalized))
    )


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
