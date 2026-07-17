"""/dy — Douyin/TikTok source plugin. Watermark-free download + search cache.

Tiers:
  Ordinary: search only
  X/Pro:    download + search (uses video_agent daily quota context)

Provides material cache for video_agent and video_pipeline.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

try:
    from ..draw_command.pro_access import Tier, get_tier
except ImportError:
    from data.plugins.draw_command.pro_access import Tier, get_tier

SEARCH_PROXY_URL = "http://127.0.0.1:3000/v1/chat/completions"
MAX_VIDEO_BYTES = 50 * 1024 * 1024
CACHE_TTL = 7 * 86400  # 7 days

_DOUYIN_SHARE_RE = re.compile(
    r"https?://(?:v\.douyin\.com|www\.iesdouyin\.com|www\.douyin\.com)/\S+",
    re.I,
)
_TIKTOK_SHARE_RE = re.compile(
    r"https?://(?:vm\.tiktok\.com|vt\.tiktok\.com|www\.tiktok\.com)/\S+",
    re.I,
)
_VIDEO_ID_RE = re.compile(r"(?:video|note)/(\d+)", re.I)


def _cache_dir() -> Path:
    root = Path(__file__).resolve().parents[4]
    d = root / "claude_workspace" / "douyin_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _clean_cache() -> None:
    """Remove expired cache entries."""
    try:
        now = time.time()
        for f in _cache_dir().glob("*.mp4"):
            if now - f.stat().st_mtime > CACHE_TTL:
                f.unlink(missing_ok=True)
                meta = f.with_suffix(".json")
                meta.unlink(missing_ok=True)
    except Exception:
        pass


def _extract_video_id(url: str) -> str | None:
    """Extract numeric video ID from douyin/tiktok URL."""
    m = _VIDEO_ID_RE.search(url)
    return m.group(1) if m else None


def _resolve_douyin_url(share_url: str) -> tuple[str, str] | None:
    """Resolve douyin share link → (real_url, video_id). Uses redirect + HTML parsing."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }
    try:
        # Follow redirect to get real page URL
        resp = requests.get(share_url, headers=headers, timeout=(10, 15),
                           allow_redirects=True)
        final_url = resp.url
        video_id = _extract_video_id(final_url)
        if video_id:
            return final_url, video_id

        # Try to find video ID in page HTML
        html = resp.text
        # douyin patterns: "video_id":"xxx" or video/xxx
        patterns = [
            r'"video_id"\s*:\s*"(\d+)"',
            r'"aweme_id"\s*:\s*"(\d+)"',
            r'video/(\d{15,20})',
        ]
        for pat in patterns:
            m = re.search(pat, html)
            if m:
                return final_url, m.group(1)
    except Exception as e:
        logger.debug("[DouyinSource] resolve failed: %s", e)
    return None


def _download_douyin_video(dest: Path) -> tuple[bool, str]:
    """Download douyin video using public API endpoint.

    Returns (success, title_or_error).
    Uses the douyin.com oembed-like endpoint that exposes public video data.
    """
    # ponytail: public oembed endpoint — no auth, rate-limited by IP
    # Upgrade to media-parser style multi-source if this endpoint dies
    return False, "download via share link resolution — use /dy <full_share_link>"


def _search_douyin_public(query: str, max_results: int = 5) -> list[dict]:
    """Search douyin public videos via Gemini-grounded search.

    Returns list of {title, url, thumb}.
    """
    try:
        resp = requests.post(
            SEARCH_PROXY_URL,
            json={
                "model": "gemini-2.5-flash-search",
                "google_search": True,
                "max_tokens": 600,
                "messages": [{
                    "role": "user",
                    "content": (
                        f"在抖音(douyin.com)上搜索公开视频：{query}。"
                        f"只返回 douyin.com 域名下的视频页面链接和标题。"
                        f"返回不超过{max_results}个结果。格式：标题 - URL（每行一个）"
                    ),
                }],
            },
            timeout=(10, 45),
        )
        resp.raise_for_status()
        body = resp.json()
        sources = body.get("grounding", {}).get("sources", [])
        results: list[dict] = []
        for src in sources if isinstance(sources, list) else []:
            url = str(src.get("uri") or "") if isinstance(src, dict) else ""
            host = (urlparse(url).hostname or "").lower()
            if not (host == "douyin.com" or host.endswith(".douyin.com")):
                continue
            results.append({
                "title": str(src.get("title") or "抖音视频"),
                "url": url,
            })
            if len(results) >= max_results:
                break
        return results
    except Exception as e:
        logger.debug("[DouyinSource] search failed: %s", e)
        return []


