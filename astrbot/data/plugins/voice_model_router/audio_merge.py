"""Safe WAV concatenation for a single QQ voice-message delivery."""

from __future__ import annotations

import os
import uuid
import wave
from pathlib import Path
from typing import Sequence


def merge_wav_files(paths: Sequence[Path], output_path: Path) -> Path:
    """Concatenate compatible PCM WAV files atomically into ``output_path``."""
    sources = [Path(path) for path in paths]
    if not sources:
        raise ValueError("at least one WAV source is required")
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.stem}.{uuid.uuid4().hex}.tmp.wav")
    expected_params: tuple[int, int, int, str, str] | None = None
    try:
        with wave.open(str(temporary), "wb") as destination:
            for source in sources:
                if source.suffix.lower() != ".wav" or not source.is_file():
                    raise ValueError("invalid WAV source")
                with wave.open(str(source), "rb") as input_file:
                    params = (
                        input_file.getnchannels(),
                        input_file.getsampwidth(),
                        input_file.getframerate(),
                        input_file.getcomptype(),
                        input_file.getcompname(),
                    )
                    if expected_params is None:
                        expected_params = params
                        destination.setparams(input_file.getparams())
                    elif params != expected_params:
                        raise ValueError("WAV formats do not match")
                    destination.writeframes(input_file.readframes(input_file.getnframes()))
        os.replace(temporary, target)
        return target
    finally:
        temporary.unlink(missing_ok=True)
