"""/做视频 — one-sentence video agent. Gemini script → TTS → stock footage → FFmpeg.

Tiers:
  Ordinary: 480p ≤30s 1/day
  X:        720p ≤60s 3/day
  PRO:      1080p ≤120s 10/day

Dependencies: FFmpeg (PATH), edge-tts (pip), Pexels API key (free: pexels.com)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import subprocess
import traceback
import tempfile
import time
import uuid
from pathlib import Path

import requests
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

try:
    from ..draw_command.pro_access import Tier, get_tier
except ImportError:
    from data.plugins.draw_command.pro_access import Tier, get_tier

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

PROXY_CHAT = "http://127.0.0.1:3000/v1/chat/completions"
IMAGE_PROXY = "http://127.0.0.1:3000/v1/images/generations"
PEXELS_BASE = "https://api.pexels.com/videos"
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()

# TTS multi-voice mapping — edge-tts built-in voices, zero cost
TTS_VOICES = {
    "narration": "zh-CN-YunxiNeural",       # 旁白男声，沉稳
    "female": "zh-CN-XiaoxiaoNeural",       # 女声，活泼
    "male": "zh-CN-YunyangNeural",          # 男声，新闻感
    "storytelling": "zh-CN-XiaoyiNeural",   # 讲故事，有感情
    "gentle": "zh-CN-XiaochenNeural",       # 温柔女声
    "default": "zh-CN-XiaoxiaoNeural",
}

# xfade transition presets — AutoShorts AI reference
XFADE_PRESETS = {
    "fade": "fade",
    "slide": "slideright",
    "pixel": "pixelize",
    "dissolve": "fadegrays",
    "wipe": "wiperight",
    "zoom": "zoomin",
}


def _load_pexels_api_key() -> str:
    key = os.environ.get("PEXELS_API_KEY", "").strip()
    if key or os.name != "nt":
        return key
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
            value, _kind = winreg.QueryValueEx(handle, "PEXELS_API_KEY")
        return str(value or "").strip()
    except OSError:
        return ""


PEXELS_API_KEY = _load_pexels_api_key()

AGENT_SYSTEM = """你是视频脚本撰写助手。根据用户主题，生成一段短视频脚本。

返回 JSON：
{{
  "title": "视频标题（≤15字）",
  "scenes": [
    {{"narration": "旁白文案（≤80字）", "visual": "画面描述英文关键词（用于搜索素材）", "duration": 5}}
  ]
}}

