"""Voice engine — multi-voice TTS + background music mixing."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

# edge-tts built-in voices — zero cost, always available
VOICE_MAP = {
    "narration": "zh-CN-YunxiNeural",       # 沉稳男旁白
    "female": "zh-CN-XiaoxiaoNeural",       # 活泼女声
    "male": "zh-CN-YunyangNeural",          # 新闻男声
    "storytelling": "zh-CN-XiaoyiNeural",   # 讲故事
    "gentle": "zh-CN-XiaochenNeural",       # 温柔女声
    "default": "zh-CN-XiaoxiaoNeural",
}


class VoiceEngine:
    """TTS generation per scene with voice selection and audio mixing."""

    def __init__(self):
        self._voice = VOICE_MAP["narration"]

    def set_voice(self, style: str) -> None:
        self._voice = VOICE_MAP.get(style, VOICE_MAP["default"])

    async def generate(self, text: str, output_path: Path,
                       voice: str | None = None) -> bool:
        """Generate TTS audio for one text segment.

        Args:
            text: Chinese text to speak
            output_path: Path for output .mp3 file
            voice: Override voice, uses instance default if None
        Returns: True on success
        """
        voice_name = voice or self._voice
        if voice_name not in VOICE_MAP.values():
            voice_name = VOICE_MAP["default"]

        try:
            proc = await asyncio.create_subprocess_exec(
                "edge-tts", "--voice", voice_name,
                "--text", text, "--write-media", str(output_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if output_path.stat().st_size > 100:
                return True
        except Exception:
            pass

        # Fallback to default voice
        if voice_name != VOICE_MAP["default"]:
            return await self.generate(text, output_path, VOICE_MAP["default"])
        return False

    async def generate_all(self, segments: list[str],
                           output_dir: Path) -> list[Path]:
        """Generate TTS for all text segments. Returns list of audio paths."""
        paths: list[Path] = []
        for i, text in enumerate(segments):
            out = output_dir / f"voice_{i}.mp3"
            ok = await self.generate(text, out)
            paths.append(out if ok else None)  # type: ignore[arg-type]
        return paths

    @staticmethod
    def mix_audio(audio_paths: list[Path], output_path: Path) -> bool:
        """Concatenate multiple audio files into one. Returns success."""
        valid = [p for p in audio_paths if p and p.exists() and p.stat().st_size > 100]
        if not valid:
            return False
        if len(valid) == 1:
            valid[0].rename(output_path)
            return True

        audio_inputs = []
        for p in valid:
            audio_inputs.extend(["-i", str(p)])
        result = subprocess.run(
            ["ffmpeg", "-y", *audio_inputs,
             "-filter_complex", f"concat=n={len(valid)}:v=0:a=1",
             "-ac", "1", str(output_path)],
            capture_output=True, timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW if __import__("os").name == "nt" else 0,
        )
        return result.returncode == 0 and output_path.stat().st_size > 100

    @staticmethod
    def get_duration(path: Path) -> float:
        """Get audio duration in seconds."""
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(path)],
                capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if __import__("os").name == "nt" else 0,
            )
            return float(result.stdout.strip() or 0)
        except (ValueError, subprocess.SubprocessError, FileNotFoundError):
            return 5.0


__all__ = ["VoiceEngine", "VOICE_MAP"]
