"""X/Pro video command — Veo 3.1 Lite + smart video search.

X/Pro users: /video <prompt> or natural language.
   Duration <=4s or omitted -> generate with Veo. Duration >4s -> search web.
"/findvideo" or "找视频" -> force search mode (all tiers).
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import re
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

try:
    from xiaoning_runtime import (
        ArtifactDeliveryResult,
        deliver_local_artifact,
        mirror_runtime_task_status,
    )
except ImportError:
    from data.plugins.xiaoning_runtime import (
        ArtifactDeliveryResult,
        deliver_local_artifact,
        mirror_runtime_task_status,
    )

VIDEO_PROXY_URL = "http://127.0.0.1:3000/v1/videos/generations"
SEARCH_PROXY_URL = "http://127.0.0.1:3000/v1/chat/completions"
MAX_VIDEO_BYTES = 50 * 1024 * 1024
VIDEO_DAILY = 2
VIDEO_COOLDOWN = 180
VIDEO_MAX_GEN_SECS = 4
VIDEO_MODEL = "veo-3.1-generate-001"
VIDEO_LIMIT_MSG = "视频生成次数已用完（今日 {used}/{limit}）。明天自动重置。"
COOLDOWN_MSG = "视频生成冷却中，{retry} 秒后再试。"
GENERATING_MSG = "视频生成中… Veo 预计 3–8 分钟；QQ 视频文件成功交付后才会标记完成。"
SEARCHING_MSG = "正在搜索 B 站和抖音公开视频，预计 5–15 秒…"

# Duration: "5s", "10秒", "1分钟", "30 sec", "2min"
_DURATION_RE = re.compile(
    r"(\d+)\s*(?:秒|[sS](?:ec(?:ond)?s?)?\b|分(?:钟)?|min(?:ute)?s?)",
    re.I,
)
# AI video intent: explicitly asks for AI generation or gives short duration
_AI_VIDEO_RE = re.compile(
    r"(?:ai|AI|人工智能|自动).{0,4}(?:生成|做|制作|画|创建).{0,6}(?:视频|短片)",
    re.I,
)
# Natural-language ownership is intentionally deterministic.  "生成/创建/画"
# means a short Veo clip; "做/制作" is left to video_agent for a full edit.
_AI_GENERATION_VERB_RE = re.compile(r"(?:生成|创建|画).{0,12}(?:视频|短片|动画|sp|vid|video)", re.I)
_EXPLICIT_VEO_COMMANDS = ("/video", "/生成视频", "/vid", "/生成sp", "/视频")
_FULL_PRODUCTION_REQUEST_RE = re.compile(
    r"^(?:小柠[，,\s]*)?(?:帮我|请|给我|帮忙|来|想|想要?|要)?"
    r"(?:做|制作|弄|搞|整).{0,80}(?:视频|短片)"
    r"|^(?:小柠[，,\s]*)?(?:帮我|请|给我|帮忙|来|想|想要?|要)?"
    r"(?:做|制作|弄|搞|整)(?:一段|一个|个|段|一下|下)?(?:视频|短片).*$",
    re.I,
)


def _is_seconds_unit(unit_str: str) -> bool:
    """Check if duration unit is seconds (not minutes/hours)."""
    u = unit_str.lower()
    if "分" in u or "min" in u or "时" in u or "h" in u:
        return False
    return "秒" in u or "s" in u
_SEARCH_KEYWORDS = ("帮我找", "搜索", "寻找", "查找", "找", "搜")
_SEARCH_INTENT_RE = re.compile(
    r"^\s*(?:小柠[，,\s]*)?(?:(?:帮我|请|麻烦你|我想|想要)\s*)?"
    r"(?:找|搜|搜索|寻找|查找)(?:一下)?\s*(?:视频|短片|小视频|动画|影片)?",
    re.I,
)
_VIDEO_CAPABILITY_QUERY_RE = re.compile(
    r"^(?:小柠[，,\s]*)?(?:你\s*)?(?:能不能|可不可以|能|会|可以|支持|有没有|是否|怎么|为什么)"
    r".*(?:视频|短片|小视频|动画|影片|sp|vid|video)",
    re.I,
)

_NATURAL_VIDEO = re.compile(
    r"(?:小柠[，,\s]*)?(?:帮我|请|给我|帮忙|来|整|想|想要?|要)?"
    r"(?:生成|创建|画|做|制作|弄|搞)"
    r"(?:一段|一个|个|段|一下|下)?"
    r"(?:的)?"
    r"(?:视频|短片|小视频|动画|影片|sp|vid|video)"
    r"[\s，,：:]*(.*)$",
    re.I,
)
# Catch "desire" patterns without creation verb: "我想要一个猫视频", "有没有猫视频", "看看搞笑视频 猫咪"
_DESIRE_VIDEO = re.compile(
    r"(?:小柠[，,\s]*)?(?:我想要|我想看|给我|来|有没有|有|看看|想看|要)"
    r"\s*"
    r"(.*?)"  # topic/description — everything before 视频/短片/etc
    r"(?:视频|短片|小视频|动画|影片)"
    r"[吗呢吧啊]?\s*"
    r"(.*?)\s*$",  # optional extra content after 视频 (e.g. "看看搞笑视频 猫咪" → extra="猫咪")
    re.I,
)
# "我要看视频 海边", "来一段视频 猫咪" — 视频 in middle, topic after
_DESIRE_VIDEO_MID = re.compile(
    r"(?:小柠[，,\s]*)?(?:我想要看?|我想看|我要看|给我看看?|来看看?|看看|看|要|来|有没有)"
    r"\s*(?:一个|一段|个|段)?\s*"
    r"(?:好看的?|有趣的?|搞笑的?|可爱的?|酷的?)?\s*"
    r"(?:视频|短片|小视频|动画|影片)"
    r"[\s，,：:]+"
    r"(.+?)\s*$",
    re.I,
)
# Catch capability questions: "你能做视频吗", "可以做视频吗xxx", "会不会做视频 风景"
_CAN_VIDEO = re.compile(
    r"(?:小柠[，,\s]*)?(?:你\s*)?(?:能|可以|会|能不能|可不可以|会不会)"
    r"\s*(?:帮我|给我|帮忙)?\s*"
    r"(?:做|制作|生成|搞|弄|整)?\s*"
    r"(?:一个|一段|个)?\s*"
    r"(?:视频|短片|小视频|动画|影片)"
    r"[吗呢吧啊]?\s*"
    r"(.*?)\s*$",
    re.I,
)
# Catch bare "来个视频 xxx" or "整一个视频xxx"
_BARE_VIDEO = re.compile(
    r"(?:小柠[，,\s]*)?(?:来个?|整一个?|搞一个?)\s*"
    r"(?:视频|短片)\s*[\s，,：:]*(.+?)\s*$",
    re.I,
)
_VIDEO_STATEMENT_TAIL = re.compile(
    r"^(?:很难|不容易|很麻烦|不太靠谱)(?:[。.!！?？吧呢啊]*)$",
    re.I,
)

_SPLIT_VIDEO = re.compile(
    r"(?:小柠[，,\s]*)?(?:帮我|请|给我|帮忙|来|整|想|想要?|要)?"
    r"(?:生成|创建|画|做|制作|弄|搞)"
    r"(?:一段|一个|个|段|一下|下)?"
    r"(.+?)"
    r"(?:的)?"
    r"(?:视频|短片|小视频|动画|影片|sp|vid|video)"
    r"[\s，,：:。.!！?？]*$",
    re.I,
)

_VIDEO_ASPECT_FLAGS = {
    "--9:16": "9:16", "--vertical": "9:16", "--portrait": "9:16",
    "--16:9": "16:9", "--landscape": "16:9",
    "--1:1": "1:1", "--square": "1:1",
}


def _parse_video_options(prompt: str) -> tuple[str, str]:
    """Extract (clean_prompt, aspect_ratio) from video prompt."""
    aspect = "16:9"
    lowered = prompt.lower()
    for flag, ratio in _VIDEO_ASPECT_FLAGS.items():
        if lowered.endswith(flag):
            prompt = prompt[: -len(flag)].strip()
            aspect = ratio
            break
    return prompt, aspect


def _parse_duration(text: str) -> int | None:
    """Extract duration in seconds from prompt, or None."""
    m = _DURATION_RE.search(text)
    if not m:
        return None
    n = int(m.group(1))
    raw = m.group(0).lower()
    if any(u in raw for u in ("分", "min")):
        return n * 60
    if n > 120:
        return None  # unreasonable as seconds
    return n


def _has_search_intent(text: str) -> bool:
    t = str(text or "").lower()
    # /findvideo command
    if t.startswith(("/findvideo", "/findvid", "/搜视频", "/找视频")):
        return True
    return bool(_SEARCH_INTENT_RE.match(t))


def _strip_duration(text: str) -> str:
    """Remove duration specifier from prompt text."""
    return _DURATION_RE.sub("", text).strip()


def _is_search_mode(source_text: str, prompt: str) -> bool:
    """Search existing public videos when explicitly requested or too long for Veo.

    The generation endpoint is limited to a four-second clip for the X tier.
    Silently rendering a shorter clip than the user requested is misleading, so
    a longer duration consistently takes the public-video search route.
    """
    duration = _parse_duration(prompt)
    return _has_search_intent(source_text) or (
        duration is not None and duration > VIDEO_MAX_GEN_SECS
    )


def _classify_video_intent(text: str, prompt: str) -> str:
    """Use Gemini Flash to classify ambiguous video intent.
    Returns: 'generate' | 'search' | 'agent' | 'chat'
    Only called when regex routing is uncertain — adds ~1s latency.
    """
    classifier_prompt = (
        "你是一个意图分类器。用户提到了视频相关的内容，判断ta的真实意图：\n"
        "- generate: 用户想用AI生成一段原创视频（比如\"生成8s的猫视频\"、\"帮我做一段5秒的动画\"）\n"
        "- search: 用户想搜索/查找现有的公开视频（比如\"找个猫视频\"、\"有没有搞笑的视频\"）\n"
        "- agent: 用户想制作完整的短视频（有主题有脚本，比如\"做个关于咖啡的视频\"、\"/做视频 如何做咖啡\"）\n"
        "- chat: 用户只是在聊视频相关的话题，不是在请求（比如\"这个视频不错\"、\"视频很难做\"）\n\n"
        f"用户消息：{text[:300]}\n\n"
        "只返回一个单词：generate / search / agent / chat"
    )
    try:
        resp = requests.post(
            SEARCH_PROXY_URL,
            json={
                "model": "gemini-3.6-flash",
                "messages": [{"role": "user", "content": classifier_prompt}],
                "max_tokens": 10,
            },
            timeout=(3, 5),
        )
        raw = resp.json()["choices"][0]["message"]["content"].strip().lower()
        for intent in ("generate", "search", "agent", "chat"):
            if intent in raw:
                return intent
    except Exception:
        pass
    return ""  # uncertain → fall through to default logic


def _clean_natural_search_query(text: str) -> str:
    value = re.sub(r"^(?:一个|一段|一部|一条|个|段)\s*", "", text.strip())
    return re.sub(r"\s*(?:的)?(?:视频|短片|小视频|动画|影片)\s*$", "", value).strip()


def _clean_desire_topic(topic: str) -> str:
    """Strip leading quantifiers/adjectives from desire-pattern captures."""
    t = str(topic or "").strip()
    # Remove leading quantifiers: "一个", "一段", "个", etc.
    t = re.sub(r"^(?:一个|一段|一部|一支|一块|个|段|部|支)\s*", "", t)
    # Remove leading filler adjectives with optional 的
    t = re.sub(r"^(?:好看的?|有趣的?|搞笑的?|可爱的?|酷的?|漂亮的?)(?:的)?\s*", "", t)
    return t.strip()


def _parse_video_command(text: str) -> str | None:
    raw = str(text or "").strip()
    lowered = raw.lower()
    if _VIDEO_CAPABILITY_QUERY_RE.match(raw):
        return None
    if lowered.startswith(("/做视频", "/制作视频", "/视频制作")):
        return None

    for prefix in ("/video", "/生成视频", "/vid", "/生成sp", "/视频",
                   "/findvideo", "/findvid", "/搜视频", "/找视频"):
        if lowered.startswith(prefix):
            parts = raw.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                return ""
            if len(parts[1]) > 800:
                return None
            return parts[1].strip()

    ai_prefix = re.match(
        r"^(?:小柠[，,\s]*)?(?:请|帮我|给我)?用\s*(?:ai|人工智能)\s*",
        raw,
        re.I,
    )
    if ai_prefix is None and _FULL_PRODUCTION_REQUEST_RE.match(raw):
        return None
    natural_raw = raw[ai_prefix.end():].strip() if ai_prefix else raw

    match = _SPLIT_VIDEO.match(natural_raw)
    if match:
        prompt = match.group(1).strip()
        if prompt in {"一个", "一段", "个", "段", "一块", "一部", "一支"}:
            prompt = ""
        return prompt if len(prompt) <= 800 else None

    match = _NATURAL_VIDEO.match(natural_raw)
    if match:
        prompt = (match.group(1) or "").strip()
        if _VIDEO_STATEMENT_TAIL.match(prompt):
            return None
        return prompt if len(prompt) <= 800 else None

    match = _DESIRE_VIDEO.match(raw)
    if match:
        prompt = _clean_desire_topic(f"{match.group(1) or ''} {match.group(2) or ''}".strip())
        return prompt if len(prompt) <= 800 else None

    match = _DESIRE_VIDEO_MID.match(raw) or _BARE_VIDEO.match(raw)
    if match:
        prompt = _clean_desire_topic((match.group(1) or "").strip())
        return prompt if len(prompt) <= 800 else None

    if _has_search_intent(raw):
        prompt = _clean_natural_search_query(_SEARCH_INTENT_RE.sub("", raw, count=1).strip())
        return prompt if len(prompt) <= 800 else None

    return None


class VideoCommand(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}
        self._generation_lock = asyncio.Lock()
        self._cooldowns: dict[str, float] = {}
        data_dir = Path(StarTools.get_data_dir("video_command"))
        data_dir.mkdir(parents=True, exist_ok=True)
        self._usage_file = data_dir / "usage.json"
        self._daily_usage = self._load_usage()
        project_root = Path(__file__).resolve().parents[4]
        self._output_root = project_root / "claude_workspace" / "pro_video"

    def _pro_db_path(self) -> Path:
        return (
            Path(__file__).resolve().parents[2]
            / "plugin_data" / "xiaoning_pro" / "pro_members.db"
        )

    def _load_usage(self) -> dict[str, int]:
        try:
            raw = json.loads(self._usage_file.read_text(encoding="utf-8"))
            today = time.strftime("%Y%m%d")
            return {
                str(key): int(value)
                for key, value in raw.items()
                if str(key).endswith(f":{today}") and int(value) >= 0
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}

    def _save_usage(self) -> None:
        temporary = self._usage_file.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self._daily_usage, ensure_ascii=False), encoding="utf-8"
        )
        temporary.replace(self._usage_file)

    @staticmethod
    def _sender(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_sender_id", None)
        return str(getter() if callable(getter) else "").strip()

    def _request_video(self, prompt: str, model: str = "", duration: int = 4, aspect: str = "16:9") -> tuple[bytes, str, str]:
        body = {"prompt": prompt, "duration": duration, "aspect_ratio": aspect}
        if model:
            body["model"] = model
        response = requests.post(
            VIDEO_PROXY_URL,
            json=body,
            timeout=(30, 600),
        )
        response.raise_for_status()
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or not data:
            raise ValueError("missing video response")
        encoded = data[0].get("b64_json")
        mime = data[0].get("mime_type", "video/mp4")
        if not isinstance(encoded, str):
            raise ValueError("missing video response")
        payload = base64.b64decode(encoded, validate=True)
        if not payload or len(payload) > MAX_VIDEO_BYTES:
            raise ValueError("invalid video size")
        ext = ".gif" if "gif" in mime else ".mp4"
        return payload, mime, ext

    def _search_videos(self, query: str) -> tuple[str, list[str]]:
        """Return public Bilibili and Douyin pages; downloads remain best effort."""
        bili_text, bili_urls = self._search_bilibili(query)
        if not bili_urls:
            bili_text, bili_urls = self._search_bilibili_all(query)
        douyin_text, douyin_urls = self._search_douyin(query)
        text = "\n".join(part for part in (bili_text, douyin_text) if part)
        return text, bili_urls + douyin_urls

    @staticmethod
    def _search_douyin(query: str) -> tuple[str, list[str]]:
        """Use existing Google-grounded proxy; no unofficial Douyin scraper."""
        try:
            response = requests.post(
                SEARCH_PROXY_URL,
                json={
                    "model": "gemini-3.6-flash-search",
                    "google_search": True,
                    "max_tokens": 400,
                    "messages": [{
                        "role": "user",
                        "content": f"只找抖音公开视频：{query}。返回不超过 3 个 douyin.com 视频页面链接。",
                    }],
                },
                timeout=(10, 45),
            )
            response.raise_for_status()
            sources = response.json().get("grounding", {}).get("sources", [])
            results = []
            for source in sources if isinstance(sources, list) else []:
                url = str(source.get("uri") or "") if isinstance(source, dict) else ""
                host = (urlparse(url).hostname or "").lower()
                if host == "douyin.com" or host.endswith(".douyin.com") or host.endswith("iesdouyin.com"):
                    results.append((str(source.get("title") or "抖音视频"), url))
                if len(results) == 3:
                    break
            if results:
                return "抖音：\n" + "\n".join(
                    f"{index}. {title} - {url}" for index, (title, url) in enumerate(results, 1)
                ), [url for _, url in results]
        except (requests.RequestException, ValueError, TypeError, KeyError, json.JSONDecodeError):
            pass
        fallback = f"https://www.douyin.com/search/{quote(query)}"
        return f"抖音搜索：{fallback}", []

    @staticmethod
    def _search_bilibili_all(query: str) -> tuple[str, list[str]]:
        """Use Bilibili's all-search endpoint when the video endpoint is rate-limited."""
        try:
            response = requests.get(
                "https://api.bilibili.com/x/web-interface/search/all/v2",
                params={"keyword": query, "page": 1},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=(10, 30),
            )
            response.raise_for_status()
            body = response.json()
            if body.get("code") != 0:
                raise ValueError("Bilibili all-search rejected request")
            videos = next(
                (
                    item.get("data", [])
                    for item in body.get("data", {}).get("result", [])
                    if item.get("result_type") == "video"
                ),
                [],
            )
            results: list[tuple[str, str]] = []
            for item in videos:
                bvid = str(item.get("bvid", ""))
                if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid):
                    continue
                title = re.sub(r"<[^>]+>", "", str(item.get("title", ""))).strip()
                results.append((title or bvid, f"https://www.bilibili.com/video/{bvid}"))
                if len(results) == 5:
                    break
            if results:
                lines = [f"{index}. {title} - {url}" for index, (title, url) in enumerate(results, 1)]
                return "\n".join(lines), [url for _, url in results]
        except (requests.RequestException, ValueError, TypeError, KeyError):
            pass
        return f"没有找到与「{query}」相关的 B 站视频，换个关键词试试。", []

    @staticmethod
    def _search_bilibili(query: str) -> tuple[str, list[str]]:
        """Search Bilibili's public video index and return canonical BV links."""
        try:
            response = requests.get(
                "https://api.bilibili.com/x/web-interface/search/type",
                params={"search_type": "video", "keyword": query, "page": 1},
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://search.bilibili.com/",
                    "Accept": "application/json, text/plain, */*",
                },
                timeout=(5, 15),
            )
            response.raise_for_status()
            items = response.json().get("data", {}).get("result", [])
            lines: list[str] = []
            urls: list[str] = []
            for item in items[:5]:
                if not isinstance(item, dict):
                    continue
                bvid = str(item.get("bvid") or "").strip()
                if not bvid:
                    continue
                title = re.sub(r"<[^>]+>", "", html.unescape(str(item.get("title") or ""))).strip()
                url = f"https://www.bilibili.com/video/{bvid}"
                lines.append(f"{title or bvid} - {url}")
                urls.append(url)
            if lines:
                return "\n".join(lines), urls
        except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError):
            pass
        return f"没有找到与「{query}」相关的视频，换个关键词试试。", []

    def _save_video(self, payload: bytes, ext: str) -> Path:
        self._output_root.mkdir(parents=True, exist_ok=True)
        target = self._output_root / f"video-{uuid.uuid4().hex}{ext}"
        target.write_bytes(payload)
        return target

    async def _deliver_video(
        self,
        event: AstrMessageEvent,
        path: Path,
        *,
        kind: str = "file",
        task_id: str = "",
        task_desc: str = "",
    ) -> ArtifactDeliveryResult:
        return await deliver_local_artifact(
            event,
            path,
            allowed_roots=[self._output_root],
            kind=kind,
            task_id=task_id,
            task_desc=task_desc,
            task_owner="video" if task_id else "",
        )

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=935)
    async def on_message(self, event: AstrMessageEvent):
        text = str(getattr(event, "get_message_str", lambda: "")() or "")
        prompt = _parse_video_command(text)
        if prompt is None or not (event.is_private_chat() or event.is_at_or_wake_command):
            return
        sender_id = self._sender(event)
        if not sender_id.isdigit():
            logger.warning("[VideoCmd] skip response without numeric sender")
            return

        # Show help for empty prompt
        if prompt == "":
            yield event.plain_result(
                f"🎬 AI 视频生成（/video）\n"
                f"生成原创 AI 视频：/video <画面描述>\n"
                f"搜索公开视频：/findvideo <关键词>\n"
                f"制作完整短片（脚本+配音+素材）：/做视频 <主题>\n"
                f"示例：/video 一只猫在草地上跑\n"
                f"示例：/做视频 如何在家做拿铁咖啡"
            )
            event.stop_event()
            return

        # ---- route: AI video (short/explicit) vs video agent (creation verb) ----
        search_mode = _is_search_mode(text, prompt)
        base_prompt, aspect = _parse_video_options(prompt)
        clean_prompt = _strip_duration(base_prompt) or base_prompt

        # Deterministic wording owns the common cases. Gemini Flash is reserved
        # for the genuinely ambiguous residue below.
        dur_match = _DURATION_RE.search(base_prompt)
        has_seconds = dur_match and _is_seconds_unit(dur_match.group(0))
        dur_val = int(dur_match.group(1)) if has_seconds else None
        is_ai_intent = bool(_AI_VIDEO_RE.search(text))
        explicit_veo_command = text.strip().lower().startswith(_EXPLICIT_VEO_COMMANDS)
        natural_ai_generation = bool(_AI_GENERATION_VERB_RE.search(text))
        use_ai_video = (
            is_ai_intent
            or explicit_veo_command
            or natural_ai_generation
            or (dur_val is not None and dur_val <= VIDEO_MAX_GEN_SECS)
        )
        # Only route to video_agent when the request uses a full-production
        # verb such as "做/制作".  "生成一只猫的视频" must never disappear
        # into Agent and become a generic text reply.
        has_creation_verb = bool(_NATURAL_VIDEO.match(text) or _SPLIT_VIDEO.match(text))

        if not search_mode and not use_ai_video and not _has_search_intent(text):
            if has_creation_verb:
                # Check if video_agent can actually handle this
                try:
                    from video_agent.main import _parse_agent_command
                except ImportError:
                    from data.plugins.video_agent.main import _parse_agent_command
                agent_topic = _parse_agent_command(text)
                if agent_topic is not None:
                    logger.info("[VideoCmd] ROUTE to agent: dur=%s ai=%s chars=%d",
                                 dur_val, is_ai_intent, len(text))
                    return
                # Agent can't handle → use LLM to decide: generate or search?
                llm_intent = await asyncio.to_thread(_classify_video_intent, text, prompt)
                if llm_intent == "generate":
                    use_ai_video = True
                    logger.info("[VideoCmd] LLM classified as generate: chars=%d", len(text))
                elif llm_intent == "search":
                    search_mode = True
                    logger.info("[VideoCmd] LLM classified as search: chars=%d", len(text))
                elif llm_intent == "chat":
                    logger.info("[VideoCmd] LLM classified as chat, passing: chars=%d", len(text))
                    return  # let normal chat handle it
                else:
                    # LLM uncertain → default to search
                    search_mode = True
                    logger.info("[VideoCmd] LLM uncertain, fallback search: chars=%d", len(text))
            else:
                # Desire/capability/bare patterns → use LLM to decide
                llm_intent = await asyncio.to_thread(_classify_video_intent, text, prompt)
                if llm_intent == "generate":
                    use_ai_video = True
                elif llm_intent == "chat":
                    return
                else:
                    search_mode = True

        if search_mode:
            aspect = "16:9"
            yield event.plain_result(SEARCHING_MSG)
            try:
                text, _ = await asyncio.to_thread(self._search_videos, clean_prompt)
            except Exception as exc:
                logger.warning("[VideoCmd] search failed: %s", type(exc).__name__)
                yield event.plain_result("视频搜索暂时失败，请稍后再试或换个关键词。")
                event.stop_event()
                return
            # Search pages are links, not reliable media files. Downloading them
            # here caused long Bilibili 412 retries and prevented a QQ reply.
            yield event.plain_result(
                f"搜索「{clean_prompt}」的结果：\n\n{text or '暂未找到公开结果，换个关键词试试。'}"
            )

            event.stop_event()
            return

        # ── Unified video gen for all users ──────────────────────
        today = time.strftime("%Y%m%d")
        dk = f"{sender_id}:{today}"
        used = self._daily_usage.get(dk, 0)
        if used >= VIDEO_DAILY:
            yield event.plain_result(VIDEO_LIMIT_MSG.format(used=used, limit=VIDEO_DAILY))
            event.stop_event()
            return

        now = time.monotonic()
        cooldown_until = self._cooldowns.get(sender_id, 0)
        if now < cooldown_until:
            yield event.plain_result(COOLDOWN_MSG.format(retry=int(cooldown_until - now)))
            event.stop_event()
            return

        if self._generation_lock.locked():
            yield event.plain_result("正在生成一个视频，等这个完成后再试。")
            event.stop_event()
            return

        self._cooldowns[sender_id] = now + VIDEO_COOLDOWN
        task_id = uuid.uuid4().hex[:12]
        task_desc = f"生成视频：{clean_prompt[:140]}"
        await mirror_runtime_task_status(
            sender_id, task_id, task_desc, "in_progress", "veo_started", owner="video"
        )
        yield event.plain_result(GENERATING_MSG)

        try:
            async with self._generation_lock:
                model = VIDEO_MODEL
                duration = 4
                payload, mime, ext = await asyncio.to_thread(
                    self._request_video, clean_prompt, model, duration, aspect
                )
                output_path = self._save_video(payload, ext)
        except Exception as exc:
            logger.warning("[VideoCmd] generation failed: %s", type(exc).__name__)
            self._cooldowns.pop(sender_id, None)
            await mirror_runtime_task_status(
                sender_id, task_id, task_desc, "failed", type(exc).__name__, owner="video"
            )
            yield event.plain_result(
                f"Veo AI 视频生成失败（{type(exc).__name__}），请稍后再试。\n"
                f"💡 可以试试免费的 /做视频 {clean_prompt[:30]} — 素材拼接+配音合成，同样出片。"
            )
            event.stop_event()
            return

        usage_key = dk
        self._daily_usage[usage_key] = used + 1
        try:
            self._save_usage()
        except OSError:
            logger.warning("[VideoCmd] usage persistence failed")
        delivery = await self._deliver_video(
            event,
            output_path,
            kind="image" if ext == ".gif" else "file",
            task_id=task_id,
            task_desc=task_desc,
        )
        if delivery.delivered:
            await mirror_runtime_task_status(
                sender_id, task_id, task_desc, "done", f"qq:{delivery.channel}", owner="video"
            )
            event.set_extra("_pro_video_output_paths", [str(output_path)])
            suffix = {
                "group_upload": "已上传到群文件",
                "private_fallback": "群文件上传失败，已私聊发送给你",
                "group_component": "群文件和私聊投递失败，已改为在群内发送",
                "private": "已发送到当前私聊",
                "private_component": "已发送到当前私聊",
            }.get(delivery.channel, "已发送")
            yield event.plain_result(f"视频已生成，{suffix}：{output_path.name}")
        else:
            await mirror_runtime_task_status(
                sender_id, task_id, task_desc, "delivery_pending", delivery.channel, owner="video"
            )
            retry_note = (
                "已加入后台重试队列，稍后自动送达。"
                if delivery.channel == "queued"
                else "文件已安全保留，请稍后重试。"
            )
            yield event.plain_result(
                f"视频已生成，但 QQ 文件尚未交付，任务未完成；{retry_note}"
            )
        event.stop_event()

    @filter.after_message_sent(priority=-1000)
    async def cleanup_sent_videos(self, event: AstrMessageEvent) -> None:
        paths = event.get_extra("_pro_video_output_paths", []) or []
        event.set_extra("_pro_video_output_paths", [])
        async def _delayed_cleanup():
            await asyncio.sleep(60)
            root = self._output_root.resolve(strict=False)
            for raw_path in paths:
                candidate = Path(str(raw_path or ""))
                if candidate.is_symlink():
                    continue
                try:
                    resolved = candidate.resolve(strict=True)
                except OSError:
                    continue
                if root not in resolved.parents or resolved.suffix.lower() not in {
                    ".mp4", ".gif", ".webm", ".mkv", ".mov",
                }:
                    continue
                try:
                    resolved.unlink()
                except OSError:
                    continue
        asyncio.ensure_future(_delayed_cleanup())
        try:
            now = time.time()
            root = self._output_root.resolve(strict=False)
            if root.is_dir():
                candidates = [
                    f
                    for suffix in (".mp4", ".gif", ".webm", ".mkv", ".mov")
                    for f in root.glob(f"video-*{suffix}")
                ]
                for f in candidates:
                    try:
                        if now - f.stat().st_mtime > 900:
                            f.unlink()
                    except OSError:
                        continue
        except Exception:
            pass