规则：
- 总时长不超过 {max_duration} 秒
- {max_scenes} 个场景以内
- narration 是配音朗读的文字（中文）
- visual 用英文关键词（Pexels 搜索用）
- duration 每个场景 4-8 秒
只返回 JSON，不要其他文字。"""

# Tier config: (max_duration_sec, max_scenes, daily_limit, resolution)
TIER_CONFIG = {
    Tier.ORDINARY: (0, 0, 0, "0p"),   # no video agent access
    Tier.X: (60, 6, 1, "720p"),       # 1/day
    Tier.PRO: (120, 10, 5, "1080p"),   # 5/day
}

_HIGH_QUALITY_PIPELINE_RE = re.compile(
    r"(?:高质量|专业|精美|电影级).{0,80}(?:视频|短片)"
    r"|(?:视频|短片).{0,12}(?:工坊|工作室|全流程)",
    re.I,
)


def _parse_agent_command(text: str) -> str | None:
    """Extract topic from /做视频 or natural language. Returns None=not a match, ''=help."""
    raw = str(text or "").strip()
    lowered = raw.lower()

    # ── Command prefix ──
    for prefix in ("/做视频", "/制作视频", "/视频制作", "/makevideo", "/videomake",
                   "/做短片", "/制作短片"):
        if lowered.startswith(prefix):
            rest = raw.split(maxsplit=1)[1].strip() if " " in raw else ""
            return rest if rest else ""

    # Explicitly naming Video Agent must route here instead of falling through
    # to ordinary chat or the short Veo clip generator.
    agent_prefix = re.match(
        r"^(?:小柠[，,\s]*)?(?:请|帮我|给我)?(?:使用|用)?\s*"
        r"(?:视频\s*agent|video\s*agent)\s*(?:帮我|给我)?\s*",
        raw,
        re.I,
    )
    if agent_prefix:
        raw = raw[agent_prefix.end():].strip()
        if not raw:
            return ""

    # High-quality/workshop wording belongs to video_pipeline.  Keep this
    # parser silent even if the topic-before-视频 pattern would otherwise match.
    if not agent_prefix and _HIGH_QUALITY_PIPELINE_RE.search(raw):
        return None

    # Plain “生成/创建/画视频” belongs to the short Veo generator.  It reaches
    # this parser only when the user explicitly names Video Agent.
    if not agent_prefix and re.match(
        r"^(?:小柠[，,\s]*)?(?:帮我|请|给我|来|想|想要?)?\s*"
        r"(?:生成|创建|画).{0,80}(?:视频|短片|动画)",
        raw,
        re.I,
    ):
        return None

    # ── Natural language: wide match for "做/制作/弄 一个/个 视频/短片" ──
    patterns = [
        # "帮我做个猫猫视频" (prefix + verb ... topic ... 视频 at end)
        r"(?:小柠[，,\s]*)?(?:帮我|请|给我|来|想|想要?)\s*"
        r"(?:做|制作|弄|搞|生成|整)\s*(?:一?[个段部]\s*)?"
        r"(?:一个|一段)?\s*(.+?)\s*(?:的)?(?:视频|短片)\s*$",
        # "帮我做个视频 关于xxx" (prefix + verb ... 视频 ... topic)
        r"(?:小柠[，,\s]*)?(?:帮我|请|给我|来|想|想要?)\s*"
        r"(?:做|制作|弄|搞|生成|整)\s*(?:一?[个段部]\s*)?(?:视频|短片)\s*(.+)",
        # "做个视频xxx", "做一个视频关于xxx" (verb + optional quantifier + 视频 + topic, no prefix)
        r"(?:做|制作|弄|搞|生成|整)\s*(?:一?[个段部]\s*)?(?:视频|短片)\s*(.+)",
        # "做一段如何成为博主的视频" (verb + quantifier + topic + 视频 at end, no prefix)
        r"(?:做|制作|弄|搞|生成|整)\s*(?:一?[个段部]\s*)"
        r"(.+?)(?:视频|短片)\s*$",
        # "能帮我做视频吗xxx", "可以做视频吗xxx" (capability)
        r"(?:能|可以|能不能|可不可以)\s*(?:帮我|给我)?\s*"
        r"(?:做|制作|弄|生成)\s*(?:一?[个段部]\s*)?(?:视频|短片)\s*(.+)",
    ]
    for pattern in patterns:
        m = re.match(pattern, raw, re.I)
        if m:
            topic = m.group(1).strip()
            # Filter out noise tails like "吗", "呢", "？"
            topic = re.sub(r"[吗呢嘛吧啊][？?！!。.]*$", "", topic).strip()
            topic = re.sub(r"的$", "", topic).strip()
            topic = re.sub(r"^如何关于", "如何", topic).strip()
            if topic and len(topic) <= 500:
                return topic
            if not topic:
                return ""  # show help
            return None

    return None


def _generate_script(topic: str, max_duration: int, max_scenes: int) -> dict | None:
    """Call Gemini to generate video script, with retry on parse failure."""
    system = AGENT_SYSTEM.format(max_duration=max_duration, max_scenes=max_scenes)

    for attempt in range(3):
        try:
            resp = requests.post(
                PROXY_CHAT,
                json={
                    "model": "gemini-2.5-flash",
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": f"主题：{topic}"},
                    ],
                    "max_tokens": 2048,
                },
                timeout=(15, 45),
            )
            resp.raise_for_status()
            body = resp.json()
            raw = str(body.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
            if not raw:
                logger.warning("[VideoAgent] empty script response attempt=%d", attempt + 1)
                continue
            # ── Strip ```json / ``` fences ──
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*", "", raw)
                raw = re.sub(r"\s*```\s*$", "", raw)
            raw = raw.strip()
            if not raw.startswith("{"):
                logger.warning("[VideoAgent] unexpected script response: %s", raw[:200])
                continue
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and parsed.get("scenes"):
                return parsed
            logger.warning("[VideoAgent] parsed JSON missing scenes: %s", str(parsed)[:200])
        except json.JSONDecodeError as e:
            logger.warning("[VideoAgent] JSON parse error attempt=%d: %s", attempt + 1, e)
        except (requests.RequestException, ValueError, KeyError) as e:
            logger.warning("[VideoAgent] script generation failed attempt=%d: %s", attempt + 1, e)
            if attempt == 2:
                return None
    return None


def _search_pexels(query: str, per_page: int = 3) -> list[str]:
    """Search Pexels for free stock video clips. Returns list of download URLs."""
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
            # Get lowest-resolution video file (faster download for QQ)
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
    except Exception as e:
        logger.debug("[VideoAgent] Pexels search failed: %s", e)
        return []


def _download_clip(url: str, dest: Path) -> bool:
    """Download a video clip. Returns True on success."""
    try:
        resp = requests.get(url, timeout=(15, 60), stream=True)
        resp.raise_for_status()
        total = 0
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)
                total += len(chunk)
                if total > 30 * 1024 * 1024:  # 30MB max per clip
                    return False
        return dest.stat().st_size > 1000
    except Exception:
        return False


def _generate_scene_clip(
    visual: str, duration: float, dest: Path, resolution: str
) -> bool:
    """Generate a real scene still and animate it when stock footage is unavailable."""
    width, height = {
        "480p": (854, 480),
        "720p": (1280, 720),
        "1080p": (1920, 1080),
    }.get(resolution, (1280, 720))
    still = dest.with_suffix(".png")
    try:
        response = requests.post(
            IMAGE_PROXY,
            json={
                "prompt": (
                    "Cinematic documentary video frame, realistic, visually rich, "
                    f"16:9 composition, no captions, no logo, no watermark. Scene: {visual}"
                ),
                "model": "gemini-3.1-flash-image",
                "size": "1024x576",
            },
            timeout=(30, 240),
        )
        response.raise_for_status()
        data = response.json().get("data", [])
        encoded = data[0].get("b64_json") if data else None
        if not isinstance(encoded, str):
            return False
        payload = base64.b64decode(encoded, validate=True)
        if not payload or len(payload) > 20 * 1024 * 1024:
            return False
        still.write_bytes(payload)
        frames = max(25, int(max(1.0, float(duration)) * 25))
        video_filter = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},"
            f"zoompan=z='min(zoom+0.001,1.08)':d={frames}:s={width}x{height}:fps=25,"
            "format=yuv420p"
        )
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-loop", "1", "-i", str(still),
                "-vf", video_filter,
                "-t", f"{max(1.0, float(duration)):.2f}",
                "-c:v", "libx264", "-preset", "fast", "-crf", "25",
                "-pix_fmt", "yuv420p", str(dest),
            ],
            capture_output=True,
            timeout=120,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return result.returncode == 0 and dest.is_file() and dest.stat().st_size > 1000
    except Exception as exc:
        logger.warning("[VideoAgent] generated scene fallback failed: %s", type(exc).__name__)
        return False


async def _tts_audio(text: str, output_path: Path) -> bool:
    """Generate TTS audio with edge-tts. Returns True on success."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "edge-tts", "--voice", "zh-CN-XiaoxiaoNeural",
            "--text", text, "--write-media", str(output_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return output_path.stat().st_size > 100
    except Exception:
        return False


def _get_duration(file_path: Path) -> float:
    """Get media duration in seconds via ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(file_path)],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return float(result.stdout.strip() or 0)
    except (ValueError, subprocess.SubprocessError, FileNotFoundError):
        return 5.0


def _subtitle_font_path() -> Path | None:
    candidates = (
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/msyhbd.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/simsun.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
    )
    return next((path for path in candidates if path.is_file()), None)


def _ffmpeg_filter_path(path: Path) -> str:
    return path.resolve(strict=True).as_posix().replace(":", r"\:").replace("'", r"\'")


_BLACK_DURATION_RE = re.compile(r"black_duration:([0-9]+(?:\.[0-9]+)?)")


def _black_ratio_from_ffmpeg(output: str, duration: float) -> float:
    if duration <= 0:
        return 1.0
    black = sum(float(value) for value in _BLACK_DURATION_RE.findall(str(output or "")))
    return min(1.0, black / duration)


def _video_is_usable(path: Path) -> bool:
    duration = _get_duration(path)
    if duration < 1 or not path.is_file() or path.stat().st_size <= 1000:
        return False
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-i", str(path),
                "-vf", "blackdetect=d=0.5:pix_th=0.10",
                "-an", "-f", "null", "NUL" if os.name == "nt" else "/dev/null",
            ],
            capture_output=True,
            timeout=90,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    diagnostic = result.stderr.decode("utf-8", errors="replace")
    ratio = _black_ratio_from_ffmpeg(diagnostic, duration)
    if ratio >= 0.90:
        logger.warning("[VideoAgent] rejected near-black output ratio=%.3f", ratio)
        return False
    return True


def _video_delivery_message(
    title: str, delivery: ArtifactDeliveryResult, used: int, daily_limit: int
) -> str:
    if delivery.delivered:
        return (
            f"✅ 视频「{title}」制作完成！已发送。\n"
            f"剩余次数：{daily_limit - used - 1}/{daily_limit}"
        )
    retry_note = (
        "已加入后台重试队列，稍后自动送达。"
        if delivery.channel == "queued"
        else "文件已安全保留，请稍后重试。"
    )
    return (
        f"视频「{title}」已生成，但 QQ 文件尚未交付，任务未完成；"
        f"{retry_note}本次次数不计。"
    )


def _compose_video(
    clips: list[Path],
    audio_paths: list[Path],
    subtitles: list[str],
    output_path: Path,
    resolution: str = "720p",
) -> bool:
    """Compose final video with FFmpeg: concat clips + overlay subtitles + merge audio."""
    if not clips:
        return False

    width, height = {"480p": (854, 480), "720p": (1280, 720), "1080p": (1920, 1080)}.get(
        resolution, (1280, 720)
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # Step 1: Resize & normalize all clips
        normalized: list[Path] = []
        for i, clip in enumerate(clips):
            out = tmp / f"clip_{i}.mp4"
            normalized_result = subprocess.run(
                ["ffmpeg", "-y", "-i", str(clip),
                 "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1",
                 "-c:v", "libx264", "-preset", "fast", "-crf", "28",
                 "-an", "-t", "8", str(out)],
                capture_output=True, timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if normalized_result.returncode == 0 and out.exists() and out.stat().st_size > 1000:
                normalized.append(out)

        if len(normalized) != len(clips):
            return False

        # Step 2: Build concat file
        concat_list = tmp / "concat.txt"
        concat_list.write_text(
            "\n".join(f"file '{p.as_posix()}'" for p in normalized), encoding="utf-8"
        )

        # Step 3: Merge audio tracks
        audio_concat = tmp / "audio_merged.mp3"
        if audio_paths and all(p.exists() for p in audio_paths):
            audio_inputs = []
            for p in audio_paths:
                audio_inputs.extend(["-i", str(p)])
            subprocess.run(
                ["ffmpeg", "-y", *audio_inputs,
                 "-filter_complex", f"concat=n={len(audio_paths)}:v=0:a=1",
                 "-ac", "1", str(audio_concat)],
                capture_output=True, timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

        # Step 4: Assemble final video
        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
        ]
        if audio_concat.exists():
            cmd.extend(["-i", str(audio_concat)])
            cmd.extend(["-c:v", "libx264", "-preset", "fast", "-crf", "26",
                        "-c:a", "aac", "-b:a", "96k",
                        "-shortest", "-movflags", "+faststart"])
        else:
            cmd.extend(["-c:v", "libx264", "-preset", "fast", "-crf", "26",
                        "-movflags", "+faststart"])

        # Add subtitle burn-in if subtitles present
        if any(subtitles):
            font = _subtitle_font_path()
            if font is None:
                logger.warning("[VideoAgent] no CJK subtitle font available")
                return False
            font_arg = _ffmpeg_filter_path(font)
            draw_parts: list[str] = []
            t = 0.0
            for i, sub in enumerate(subtitles):
                dur = _get_duration(normalized[i]) if i < len(normalized) else 5.0
                subtitle_file = tmp / f"subtitle_{i}.txt"
                subtitle_file.write_text(str(sub), encoding="utf-8")
                subtitle_arg = _ffmpeg_filter_path(subtitle_file)
                draw_parts.append(
                    f"drawtext=fontfile='{font_arg}':textfile='{subtitle_arg}':"
                    f"fontsize=30:fontcolor=white:"
                    f"x=(w-text_w)/2:y=h-th-60:"
                    f"box=1:boxcolor=black@0.5:boxborderw=6:"
                    f"enable='between(t,{t:.1f},{t + dur:.1f})'"
                )
                t += dur
            vf_arg = ",".join(draw_parts)
            try:
                idx = cmd.index("-movflags")
            except ValueError:
                idx = len(cmd) - 1
            cmd.insert(idx, "-vf")
            cmd.insert(idx + 1, vf_arg)

        cmd.append(str(output_path))
        result = subprocess.run(
            cmd, capture_output=True, timeout=180,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        success = (
            result.returncode == 0
            and output_path.is_file()
            and output_path.stat().st_size > 1000
            and _video_is_usable(output_path)
        )
        if not success:
            stderr = result.stderr.decode("utf-8", errors="replace")[-300:]
            logger.warning("[VideoAgent] FFmpeg failed: %s", stderr)
        return success


def _fmt_srt(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ═══════════════════════════════════════════════════════════════
# Enhanced functions — additive, existing functions untouched
# ═══════════════════════════════════════════════════════════════


def _simplify_query(query: str) -> str:
    """Simplify a visual query for retry — drop adjectives, keep nouns."""
    stopwords = {"beautiful", "stunning", "amazing", "cinematic", "epic", "dramatic",
                 "gorgeous", "breathtaking", "spectacular", "magnificent", "vibrant"}
    words = [w for w in query.split() if w.lower() not in stopwords]
    return " ".join(words) if words else query


def _generate_script_deepseek(topic: str, max_duration: int, max_scenes: int) -> dict | None:
    """DeepSeek script generation — used as fallback when Gemini fails."""
    if not DEEPSEEK_KEY:
        return None
    system = AGENT_SYSTEM.format(max_duration=max_duration, max_scenes=max_scenes)
    try:
        resp = requests.post(
            DEEPSEEK_URL,
            headers={"Authorization": f"Bearer {DEEPSEEK_KEY}"},
            json={
                "model": "deepseek-v4-flash",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": f"主题：{topic}"},
                ],
                "max_tokens": 2048,
                "temperature": 0.8,
            },
            timeout=(15, 45),
        )
        resp.raise_for_status()
        body = resp.json()
        raw = str(body.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
        if not raw:
            return None
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```\s*$", "", raw)
        raw = raw.strip()
        if not raw.startswith("{"):
            return None
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) and parsed.get("scenes") else None
    except Exception:
        return None


def _review_script_deepseek(script: dict) -> dict | None:
    """DeepSeek reviews script quality, returns {score:1-10, suggestions:[...]}."""
    if not DEEPSEEK_KEY:
        return None
    try:
        resp = requests.post(
            DEEPSEEK_URL,
            headers={"Authorization": f"Bearer {DEEPSEEK_KEY}"},
            json={
                "model": "deepseek-v4-flash",
                "messages": [
                    {"role": "system", "content": (
                        "你是视频脚本审核专家。从吸引力、节奏、画面多样性、叙事连贯性四个维度"
                        "给脚本打分(1-10)并给出改进建议。返回JSON："
                        '{{"score": 7, "suggestions": ["建议1", "建议2"]}}'
                        "只返回JSON，不要其他文字。"
                    )},
                    {"role": "user", "content": json.dumps(script, ensure_ascii=False)},
                ],
                "max_tokens": 512,
                "temperature": 0.3,
            },
            timeout=(10, 20),
        )
        resp.raise_for_status()
        raw = str(resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```\s*$", "", raw)
        return json.loads(raw) if raw.startswith("{") else None
    except Exception:
        return None


def _generate_script_v2(topic: str, max_duration: int, max_scenes: int) -> dict | None:
    """Multi-model script: Gemini primary → DeepSeek review → fallback to DeepSeek."""
    draft = _generate_script(topic, max_duration, max_scenes)
    if not draft:
        logger.info("[VideoAgent] Gemini script failed, fallback to DeepSeek")
        return _generate_script_deepseek(topic, max_duration, max_scenes)
    review = _review_script_deepseek(draft)
    if review and review.get("score", 10) < 6:
        logger.info("[VideoAgent] script score=%d < 6, retry with DeepSeek", review.get("score", 0))
        retry = _generate_script_deepseek(topic, max_duration, max_scenes)
        return retry if retry else draft
    return draft


def _search_douyin_cache(query: str, per_page: int = 3) -> list[str]:
    """Search local douyin_source cache for matching video files."""
    try:
        cache_dir = Path(__file__).resolve().parents[5] / "claude_workspace" / "douyin_cache"
        if not cache_dir.is_dir():
            return []
        results: list[tuple[float, str]] = []
        keywords = query.lower().split()
        for f in cache_dir.glob("*.mp4"):
            meta_file = cache_dir / f"{f.stem}.json"
            meta_text = ""
            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    meta_text = str(meta.get("title", "") or meta.get("desc", "")).lower()
                except (OSError, json.JSONDecodeError):
                    pass
            # Score by keyword overlap
            score = sum(1 for kw in keywords if kw in f"{f.stem} {meta_text}".lower())
            if score > 0:
                results.append((score, str(f)))
        results.sort(key=lambda x: -x[0])
        return [url for _, url in results[:per_page]]
    except Exception:
        return []


def _search_multi_source(query: str, per_page: int = 3) -> list[str]:
    """Three-tier asset acquisition: Pexels → Douyin cache → Pexels retry."""
    urls = _search_pexels(query, per_page)
    if urls:
        return urls
    cache_urls = _search_douyin_cache(query, per_page)
    if cache_urls:
        logger.info("[VideoAgent] using douyin_cache for query=%r", query[:60])
        return cache_urls
    simplified = _simplify_query(query)
    if simplified != query:
        urls = _search_pexels(simplified, per_page)
        if urls:
            logger.info("[VideoAgent] Pexels retry with simplified query=%r", simplified[:60])
            return urls
    return []


async def _tts_audio_with_voice(text: str, output_path: Path,
                                 voice_style: str = "default") -> bool:
    """TTS with voice selection. Falls back to default voice on failure."""
    voice = TTS_VOICES.get(voice_style, TTS_VOICES["default"])
    try:
        proc = await asyncio.create_subprocess_exec(
            "edge-tts", "--voice", voice,
            "--text", text, "--write-media", str(output_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if output_path.stat().st_size > 100:
            return True
        # Fallback to default voice
        if voice != TTS_VOICES["default"]:
            return await _tts_audio(text, output_path)
        return False
    except Exception:
        if voice != TTS_VOICES["default"]:
            return await _tts_audio(text, output_path)
        return False


def _compose_video_v2(
    clips: list[Path],
    audio_paths: list[Path],
    subtitles: list[str],
    output_path: Path,
    resolution: str = "720p",
    transition: str = "fade",
) -> bool:
    """Enhanced compose with xfade transitions, LUT, and Ken Burns motion.

    Falls back to _compose_video (simple concat) if xfade filtergraph fails.
    """
    if not clips:
        return False
    if len(clips) == 1 and transition == "fade":
        # Single clip — no transition needed, use simple compose
        return _compose_video(clips, audio_paths, subtitles, output_path, resolution)

    width, height = {"480p": (854, 480), "720p": (1280, 720), "1080p": (1920, 1080)}.get(
        resolution, (1280, 720)
    )
    xfade_type = XFADE_PRESETS.get(transition, "fade")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # Step 1: normalize all clips to same resolution
        normalized: list[Path] = []
        for i, clip in enumerate(clips):
            out = tmp / f"norm_{i}.mp4"
            dur = _get_duration(clip) if i < len(clips) else 5.0
            # Ken Burns slow zoom for visual interest
            motion_vf = (
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},"
                f"zoompan=z='min(zoom+0.0015,1.06)':d={max(25, int(dur*25))}:s={width}x{height}:fps=25,"
                f"format=yuv420p"
            )
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", str(clip),
                 "-vf", motion_vf,
                 "-c:v", "libx264", "-preset", "fast", "-crf", "25",
                 "-an", "-t", f"{dur:.2f}", str(out)],
                capture_output=True, timeout=90,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if result.returncode == 0 and out.is_file() and out.stat().st_size > 1000:
                normalized.append(out)

        if len(normalized) != len(clips):
            logger.warning("[VideoAgent] normalize clip count mismatch, fallback to simple compose")
            return _compose_video(clips, audio_paths, subtitles, output_path, resolution)

        # Step 2: build xfade filtergraph
        if len(normalized) == 1:
            # Single normalized clip — simple path
            return _compose_video(clips, audio_paths, subtitles, output_path, resolution)

        # Build xfade filter_complex string
        # Pattern: [0][1]xfade=transition=fade:duration=0.3:offset=3.8[x1];
        #          [x1][2]xfade=transition=fade:duration=0.3:offset=7.3[x2]; ...
        filter_parts: list[str] = []
        last_label = "0"
        accumulated_offset = 0.0
        transition_dur = 0.3

        for i in range(len(normalized)):
            dur = _get_duration(normalized[i])
            if i == 0:
                accumulated_offset += dur
                continue
            offset = accumulated_offset - transition_dur
            next_label = f"x{i}" if i < len(normalized) - 1 else "xfade_out"
            filter_parts.append(
                f"[{last_label}][{i}]xfade=transition={xfade_type}:"
                f"duration={transition_dur}:offset={offset:.2f}[{next_label}]"
            )
            last_label = next_label
            accumulated_offset += dur - transition_dur

        vf_filter = ";".join(filter_parts)

        # Step 3: merge audio
        audio_concat = tmp / "audio_merged.mp3"
        if audio_paths and all(p.exists() for p in audio_paths):
            audio_inputs = []
            for p in audio_paths:
                audio_inputs.extend(["-i", str(p)])
            subprocess.run(
                ["ffmpeg", "-y", *audio_inputs,
                 "-filter_complex", f"concat=n={len(audio_paths)}:v=0:a=1",
                 "-ac", "1", str(audio_concat)],
                capture_output=True, timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

        # Step 4: assemble
        inputs = []
        for p in normalized:
            inputs.extend(["-i", str(p)])
        cmd = ["ffmpeg", "-y", *inputs,
               "-filter_complex", vf_filter,
               "-map", f"[{last_label}]"]
        if audio_concat.exists():
            cmd.extend(["-i", str(audio_concat)])
            cmd.extend(["-map", "1:a:0"])
            cmd.extend(["-c:v", "libx264", "-preset", "fast", "-crf", "26",
                        "-c:a", "aac", "-b:a", "96k",
                        "-shortest", "-movflags", "+faststart"])
        else:
            cmd.extend(["-c:v", "libx264", "-preset", "fast", "-crf", "26",
                        "-movflags", "+faststart"])

        # Add subtitle burn-in
        if any(subtitles):
            font = _subtitle_font_path()
            if font is None:
                logger.warning("[VideoAgent] no CJK subtitle font, fallback to simple compose")
                return _compose_video(clips, audio_paths, subtitles, output_path, resolution)
            font_arg = _ffmpeg_filter_path(font)
            draw_parts: list[str] = []
            t = 0.0
            for i, sub in enumerate(subtitles):
                dur_val = _get_duration(normalized[i]) if i < len(normalized) else 5.0
                subtitle_file = tmp / f"sub_{i}.txt"
                subtitle_file.write_text(str(sub), encoding="utf-8")
                sub_arg = _ffmpeg_filter_path(subtitle_file)
                draw_parts.append(
                    f"drawtext=fontfile='{font_arg}':textfile='{sub_arg}':"
                    f"fontsize=30:fontcolor=white:"
                    f"x=(w-text_w)/2:y=h-th-60:"
                    f"box=1:boxcolor=black@0.5:boxborderw=6:"
                    f"enable='between(t,{t:.1f},{t + dur_val:.1f})'"
                )
                t += dur_val - transition_dur
            subtitle_vf = ",".join(draw_parts)
            # Chain subtitle filter after xfade output
            orig_vf = vf_filter
            vf_filter = f"{orig_vf};[{last_label}]{subtitle_vf}[final]"
            # Update map target
            for i, part in enumerate(cmd):
                if part == f"[{last_label}]":
                    cmd[i] = "[final]"
                    break

        cmd.append(str(output_path))
        result = subprocess.run(
            cmd, capture_output=True, timeout=300,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        success = (
            result.returncode == 0
            and output_path.is_file()
            and output_path.stat().st_size > 1000
            and _video_is_usable(output_path)
        )
        if not success:
            stderr = result.stderr.decode("utf-8", errors="replace")[-300:]
            logger.warning("[VideoAgent] xfade compose failed: %s, fallback to simple", stderr)
            return _compose_video(clips, audio_paths, subtitles, output_path, resolution)
        return success


# ═══════════════════════════════════════════════════════════════


class VideoAgent(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}
        data_dir = Path(StarTools.get_data_dir("video_agent"))
        data_dir.mkdir(parents=True, exist_ok=True)
        self._usage_file = data_dir / "usage.json"
        self._daily_usage = self._load_usage()
        project_root = Path(__file__).resolve().parents[4]
        self._output_root = project_root / "claude_workspace" / "video_agent"
        self._output_root.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

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
                k: v for k, v in raw.items()
                if k.endswith(f":{today}") and isinstance(v, int) and v >= 0
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}

    def _save_usage(self) -> None:
        tmp = self._usage_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._daily_usage, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._usage_file)

    @staticmethod
    def _sender(event: AstrMessageEvent) -> str:
        g = getattr(event, "get_sender_id", None)
        return str(g() if callable(g) else "").strip()

    @staticmethod
    def _msg(event: AstrMessageEvent) -> str:
        g = getattr(event, "get_message_str", None)
        return str(g() if callable(g) else "").strip()

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=934)
    async def on_message(self, event: AstrMessageEvent):
        text = self._msg(event)
        topic = _parse_agent_command(text)
        if topic is None:
            return
        if not (event.is_private_chat() or event.is_at_or_wake_command):
            return

        sender_id = self._sender(event)
        if not sender_id.isdigit():
            return

        tier = get_tier(sender_id, self._pro_db_path())
        cfg = TIER_CONFIG.get(tier)
        if cfg is None:
            logger.warning("[VideoAgent] unknown tier %s for sender %s", tier, sender_id)
            yield event.plain_result("权限查询异常，请稍后再试或发送 /pro status 确认。")
            event.stop_event()
            return
        max_dur, max_scenes, daily_limit, resolution = cfg

        # Usage cap
        if daily_limit == 0:
            yield event.plain_result(
                "视频制作需要 X 或 Pro 资格。添加小柠为 QQ 好友即可自动获得 X资格（每天 1 次）。"
            )
            event.stop_event()
            return
        today = time.strftime("%Y%m%d")
        dk = f"{sender_id}:{today}"
        used = self._daily_usage.get(dk, 0)
        if used >= daily_limit:
            yield event.plain_result(
                f"今日视频制作次数已用完（{used}/{daily_limit}）。明天自动重置。"
                + ("\nPro 资格每天 5 次，发送 /pro status 查看。" if tier < Tier.PRO else "")
            )
            event.stop_event()
            return

        if self._lock.locked():
            yield event.plain_result("正在制作另一个视频，请等这个完成后再试。")
            event.stop_event()
            return

        if topic == "":
            yield event.plain_result(
                "🎬 AI 视频制作\n"
                f"/做视频 <主题> — 一句话生成完整短视频\n"
                f"当前等级：{tier.value}（{resolution} ≤{max_dur}s {daily_limit}次/天）\n"
                f"示例：/做视频 如何在家做一杯拿铁咖啡"
            )
            event.stop_event()
            return

        task_id = uuid.uuid4().hex[:12]
        task_desc = f"制作完整视频：{topic[:140]}"
        await mirror_runtime_task_status(
            sender_id, task_id, task_desc, "in_progress", "video_agent_started", owner="video_agent"
        )
        yield event.plain_result(
            f"🎬 开始制作视频「{topic[:30]}」…\n"
            f"① 生成脚本 → ② 搜索素材 → ③ 合成配音 → ④ 渲染输出\n"
            f"预计 2-5 分钟，请耐心等待。"
        )

        try:
            async with self._lock:
                # Step 1: Generate script (multi-model: Gemini → DeepSeek review → fallback)
                script = await asyncio.to_thread(
                    _generate_script_v2, topic, max_dur, max_scenes
                )
                if not script or not script.get("scenes"):
                    await mirror_runtime_task_status(
                        sender_id, task_id, task_desc, "failed", "script_generation", owner="video_agent"
                    )
                    yield event.plain_result("脚本生成失败，请换个主题试试。")
                    event.stop_event()
                    return

                scenes = script["scenes"]
                title = script.get("title", topic[:15])

                yield event.plain_result(
                    f"📝 脚本已生成：{title}（{len(scenes)} 个场景）\n正在搜索素材和合成配音…"
                )

                # Step 2+3: Download clips + generate TTS in parallel per scene
                with tempfile.TemporaryDirectory() as tmpdir:
                    tmp = Path(tmpdir)
                    clip_paths: list[Path] = []
                    audio_paths: list[Path] = []
                    subtitles: list[str] = []

                    for i, scene in enumerate(scenes):
                        visual = scene.get("visual", topic)
                        narration = scene.get("narration", "")
                        subtitles.append(narration[:80])

                        # Search & download clip (three-tier: Pexels → Douyin cache → Pexels retry)
                        urls = await asyncio.to_thread(_search_multi_source, visual, 3)
                        clip_path = tmp / f"scene_{i}.mp4"
                        downloaded = False
                        if urls:
                            downloaded = await asyncio.to_thread(_download_clip, urls[0], clip_path)
                        if not downloaded:
                            await mirror_runtime_task_status(
                                sender_id, task_id, task_desc, "failed", "scene_unavailable", owner="video_agent"
                            )
                            downloaded = await asyncio.to_thread(
                                _generate_scene_clip,
                                visual,
                                float(scene.get("duration", 5) or 5),
                                clip_path,
                                resolution,
                            )
                        if not downloaded:
                            yield event.plain_result(
                                "这次没有拿到可用画面，视频未生成，本次次数不计。"
                            )
                            event.stop_event()
                            return
                        clip_paths.append(clip_path)

                        # TTS with voice selection (narration style by default)
                        audio_path = tmp / f"audio_{i}.mp3"
                        tts_ok = await _tts_audio_with_voice(narration, audio_path, "narration")
                        if not tts_ok:
                            await mirror_runtime_task_status(
                                sender_id, task_id, task_desc, "failed", "tts_failed", owner="video_agent"
                            )
                            yield event.plain_result(
                                "这次配音没有生成成功，视频未完成，本次次数不计。"
                            )
                            event.stop_event()
                            return
                        audio_paths.append(audio_path)

                    # Step 4: Compose final video with xfade transitions
                    output_path = self._output_root / f"agent-{uuid.uuid4().hex}.mp4"
                    ok = await asyncio.to_thread(
                        _compose_video_v2, clip_paths, audio_paths, subtitles,
                        output_path, resolution, "fade",
                    )
                    if not ok:
                        await mirror_runtime_task_status(
                            sender_id, task_id, task_desc, "failed", "quality_gate", owner="video_agent"
                        )
                        yield event.plain_result(
                            "视频质量检查没通过（黑屏或字幕渲染异常），没有发送，本次次数不计。"
                        )
                        event.stop_event()
                        return

                # Deliver
                delivery = await deliver_local_artifact(
                    event, output_path,
                    allowed_roots=[self._output_root], kind="file",
                    task_id=task_id, task_desc=task_desc,
                    task_owner="video_agent",
                )
                if delivery.delivered:
                    await mirror_runtime_task_status(
                        sender_id, task_id, task_desc, "done", f"qq:{delivery.channel}", owner="video_agent"
                    )
                    self._daily_usage[dk] = used + 1
                    try:
                        self._save_usage()
                    except OSError:
                        pass
                else:
                    await mirror_runtime_task_status(
                        sender_id, task_id, task_desc, "delivery_pending", delivery.channel, owner="video_agent"
                    )
                yield event.plain_result(
                    _video_delivery_message(title, delivery, used, daily_limit)
                )

        except Exception as exc:
            logger.warning("[VideoAgent] unexpected error: %s: %s\n%s", type(exc).__name__, exc, traceback.format_exc())
            await mirror_runtime_task_status(
                sender_id, task_id, task_desc, "failed", type(exc).__name__, owner="video_agent"
            )
            yield event.plain_result(f"视频制作过程中出错（{type(exc).__name__}: {exc}），请稍后再试。")
        event.stop_event()

    async def terminate(self):
        """Cleanup old output files."""
        try:
            now = time.time()
            for f in self._output_root.glob("agent-*.mp4"):
                if now - f.stat().st_mtime > 3 * 86400:
                    f.unlink(missing_ok=True)
        except Exception:
            pass
