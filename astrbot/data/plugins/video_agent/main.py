"""/做视频 — one-sentence video agent. Gemini script → TTS → stock footage → FFmpeg.

Tiers:
  Ordinary: 480p ≤30s 1/day
  X:        720p ≤60s 3/day
  PRO:      1080p ≤120s 10/day

Dependencies: FFmpeg (PATH), edge-tts (pip), Pexels API key (free: pexels.com)
"""

from __future__ import annotations

import asyncio
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
    from xiaoning_runtime import ArtifactDeliveryResult, deliver_local_artifact
except ImportError:
    from data.plugins.xiaoning_runtime import ArtifactDeliveryResult, deliver_local_artifact

PROXY_CHAT = "http://127.0.0.1:3000/v1/chat/completions"
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")
PEXELS_BASE = "https://api.pexels.com/videos"

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
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(clip),
                 "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1",
                 "-c:v", "libx264", "-preset", "fast", "-crf", "28",
                 "-an", "-t", "8", str(out)],
                capture_output=True, timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if out.exists():
                normalized.append(out)

        if not normalized:
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
        # ponytail: use drawtext instead of subtitles filter — SRT path escaping
        # breaks on Windows (colons, commas, quotes). drawtext works everywhere.
        if any(subtitles):
            draw_parts: list[str] = []
            t = 0.0
            for i, sub in enumerate(subtitles):
                dur = _get_duration(normalized[i]) if i < len(normalized) else 5.0
                # Escape special chars for drawtext: ' : \
                safe = str(sub).replace("\\", "\\\\").replace(":", "\\:").replace("'", "'\\\\\\''")
                draw_parts.append(
                    f"drawtext=text='{safe}':fontsize=20:fontcolor=white:"
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
        success = result.returncode == 0 and output_path.stat().st_size > 1000
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

        yield event.plain_result(
            f"🎬 开始制作视频「{topic[:30]}」…\n"
            f"① 生成脚本 → ② 搜索素材 → ③ 合成配音 → ④ 渲染输出\n"
            f"预计 2-5 分钟，请耐心等待。"
        )

        try:
            async with self._lock:
                # Step 1: Generate script
                script = await asyncio.to_thread(
                    _generate_script, topic, max_dur, max_scenes
                )
                if not script or not script.get("scenes"):
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

                        # Search & download clip
                        urls = await asyncio.to_thread(_search_pexels, visual, 1)
                        clip_path = tmp / f"scene_{i}.mp4"
                        downloaded = False
                        if urls:
                            downloaded = await asyncio.to_thread(_download_clip, urls[0], clip_path)
                        if not downloaded:
                            # Fallback: use a black placeholder clip
                            subprocess.run(
                                ["ffmpeg", "-y", "-f", "lavfi",
                                 "-i", f"color=c=black:s=1280x720:d={scene.get('duration', 5)}",
                                 "-c:v", "libx264", "-preset", "ultrafast",
                                 str(clip_path)],
                                capture_output=True, timeout=30,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                            )
                        clip_paths.append(clip_path)

                        # TTS
                        audio_path = tmp / f"audio_{i}.mp3"
                        tts_ok = await _tts_audio(narration, audio_path)
                        if tts_ok:
                            audio_paths.append(audio_path)

                    # Step 4: Compose final video
                    output_path = self._output_root / f"agent-{uuid.uuid4().hex}.mp4"
                    ok = await asyncio.to_thread(
                        _compose_video, clip_paths, audio_paths, subtitles,
                        output_path, resolution,
                    )
                    if not ok:
                        yield event.plain_result("视频合成失败，请稍后再试。")
                        event.stop_event()
                        return

                # Deliver
                delivery = await deliver_local_artifact(
                    event, output_path,
                    allowed_roots=[self._output_root], kind="file",
                )
                self._daily_usage[dk] = used + 1
                try:
                    self._save_usage()
                except OSError:
                    pass

                if delivery.delivered:
                    suffix = "已发送"
                elif delivery.channel == "queued":
                    suffix = "已加入后台重试队列，稍后自动送达"
                else:
                    suffix = "QQ 投递失败，文件已安全保留"
                yield event.plain_result(
                    f"✅ 视频「{title}」制作完成！{suffix}。\n"
                    f"剩余次数：{daily_limit - used - 1}/{daily_limit}"
                )

        except Exception as exc:
            logger.warning("[VideoAgent] unexpected error: %s: %s\n%s", type(exc).__name__, exc, traceback.format_exc())
            yield event.plain_result(f"视频制作过程中出错（{type(exc).__name__}: {exc}），请稍后再试。")
        event.stop_event()

    async def terminate(self):
        """Cleanup old output files."""
        try:
            now = time.time()
            for f in self._output_root.glob("agent-*.mp4"):
                if now - f.stat().st_mtime > 3600:  # 1 hour
                    f.unlink(missing_ok=True)
        except Exception:
            pass