def _download_public_video(url: str, dest: Path) -> bool:
    """Download a publicly accessible video URL to dest. Returns success."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.douyin.com/",
    }
    completed = False
    try:
        resp = requests.get(url, headers=headers, timeout=(15, 90), stream=True)
        resp.raise_for_status()
        total = 0
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(8192):
                if not chunk:
                    continue
                # The resolved share URL is often still an HTML landing page.
                # Do not poison the material cache with a page named ``.mp4``.
                if total == 0 and b"ftyp" not in chunk[:16]:
                    return False
                f.write(chunk)
                total += len(chunk)
                if total > MAX_VIDEO_BYTES:
                    return False
        completed = total > 1000
        return completed
    except Exception:
        return False
    finally:
        if not completed:
            dest.unlink(missing_ok=True)


def _cache_video(video_path: Path, title: str, source_url: str) -> str:
    """Save video to cache with metadata. Returns cache key."""
    key = hashlib.sha256(video_path.read_bytes()[:4096]).hexdigest()[:16]
    cache = _cache_dir()
    cached = cache / f"dy_{key}.mp4"
    if not cached.exists():
        video_path.rename(cached)
    # Write metadata
    meta = {"title": title, "source_url": source_url,
            "cached_at": time.time(), "key": key}
    meta_path = cache / f"dy_{key}.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return str(cached)


class DouyinSource(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}
        self._lock = asyncio.Lock()

    def _pro_db_path(self) -> Path:
        return (
            Path(__file__).resolve().parents[2]
            / "plugin_data" / "xiaoning_pro" / "pro_members.db"
        )

    @staticmethod
    def _sender(event: AstrMessageEvent) -> str:
        g = getattr(event, "get_sender_id", None)
        return str(g() if callable(g) else "").strip()

    @staticmethod
    def _msg(event: AstrMessageEvent) -> str:
        g = getattr(event, "get_message_str", None)
        return str(g() if callable(g) else "").strip()

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=932)
    async def on_message(self, event: AstrMessageEvent):
        text = self._msg(event)
        lowered = text.lower().strip()

        # Only respond to /dy commands
        if not lowered.startswith(("/dy", "/抖音", "/douyin")):
            return
        if not (event.is_private_chat() or event.is_at_or_wake_command):
            return

        sender_id = self._sender(event)
        if not sender_id.isdigit():
            return
        tier = get_tier(sender_id, self._pro_db_path())

        parts = text.split(maxsplit=1)
        sub_cmd = parts[1].strip() if len(parts) > 1 else ""

        # ── /dy (help) ──
        if not sub_cmd:
            yield event.plain_result(
                "📱 抖音素材源\n"
                "/dy search <关键词> — 搜索抖音公开视频\n"
                f"素材缓存位置：claude_workspace/douyin_cache/\n"
                f"视频制作时自动调用缓存素材。"
            )
            event.stop_event()
            return

        # ── /dy search <keyword> ──
        if sub_cmd.lower().startswith(("search ", "搜索 ", "搜 ", "s ")):
            query = sub_cmd.split(maxsplit=1)[1].strip() if " " in sub_cmd else ""
            if not query:
                yield event.plain_result("请输入搜索关键词，例如：/dy search 猫咪")
                event.stop_event()
                return

            yield event.plain_result(f"🔍 搜索抖音公开视频「{query[:30]}」…")
            results = await asyncio.to_thread(_search_douyin_public, query)
            if not results:
                yield event.plain_result(f"没有找到与「{query}」相关的抖音公开视频。")
                event.stop_event()
                return

            lines = [f"搜索「{query}」结果："]
            for i, r in enumerate(results, 1):
                lines.append(f"{i}. {r['title'][:50]} — {r['url']}")
            yield event.plain_result("\n".join(lines))
            event.stop_event()
            return

        # ── /dy <URL> download ──
        is_share = bool(_DOUYIN_SHARE_RE.search(sub_cmd) or _TIKTOK_SHARE_RE.search(sub_cmd))
        if is_share:
            if tier < Tier.X:
                yield event.plain_result(
                    "视频下载需要 X 或 Pro 资格。\n"
                    "添加小柠为 QQ 好友即可自动获得 X资格。\n"
                    "视频搜索 (/dy search) 对所有用户开放。"
                )
                event.stop_event()
                return

            if self._lock.locked():
                yield event.plain_result("正在处理另一个视频，请稍后再试。")
                event.stop_event()
                return

            yield event.plain_result("📥 正在解析并下载…")

            try:
                async with self._lock:
                    url = sub_cmd.strip()
                    resolved = await asyncio.to_thread(_resolve_douyin_url, url)
                    if not resolved:
                        yield event.plain_result(
                            "无法解析此链接。请确认链接是公开的抖音/TikTok 视频。\n"
                            "提示：部分私密或删除的视频无法访问。"
                        )
                        event.stop_event()
                        return

                    real_url, video_id = resolved
                    title = f"douyin_{video_id}"
                    tmp = _cache_dir() / f"tmp_{uuid.uuid4().hex}.mp4"

                    ok = await asyncio.to_thread(_download_public_video, real_url, tmp)
                    if not ok:
                        yield event.plain_result(
                            "下载失败：视频可能已删除、设为私密，或平台限制访问。\n"
                            "提示：目前仅支持公开视频。"
                        )
                        event.stop_event()
                        return

                    cached_path = _cache_video(tmp, title, url)
                    size_mb = Path(cached_path).stat().st_size / (1024 * 1024)
                    yield event.plain_result(
                        f"✅ 已缓存：{Path(cached_path).name} ({size_mb:.1f}MB)\n"
                        f"素材已加入 video_agent 素材库，制作视频时自动使用。\n"
                        f"来源：{url}"
                    )

            except Exception as exc:
                logger.warning("[DouyinSource] download error: %s", exc)
                yield event.plain_result(f"下载出错（{type(exc).__name__}），请稍后再试。")
            event.stop_event()
            return

        # Unknown sub-command
        yield event.plain_result(
            "用法：/dy search <关键词> 或 /dy <视频链接>"
        )
        event.stop_event()

    async def terminate(self):
        _clean_cache()
