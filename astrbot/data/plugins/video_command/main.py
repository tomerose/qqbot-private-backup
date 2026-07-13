"""PRO-only video command — Veo 3.1 Lite + smart video search.

PRO users: /video <prompt> or natural language.
   Duration <=4s or omitted -> generate with Veo. Duration >4s -> search web.
"/findvideo" or "找视频" -> force search mode.
"""

from __future__ import annotations

import asyncio
import base64
import html
import io
import json
import re
import time
import uuid
from pathlib import Path

import requests
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Image, Plain
from astrbot.api.star import Context, Star, StarTools

from ..draw_command.pro_access import get_tier, Tier
from ..claude_code_agent.agent_core import upload_aiocqhttp_group_file

VIDEO_PROXY_URL = "http://127.0.0.1:3000/v1/videos/generations"
VIDEO_DOWNLOAD_URL = "http://127.0.0.1:3000/v1/videos/download"
MAX_VIDEO_BYTES = 50 * 1024 * 1024
VIDEO_PRO_DAILY = 3
VIDEO_COOLDOWN = 180
VIDEO_MAX_GEN_SECS = 4  # Veo Lite max
VIDEO_LIMIT_MSG = "视频生成次数已用完（今日 {used}/{limit}）。明天自动重置。"
PRO_VIDEO_MESSAGE = "视频生成是 Pro 专属功能。发送 /pro status 查看资格。"
COOLDOWN_MSG = "视频生成冷却中，{retry} 秒后再试。"
GENERATING_MSG = "视频生成中… Veo 3.1 Lite 通常需要 2-5 分钟，请耐心等待。"
SEARCHING_MSG = "搜索视频中…"

# Duration: "5s", "10秒", "1分钟", "30 sec", "2min"
_DURATION_RE = re.compile(
    r"(\d+)\s*(?:秒|[sS](?:ec(?:ond)?s?)?\b|分(?:钟)?|min(?:ute)?s?)",
    re.I,
)
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
    r"(?:生成|制作|做个?|创建|弄|搞|整|来)"
    r"(?:一段|一个|个|段)?"
    r"(?:视频|短片|小视频|动画|影片|sp\b|vid|video)"
    r"[\s，,：:]*(.*)$",
    re.I,
)
_NATURAL_REQUEST_START = re.compile(
    r"^\s*(?:\u5c0f\u67e0[\uff0c,,:：\s]*|(?:\u5e2e\u6211|\u8bf7|\u7ed9\u6211|\u5e2e\u5fd9)\s*)",
    re.I,
)

_SPLIT_VIDEO = re.compile(
    r"(?:小柠[，,\s]*)?(?:帮我|请|给我|帮忙|来|整|想|想要?|要)?"
    r"(?:生成|制作|做个?|创建|弄|搞|整|来|画)"
    r"(?:一段|一个|个|段)?"
    r"(.+?)"
    r"(?:视频|短片|小视频|动画|影片|sp|vid|video)"
    r"[\s，,：:。.!！?？]*$",
    re.I,
)

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
    duration = _parse_duration(prompt)
    return _has_search_intent(source_text) or (
        duration is not None and duration > VIDEO_MAX_GEN_SECS
    )


def _clean_natural_search_query(text: str) -> str:
    value = re.sub(r"^(?:一个|一段|一部|一条|个|段)\s*", "", text.strip())
    return re.sub(r"\s*(?:的)?(?:视频|短片|小视频|动画|影片)\s*$", "", value).strip()


