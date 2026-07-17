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
# P1: 4K flag — PRO exclusive, Gemini 3 Pro Image 4096x4096
_4K_FLAG = "--4k"
# P2: style presets — inject professional prompt prefixes
_STYLE_FLAGS = {
    "--style photo": "professional photograph, photorealistic, natural lighting, shallow depth of field, 8K detail — ",
    "--style anime": "anime/manga illustration, vibrant colors, cel-shaded, studio quality — ",
    "--style product": "product photography, studio lighting, clean white background, commercial quality — ",
    "--style illustration": "digital illustration, detailed linework, concept art style — ",
    "--style cinematic": "cinematic shot, dramatic lighting, film grain, anamorphic lens — ",
    "--style watercolor": "watercolor painting, soft edges, artistic, flowing colors — ",
    "--style oil": "oil painting, rich textures, classical composition, museum quality — ",
}


def parse_draw_options(prompt: str) -> tuple[str, str, int, bool, str]:
    """Extract (clean_prompt, aspect_ratio, n_images, is_4k, style_prefix) from prompt text."""
    # ── Style preset ──
    style_prefix = ""
    prompt_lower = prompt.lower()
    for flag, prefix in _STYLE_FLAGS.items():
        if flag in prompt_lower or prompt_lower.endswith(flag):
            style_prefix = prefix
            # Remove the flag from prompt (case-insensitive)
            idx = prompt_lower.rfind(flag)
            prompt = (prompt[:idx] + prompt[idx + len(flag):]).strip()
            break

    # ── Aspect ratio ──
    aspect = "1:1"
    for flag, ratio in _ASPECT_FLAGS.items():
        if prompt.endswith(" " + flag):
            prompt = prompt[: -len(flag)].strip()
            aspect = ratio
            break

    # ── 4K flag ──
    is_4k = False
    if prompt.lower().endswith(" " + _4K_FLAG):
        prompt = prompt[: -len(_4K_FLAG) - 1].strip()
        is_4k = True

    # ── Multi-image ──
    n = 1
    m = _MULTI_RE.search(prompt)
    if m:
        n = min(max(int(m.group(1)), 1), 4)
        prompt = prompt[: m.start()].strip()

    return prompt, aspect, n, is_4k, style_prefix


_VIDEO_KEYWORD_CHECK = re.compile(r"视频|短片|小视频|动画|影片|sp\b|vid\b|video\b", re.I)
# 防误判：用户要的是文档/文件，不是画图
_DOCUMENT_KEYWORD_CHECK = re.compile(
    r"(?:ppt|word|excel|pdf|docx?|xlsx?|pptx?|"
    r"文档|报告|表格|幻灯片|演示文稿|简历|总结|计划书|方案|笔记|讲义"
    r"|合同|申请书|策划|周报|月报|日报|纪要|论文|说明书|手册|课表|日程)",
    re.I,
)
# 防误判：用户要的是歌曲/音乐，不是画图（"做一首歌"→music_command）
_MUSIC_KEYWORD_CHECK = re.compile(
    r"(?:歌曲|音乐|歌\b|唱歌|演唱|写歌|作曲|歌词|伴奏|melody|"
    r"唱一首|唱首|唱个|写一首|写首|创作一首|做一首歌|生成一首歌)",
    re.I,
)

def parse_draw_command(text: object) -> str | None:
    raw = str(text or "").strip()
    lowered = raw.lower()
    if lowered in {"生成图片", "帮我生成图片", "请生成图片", "画图"}:
        return "一张适合分享的高质量图片"
    # Document requests are not drawing requests
    if _DOCUMENT_KEYWORD_CHECK.search(raw):
        return None
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
        # ── 防误判：用户要的是歌曲/音乐，不是画图 ──
        if _MUSIC_KEYWORD_CHECK.search(raw):
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

