"""FFmpeg compositing engine — xfade transitions, LUT, Ken Burns, subtitles."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── Preset tables ───────────────────────────────────────────

XFADE_PRESETS = {
    "fade": "fade",
    "slide": "slideright",
    "pixel": "pixelize",
    "dissolve": "fadegrays",
    "wipe": "wiperight",
    "zoom": "zoomin",
}

DURATION_PATTERN = re.compile(r"duration=([0-9]+(?:\.[0-9]+)?)")

RESOLUTIONS = {
    "480p": (854, 480),
    "720p": (1280, 720),
    "1080p": (1920, 1080),
}

VIDEO_CREATION_FLAGS = (
    subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
)

BLACK_DURATION_RE = re.compile(r"black_duration:([0-9]+(?:\.[0-9]+)?)")


@dataclass
class EditConfig:
    resolution: str = "720p"
    transition: str = "fade"
    transition_dur: float = 0.3
    subtitle_enabled: bool = True
    motion: str = "ken_burns"  # ken_burns | none
    fps: int = 25


@dataclass
class Timeline:
    clips: list[Path] = field(default_factory=list)
    audio_paths: list[Path] = field(default_factory=list)
    subtitles: list[str] = field(default_factory=list)
    durations: list[float] = field(default_factory=list)


class EditEngine:
    """Enhanced FFmpeg compositing with transitions, motion, and subtitles."""

    @staticmethod
    def compose(timeline: Timeline, output_path: Path,
                config: EditConfig | None = None) -> bool:
        """Compose final video from timeline clips with effects.

        Falls back to simple concat if xfade fails.
        """
        if config is None:
            config = EditConfig()
        if not timeline.clips:
            return False

        width, height = RESOLUTIONS.get(config.resolution, (1280, 720))

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            # Step 1: Normalize clips (scale + crop + optional Ken Burns)
            normalized: list[Path] = []
            for i, clip in enumerate(timeline.clips):
                dur = timeline.durations[i] if i < len(timeline.durations) else 5.0
                out = tmp / f"norm_{i}.mp4"
                if not EditEngine._normalize_clip(clip, out, dur, width, height,
                                                  config.motion != "none", config.fps):
                    return EditEngine._simple_compose(
                        timeline, output_path, width, height)
                normalized.append(out)

            if len(normalized) != len(timeline.clips):
                return EditEngine._simple_compose(
                    timeline, output_path, width, height)

            # Step 2: Build xfade filtergraph (or simple path for single clip)
            if len(normalized) == 1:
                return EditEngine._single_clip_compose(
                    normalized[0], timeline, output_path, width, height, tmp)

            ok = EditEngine._xfade_compose(
                normalized, timeline, output_path, width, height, config, tmp)
            if ok:
                return True
            return EditEngine._simple_compose(
                timeline, output_path, width, height)

    # ── Internal steps ──────────────────────────────────────

    @staticmethod
    def _normalize_clip(clip: Path, out: Path, duration: float,
                        width: int, height: int, motion: bool, fps: int) -> bool:
        if motion:
            frames = max(fps, int(duration * fps))
            vf = (
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},"
                f"zoompan=z='min(zoom+0.0015,1.06)':d={frames}:s={width}x{height}:fps={fps},"
                "format=yuv420p"
            )
        else:
            vf = (
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p"
            )
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(clip), "-vf", vf,
             "-c:v", "libx264", "-preset", "fast", "-crf", "25",
             "-an", "-t", f"{duration:.2f}", str(out)],
            capture_output=True, timeout=90, creationflags=VIDEO_CREATION_FLAGS,
        )
        return result.returncode == 0 and out.is_file() and out.stat().st_size > 1000

    @staticmethod
    def _xfade_compose(normalized: list[Path], timeline: Timeline,
                       output_path: Path, width: int, height: int,
                       config: EditConfig, tmp: Path) -> bool:
        xfade_type = XFADE_PRESETS.get(config.transition, "fade")
        td = config.transition_dur

        # Build xfade filtergraph
        filter_parts: list[str] = []
        last_label = "0"
        accumulated = 0.0

        for i in range(len(normalized)):
            dur = EditEngine._get_duration(normalized[i])
            if i == 0:
                accumulated += dur
                continue
            offset = accumulated - td
            next_label = f"x{i}" if i < len(normalized) - 1 else "out"
            filter_parts.append(
                f"[{last_label}][{i}]xfade=transition={xfade_type}:"
                f"duration={td}:offset={offset:.2f}[{next_label}]"
            )
            last_label = next_label
            accumulated += dur - td

        vf_filter = ";".join(filter_parts)

        # Merge audio
        audio_concat = tmp / "audio_mix.mp3"
        has_audio = timeline.audio_paths and all(
            p and p.exists() for p in timeline.audio_paths)
        if has_audio:
            audio_inputs = []
            for p in timeline.audio_paths:
                if p and p.exists():
                    audio_inputs.extend(["-i", str(p)])
            if audio_inputs:
                count = len(audio_inputs) // 2
                subprocess.run(
                    ["ffmpeg", "-y", *audio_inputs,
                     "-filter_complex", f"concat=n={count}:v=0:a=1",
                     "-ac", "1", str(audio_concat)],
                    capture_output=True, timeout=60,
                    creationflags=VIDEO_CREATION_FLAGS,
                )

        # Assemble command
        inputs = []
        for p in normalized:
            inputs.extend(["-i", str(p)])
        cmd = ["ffmpeg", "-y", *inputs,
               "-filter_complex", vf_filter,
               "-map", f"[{last_label}]"]

        if audio_concat.exists():
            cmd.extend(["-i", str(audio_concat), "-map", "1:a:0"])
            codec_args = ["-c:v", "libx264", "-preset", "fast", "-crf", "26",
                          "-c:a", "aac", "-b:a", "96k",
                          "-shortest", "-movflags", "+faststart"]
        else:
            codec_args = ["-c:v", "libx264", "-preset", "fast", "-crf", "26",
                          "-movflags", "+faststart"]

        # Subtitle burn-in
        if config.subtitle_enabled and any(timeline.subtitles):
            font = EditEngine._cjk_font()
            if font:
                draw_parts: list[str] = []
                t = 0.0
                for i, sub in enumerate(timeline.subtitles):
                    dur_val = timeline.durations[i] if i < len(timeline.durations) else 5.0
                    sub_file = tmp / f"sub_{i}.txt"
                    sub_file.write_text(str(sub), encoding="utf-8")
                    font_arg = EditEngine._escape_path(font)
                    sub_arg = EditEngine._escape_path(sub_file)
                    draw_parts.append(
                        f"drawtext=fontfile='{font_arg}':textfile='{sub_arg}':"
                        f"fontsize=28:fontcolor=white:"
                        f"x=(w-text_w)/2:y=h-th-60:"
                        f"box=1:boxcolor=black@0.5:boxborderw=6:"
                        f"enable='between(t,{t:.1f},{t + dur_val:.1f})'"
                    )
                    t += dur_val - td
                sub_vf = ",".join(draw_parts)
                vf_filter = f"{vf_filter};[{last_label}]{sub_vf}[final]"
                # Update map
                for j, part in enumerate(cmd):
                    if part == f"[{last_label}]":
                        cmd[j] = "[final]"
                        break

        cmd.extend(codec_args)
        cmd.append(str(output_path))

        result = subprocess.run(
            cmd, capture_output=True, timeout=300,
            creationflags=VIDEO_CREATION_FLAGS,
        )
        return (
            result.returncode == 0
            and output_path.is_file()
            and output_path.stat().st_size > 1000
            and EditEngine._is_usable(output_path)
        )

    @staticmethod
    def _simple_compose(timeline: Timeline, output_path: Path,
                        width: int, height: int) -> bool:
        """Simple concat-based compose — reliable fallback."""
        clips = timeline.clips
        if not clips:
            return False

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            # Normalize
            normalized: list[Path] = []
            for i, clip in enumerate(clips):
                out = tmp / f"n_{i}.mp4"
                result = subprocess.run(
                    ["ffmpeg", "-y", "-i", str(clip),
                     "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1",
                     "-c:v", "libx264", "-preset", "fast", "-crf", "28",
                     "-an", "-t", "8", str(out)],
                    capture_output=True, timeout=60,
                    creationflags=VIDEO_CREATION_FLAGS,
                )
                if result.returncode == 0 and out.is_file() and out.stat().st_size > 1000:
                    normalized.append(out)

            if not normalized:
                return False

            # Concat list
            concat_file = tmp / "concat.txt"
            concat_file.write_text(
                "\n".join(f"file '{p.as_posix()}'" for p in normalized),
                encoding="utf-8")

            # Audio merge
            audio_concat = tmp / "audio.mp3"
            has_audio = timeline.audio_paths and all(
                p and p.exists() for p in timeline.audio_paths if p)
            if has_audio:
                valid_audio = [p for p in timeline.audio_paths if p and p.exists()]
                if valid_audio:
                    audio_inputs = []
                    for p in valid_audio:
                        audio_inputs.extend(["-i", str(p)])
                    subprocess.run(
                        ["ffmpeg", "-y", *audio_inputs,
                         "-filter_complex", f"concat=n={len(valid_audio)}:v=0:a=1",
                         "-ac", "1", str(audio_concat)],
                        capture_output=True, timeout=60,
                        creationflags=VIDEO_CREATION_FLAGS,
                    )

            # Final compose
            cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                   "-i", str(concat_file)]
            if audio_concat.exists():
                cmd.extend(["-i", str(audio_concat)])
                cmd.extend(["-c:v", "libx264", "-preset", "fast", "-crf", "26",
                            "-c:a", "aac", "-b:a", "96k",
                            "-shortest", "-movflags", "+faststart"])
            else:
                cmd.extend(["-c:v", "libx264", "-preset", "fast", "-crf", "26",
                            "-movflags", "+faststart"])

            # Subtitle burn-in
            if any(timeline.subtitles):
                font = EditEngine._cjk_font()
                if font:
                    draw_parts: list[str] = []
                    t = 0.0
                    for i, sub in enumerate(timeline.subtitles):
                        dur_val = timeline.durations[i] if i < len(timeline.durations) else 5.0
                        sub_file = tmp / f"s_{i}.txt"
                        sub_file.write_text(str(sub), encoding="utf-8")
                        font_arg = EditEngine._escape_path(font)
                        sub_arg = EditEngine._escape_path(sub_file)
                        draw_parts.append(
                            f"drawtext=fontfile='{font_arg}':textfile='{sub_arg}':"
                            f"fontsize=28:fontcolor=white:"
                            f"x=(w-text_w)/2:y=h-th-60:"
                            f"box=1:boxcolor=black@0.5:boxborderw=6:"
                            f"enable='between(t,{t:.1f},{t + dur_val:.1f})'"
                        )
                        t += dur_val
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
                creationflags=VIDEO_CREATION_FLAGS,
            )
            return (
                result.returncode == 0
                and output_path.is_file()
                and output_path.stat().st_size > 1000
                and EditEngine._is_usable(output_path)
            )

    @staticmethod
    def _single_clip_compose(normalized: Path, timeline: Timeline,
                             output_path: Path, width: int, height: int,
                             tmp: Path) -> bool:
        """Compose with a single clip — no transition needed."""
        audio_concat = tmp / "audio.mp3"
        has_audio = timeline.audio_paths and all(
            p and p.exists() for p in timeline.audio_paths if p)
        if has_audio:
            valid_audio = [p for p in timeline.audio_paths if p and p.exists()]
            if valid_audio:
                audio_inputs = []
                for p in valid_audio:
                    audio_inputs.extend(["-i", str(p)])
                subprocess.run(
                    ["ffmpeg", "-y", *audio_inputs,
                     "-filter_complex", f"concat=n={len(valid_audio)}:v=0:a=1",
                     "-ac", "1", str(audio_concat)],
                    capture_output=True, timeout=60,
                    creationflags=VIDEO_CREATION_FLAGS,
                )

        cmd = ["ffmpeg", "-y", "-i", str(normalized)]
        if audio_concat.exists():
            cmd.extend(["-i", str(audio_concat)])
            cmd.extend(["-c:v", "libx264", "-preset", "fast", "-crf", "26",
                        "-c:a", "aac", "-b:a", "96k",
                        "-shortest", "-movflags", "+faststart"])
        else:
            cmd.extend(["-c:v", "libx264", "-preset", "fast", "-crf", "26",
                        "-movflags", "+faststart"])

        cmd.append(str(output_path))
        result = subprocess.run(
            cmd, capture_output=True, timeout=180,
            creationflags=VIDEO_CREATION_FLAGS,
        )
        return (
            result.returncode == 0
            and output_path.is_file()
            and output_path.stat().st_size > 1000
        )

    # ── Helpers ─────────────────────────────────────────────

    @staticmethod
    def _get_duration(path: Path) -> float:
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(path)],
                capture_output=True, text=True, timeout=10,
                creationflags=VIDEO_CREATION_FLAGS,
            )
            return float(result.stdout.strip() or 5.0)
        except (ValueError, subprocess.SubprocessError, FileNotFoundError):
            return 5.0

    @staticmethod
    def _cjk_font() -> Path | None:
        candidates = (
            Path("C:/Windows/Fonts/msyh.ttc"),
            Path("C:/Windows/Fonts/msyhbd.ttc"),
            Path("C:/Windows/Fonts/simhei.ttf"),
            Path("C:/Windows/Fonts/simsun.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        )
        return next((p for p in candidates if p.is_file()), None)

    @staticmethod
    def _escape_path(path: Path) -> str:
        return path.resolve(strict=True).as_posix().replace(":", r"\:").replace("'", r"\'")

    @staticmethod
    def _is_usable(path: Path) -> bool:
        dur = EditEngine._get_duration(path)
        if dur < 1 or not path.is_file() or path.stat().st_size <= 1000:
            return False
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-i", str(path),
                 "-vf", "blackdetect=d=0.5:pix_th=0.10",
                 "-an", "-f", "null",
                 "NUL" if os.name == "nt" else "/dev/null"],
                capture_output=True, timeout=90,
                creationflags=VIDEO_CREATION_FLAGS,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode != 0:
            return False
        stderr = result.stderr.decode("utf-8", errors="replace")
        black = sum(float(v) for v in BLACK_DURATION_RE.findall(stderr))
        ratio = min(1.0, black / dur)
        return ratio < 0.90


__all__ = ["EditEngine", "EditConfig", "Timeline", "XFADE_PRESETS"]