def _parse_video_command(text: str) -> str | None:
    raw = str(text or "").strip()
    lowered = raw.lower()
    if _VIDEO_CAPABILITY_QUERY_RE.match(raw):
        return None

    # /command style
    for prefix in ("/video", "/生成视频", "/做视频", "/vid", "/生成sp", "/视频",
                   "/findvideo", "/findvid", "/搜视频", "/找视频"):
        if lowered.startswith(prefix):
            parts = raw.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                return ""
            if len(parts[1]) > 800:
                return None
            return parts[1].strip()

    # split pattern: verb ... content ... noun
    m = _SPLIT_VIDEO.match(raw)
    if m:
        prompt = m.group(1).strip()
        _qonly = {"一个", "一段", "个", "段", "一块", "一部", "一支"}
        if prompt in _qonly:
            prompt = ""
        if prompt and len(prompt) <= 800:
            return prompt
        if not prompt:
            return ""

    # natural language: verb + noun together
    m = _NATURAL_VIDEO.match(raw)
    if m:
        if not _NATURAL_REQUEST_START.match(raw):
            return None
        prompt = (m.group(1) or "").strip()
        if not prompt:
            return ""
        if len(prompt) > 800:
            return None
        return prompt

    # search-only: user said "找视频 xxx" without a generate verb
    if _has_search_intent(raw):
        rest = _SEARCH_INTENT_RE.sub("", raw, count=1).strip()
        rest = _clean_natural_search_query(rest)
        if rest and len(rest) <= 800:
            return rest
        return ""  # show help

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

    def _request_video(self, prompt: str) -> tuple[bytes, str, str]:
        response = requests.post(
            VIDEO_PROXY_URL,
            json={"prompt": prompt, "duration": 4, "aspect_ratio": "16:9"},
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
        """Prefer Bilibili so every returned page can use the native downloader."""
        text, urls = self._search_bilibili(query)
        if urls:
            return text, urls
        return self._search_bilibili_all(query)

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

    def _try_download_video(self, url: str) -> tuple[bytes, str] | None:
        """Try to download a video URL via proxy. Returns (bytes, mime) or None."""
        try:
            resp = requests.post(
                VIDEO_DOWNLOAD_URL,
                json={"url": url},
                timeout=(15, 120),
            )
            resp.raise_for_status()
            body = resp.json()
            b64 = body.get("b64_json")
            mime = str(body.get("mime_type", "")).lower()
            if not b64 or not mime.startswith("video/"):
                return None
            data = base64.b64decode(b64, validate=True)
            if not data or len(data) > MAX_VIDEO_BYTES:
                return None
            return data, mime
        except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _save_video(self, payload: bytes, ext: str) -> Path:
        self._output_root.mkdir(parents=True, exist_ok=True)
        target = self._output_root / f"video-{uuid.uuid4().hex}{ext}"
        target.write_bytes(payload)
        return target

    async def _deliver_video(self, event: AstrMessageEvent, path: Path):
        """Use QQ's native group-file upload; private chats receive a file message."""
        get_group_id = getattr(event, "get_group_id", None)
        group_id = str(get_group_id() if callable(get_group_id) else "").strip()
        if group_id and hasattr(event, "bot"):
            try:
                await upload_aiocqhttp_group_file(event.bot, group_id, path)
                return event.plain_result(f"视频已上传到群文件：{path.name}")
            except Exception as exc:
                logger.error("[VideoCmd] group video delivery failed: %s", type(exc).__name__)
                return event.plain_result("视频已生成，但上传到群文件失败，请稍后重试。")
        return event.chain_result([File(name=path.name, file=str(path))])

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=935)
    async def on_message(self, event: AstrMessageEvent):
        text = str(getattr(event, "get_message_str", lambda: "")() or "")
        prompt = _parse_video_command(text)
        if prompt is None or not (event.is_private_chat() or event.is_at_or_wake_command):
            lowered = text.lower()
            if prompt is None and any(kw in lowered for kw in ("视频", "video", "生成", "制作", "短片", "动画")):
                logger.info("[VideoCmd] SKIP text=%r", text[:120])
            return
        sender_id = self._sender(event)
        if not sender_id.isdigit():
            logger.warning("[VideoCmd] skip response without numeric sender")
            return
        tier = get_tier(sender_id, self._pro_db_path())

        # Show help for empty prompt
        if prompt == "":
            yield event.plain_result(
                f"搜索 B 站公开视频（所有版本）：/findvideo <关键词>\n"
                f"生成 4 秒原创视频（Pro {VIDEO_PRO_DAILY}次/天）：/video <描述>\n"
                f"示例：/findvideo 姆巴佩\n"
                f"示例：/video 一只猫 4s"
            )
            event.stop_event()
            return

        # ---- decide: search or generate ----
        search_mode = _is_search_mode(text, prompt)
        clean_prompt = _strip_duration(prompt) or prompt

        if search_mode:
            yield event.plain_result(SEARCHING_MSG)
            text, urls = await asyncio.to_thread(self._search_videos, clean_prompt)

            # Try downloading the first viable video
            downloaded = False
            downloaded_url = ""
            for url in urls[:3]:  # try first 3 URLs
                # Video platforms normally expose a page URL, not a direct .mp4.
                # The proxy validates the destination and lets yt-dlp resolve it.
                result = await asyncio.to_thread(self._try_download_video, url)
                if result:
                    payload, mime = result
                    ext_map = {
                        "video/mp4": ".mp4",
                        "video/webm": ".webm",
                        "video/x-matroska": ".mkv",
                        "video/quicktime": ".mov",
                    }
                    ext = ext_map.get(mime.split(";", 1)[0].strip().lower(), ".mp4")
                    output_path = self._save_video(payload, ext)
                    event.set_extra("_pro_video_output_paths", [str(output_path)])
                    yield await self._deliver_video(event, output_path)
                    downloaded = True
                    downloaded_url = url
                    break

            # Send text results (skip if we already sent the video + description)
            if text and not downloaded:
                yield event.plain_result(f"搜索「{clean_prompt}」的结果：\n\n{text}")
            elif text and downloaded:
                # Still show remaining links
                remaining = [u for u in urls if u and u != downloaded_url]
                if remaining:
                    links = "\n".join(remaining[:3])
                    yield event.plain_result(f"更多相关视频：\n{links}\n\n生成4秒以内的原创视频请用 /video <描述> <秒数>s")
                else:
                    yield event.plain_result("生成4秒以内的原创视频请用 /video <描述> <秒数>s")

            event.stop_event()
            return

        if tier < Tier.PRO:
            yield event.plain_result(PRO_VIDEO_MESSAGE)
            event.stop_event()
            return

        # ---- generate mode ----
        today = time.strftime("%Y%m%d")
        dk = f"{sender_id}:{today}"
        used = self._daily_usage.get(dk, 0)
        if used >= VIDEO_PRO_DAILY:
            yield event.plain_result(VIDEO_LIMIT_MSG.format(used=used, limit=VIDEO_PRO_DAILY))
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
        yield event.plain_result(GENERATING_MSG)

        try:
            async with self._generation_lock:
                payload, mime, ext = await asyncio.to_thread(self._request_video, clean_prompt)
                output_path = self._save_video(payload, ext)
        except Exception as exc:
            logger.warning("[VideoCmd] generation failed: %s", type(exc).__name__)
            self._cooldowns.pop(sender_id, None)
            yield event.plain_result("视频生成失败，请稍后再试。")
            event.stop_event()
            return

        self._daily_usage[dk] = used + 1
        try:
            self._save_usage()
        except OSError:
            logger.warning("[VideoCmd] usage persistence failed")
        event.set_extra("_pro_video_output_paths", [str(output_path)])
        if ext == ".gif":
            yield event.chain_result([Image.fromFileSystem(str(output_path))])
        else:
            yield await self._deliver_video(event, output_path)
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