_DEWATERMARK_KEYWORDS = (
    "去水印", "去掉水印", "消除水印", "去除水印", "移除水印", "删水印",
    "去字幕", "消除字幕", "去logo", "去掉logo",
    # natural phrasing variants — "帮我把字弄掉" etc.
    "水印去掉", "水印弄掉", "水印p掉", "水印抹掉", "水印消除",
    "把字去掉", "把字弄掉", "把字抹掉", "把文字去掉", "把文字弄掉",
    "把水印去了", "把水印弄掉", "去掉字", "去掉文字", "去掉右下角",
    "抹掉水印", "抹掉字", "抹掉文字", "擦掉水印", "擦掉字",
    "p掉水印", "p掉字", "p掉文字",
)
_DEWATERMARK_PROMPT = (
    "Remove all non-scene overlays by filling only their small regions with the natural "
    "surrounding background. Remove every overlay character, @ handle, caption, logo, "
    "timestamp, and every isolated bright decorative dot near them, especially all marks "
    "in the bottom-right. No overlay text or isolated white dot may remain. Preserve the "
    "original scene, subject, colors, crop, composition, and all unrelated details. Add nothing."
)
_NATURAL_DEWATERMARK = re.compile(
    r"(?:去|去掉|消除|清除|移除|删(?:掉)?|抹掉|擦掉|p掉|弄掉|搞掉|去了).{0,24}"
    r"(?:水印|logo|标志|署名|签名|字样|文字|字迹|字体|字幕|小尾巴|右下角|左下角|角落)"
    r"|(?:右下角|左下角|角落|图上|图片上|照片上|画面).{0,24}"
    r"(?:@|画师|署名|水印|logo|字|文字|字样|东西|标记|标识|签名|小尾巴)"
    r"|(?:去除|移除|消除|清理|清除|擦除|抹除|删掉|去掉).{0,30}"
    r"(?:水印|logo|字|文字|字样|字幕|标记|标识|签名|署名)",
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
_NATURAL_REDRAW = re.compile(
    r"^(?:小柠[，,\s]*)?(?:帮我|请|给我|麻烦|能不能|可以)?"
    r"(?:把(?:这张|这个|那张|那个|这张图|这个图|这张图片|这张照片|它)?\s*)?"
    r"(?:重新画|重画|重新绘制|重绘)(?:一张|一个|一下)?(?:成|为)?"
    r"[：:，,\s]*(.*)$",
    re.I,
)
_DEFAULT_REDRAW_PROMPT = "忠实重绘参考图，保留主体和构图，线条干净，输出清晰完整的高质量图片"


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
    redraw = _NATURAL_REDRAW.match(raw)
    if redraw:
        prompt = redraw.group(1).strip()
        if not prompt:
            return _DEFAULT_REDRAW_PROMPT
        if len(prompt) <= MAX_PROMPT_CHARS:
            return f"以参考图为基础重新绘制：{prompt}"
    return None


# Broad catch-all: "remove/delete/wipe" any overlay/content from an image
_BROAD_REMOVE = re.compile(
    r"(?:去掉|弄掉|删掉|移除|消除|清除|抹掉|擦掉|p掉|搞掉|去了|"
    r"不要|不想看到|不需要|讨厌|帮忙去掉|帮我去掉|帮我弄掉|帮我删掉|"
    r"帮我把.{0,6}(?:去掉|弄掉|删掉|抹掉|擦掉|p掉))"
    r".{0,30}(?:图|照片|画面|右下角|左下角|角落|水印|字|logo|标记|名字|名称|东西|内容)",
    re.I,
)


def is_dewatermark_request(text: object) -> bool:
    """Recognize direct and natural-language requests to remove an image overlay."""
    normalized = " ".join(str(text or "").lower().split())
    if not normalized:
        return False
    if any(keyword in normalized for keyword in _DEWATERMARK_KEYWORDS):
        return True
    if _NATURAL_DEWATERMARK.search(normalized):
        return True
    if _BROAD_REMOVE.search(normalized):
        return True
    return False


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
