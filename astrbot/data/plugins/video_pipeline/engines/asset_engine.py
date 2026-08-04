"""Multi-source asset acquisition with quality scoring and fallback."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

IMAGE_PROXY = "http://127.0.0.1:3000/v1/images/generations"
PEXELS_BASE = "https://api.pexels.com/videos"
MAX_CLIP_BYTES = 30 * 1024 * 1024


def _load_pexels_key() -> str:
    key = os.environ.get("PEXELS_API_KEY", "").strip()
    if key or os.name != "nt":
        return key
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as h:
            value, _ = winreg.QueryValueEx(h, "PEXELS_API_KEY")
        return str(value or "").strip()
    except OSError:
        return ""


PEXELS_API_KEY = _load_pexels_key()


@dataclass
class Asset:
    path: Path
    source: str  # "pexels" | "douyin_cache" | "ai_image"
    quality_score: float
    metadata: dict[str, Any] | None = None


class AssetEngine:
    """Multi-source asset acquisition with three-tier fallback.

    Pexels (free stock) → Douyin cache (Chinese content) → AI image → zoompan
    """

    def __init__(self, cache_root: Path):
        self._cache_root = Path(cache_root)
        self._douyin_dir = self._cache_root.parent / "douyin_cache"

    # ── Pexels ──────────────────────────────────────────────

    def _search_pexels(self, query: str, per_page: int = 3) -> list[str]:
        if not PEXELS_API_KEY:
            return []
        try:
            resp = requests.get(
                f"{PEXELS_BASE}/search",
                params={"query": query, "per_page": per_page, "orientation": "landscape"},
                headers={"Authorization": PEXELS_API_KEY},
                timeout=15,
            )
            resp.raise_for_status()
            videos = resp.json().get("videos", [])
            urls: list[str] = []
            for video in videos:
                files = sorted(
                    video.get("video_files", []),
                    key=lambda f: (f.get("width", 0) or 0) * (f.get("height", 0) or 0),
                )
                for f in files:
                    url = f.get("link", "")
                    if url and url.endswith(".mp4"):
                        urls.append(url)
                        break
                if len(urls) >= per_page:
                    break
            return urls
        except Exception:
            return []

    # ── Douyin cache ────────────────────────────────────────

    def _search_douyin_cache(self, query: str, per_page: int = 3) -> list[str]:
        if not self._douyin_dir.is_dir():
            return []
        try:
            results: list[tuple[float, str]] = []
            keywords = query.lower().split()
            for f in self._douyin_dir.glob("*.mp4"):
                meta_file = self._douyin_dir / f"{f.stem}.json"
                meta_text = ""
                if meta_file.exists():
                    try:
                        meta = json.loads(meta_file.read_text(encoding="utf-8"))
                        meta_text = str(meta.get("title", "") or meta.get("desc", "")).lower()
                    except (OSError, json.JSONDecodeError):
                        pass
                score = sum(1 for kw in keywords if kw in f"{f.stem} {meta_text}".lower())
                if score > 0:
                    results.append((score, str(f)))
            results.sort(key=lambda x: -x[0])
            return [url for _, url in results[:per_page]]
        except Exception:
            return []

    # ── AI image → zoompan ──────────────────────────────────

    def _generate_ai_clip(self, visual: str, duration: float,
                          dest: Path, resolution: str = "720p") -> bool:
        width, height = {
            "480p": (854, 480), "720p": (1280, 720), "1080p": (1920, 1080),
        }.get(resolution, (1280, 720))
        still = dest.with_suffix(".png")
        try:
            resp = requests.post(
                IMAGE_PROXY,
                json={
                    "prompt": (
                        "Cinematic video frame, photorealistic, rich color, "
                        f"16:9 composition, no text, no watermark. Scene: {visual}"
                    ),
                    "model": "gemini-3.1-flash-image",
                    "size": "1024x576",
                },
                timeout=(30, 240),
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
            encoded = data[0].get("b64_json") if data else None
            if not isinstance(encoded, str):
                return False
            payload = base64.b64decode(encoded, validate=True)
            if not payload or len(payload) > 20 * 1024 * 1024:
                return False
            still.write_bytes(payload)
            frames = max(25, int(max(1.0, float(duration)) * 25))
            vf = (
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},"
                f"zoompan=z='min(zoom+0.0012,1.05)':d={frames}:s={width}x{height}:fps=25,"
                "format=yuv420p"
            )
            result = subprocess.run(
                ["ffmpeg", "-y", "-loop", "1", "-i", str(still),
                 "-vf", vf, "-t", f"{max(1.0, float(duration)):.2f}",
                 "-c:v", "libx264", "-preset", "fast", "-crf", "25",
                 "-pix_fmt", "yuv420p", str(dest)],
                capture_output=True, timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            return result.returncode == 0 and dest.is_file() and dest.stat().st_size > 1000
        except Exception:
            return False

    # ── Download helper ─────────────────────────────────────

    def _download_clip(self, url: str, dest: Path) -> bool:
        try:
            resp = requests.get(url, timeout=(15, 60), stream=True)
            resp.raise_for_status()
            total = 0
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
                    total += len(chunk)
                    if total > MAX_CLIP_BYTES:
                        return False
            return dest.stat().st_size > 1000
        except Exception:
            return False

    # ── Public API ──────────────────────────────────────────

    def acquire(self, visual: str, duration: float,
                resolution: str = "720p") -> Asset | None:
        """Acquire best available asset with quality scoring.

        Returns Asset or None if all sources fail.
        """
        # Tier 1: Pexels
        urls = self._search_pexels(visual, 1)
        if urls:
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
                dest = Path(tmp.name)
            if self._download_clip(urls[0], dest):
                return Asset(dest, "pexels", 0.85)

        # Tier 2: Douyin cache
        cache_urls = self._search_douyin_cache(visual, 1)
        if cache_urls:
            return Asset(Path(cache_urls[0]), "douyin_cache", 0.70)

        # Tier 3: Pexels retry with simplified query
        simple = " ".join(
            w for w in visual.split()
            if w.lower() not in {"stunning", "beautiful", "epic", "cinematic",
                                  "gorgeous", "breathtaking", "dramatic"}
        )
        if simple != visual:
            urls = self._search_pexels(simple, 1)
            if urls:
                with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
                    dest = Path(tmp.name)
                if self._download_clip(urls[0], dest):
                    return Asset(dest, "pexels", 0.65)

        # Tier 4: AI image → zoompan (always available as ultimate fallback)
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            dest = Path(tmp.name)
        if self._generate_ai_clip(visual, duration, dest, resolution):
            return Asset(dest, "ai_image", 0.50)

        return None

    def acquire_multi(self, visuals: list[str], durations: list[float],
                      resolution: str = "720p") -> list[Asset | None]:
        """Acquire assets for multiple scenes. Returns list parallel to inputs."""
        return [self.acquire(v, d, resolution) for v, d in zip(visuals, durations)]


__all__ = ["AssetEngine", "Asset"]
