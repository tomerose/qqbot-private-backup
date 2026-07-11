import sys
import tempfile
import unittest
import wave
from pathlib import Path


PLUGINS_DIR = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS_DIR))

from voice_model_router.audio_merge import merge_wav_files  # noqa: E402


def write_wav(path: Path, frames: bytes, *, rate: int = 8000) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(frames)


class VoiceAudioMergeTests(unittest.TestCase):
    def test_merges_ordered_wav_chunks_into_one_playable_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.wav"
            second = root / "second.wav"
            merged = root / "merged.wav"
            write_wav(first, b"\x01\x00" * 4)
            write_wav(second, b"\x02\x00" * 6)

            result = merge_wav_files([first, second], merged)

            self.assertEqual(result, merged)
            with wave.open(str(merged), "rb") as handle:
                self.assertEqual(handle.getnframes(), 10)
                self.assertEqual(handle.readframes(10), b"\x01\x00" * 4 + b"\x02\x00" * 6)

    def test_rejects_mismatched_audio_formats_without_writing_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.wav"
            second = root / "second.wav"
            merged = root / "merged.wav"
            write_wav(first, b"\x01\x00", rate=8000)
            write_wav(second, b"\x02\x00", rate=16000)

            with self.assertRaises(ValueError):
                merge_wav_files([first, second], merged)

            self.assertFalse(merged.exists())


if __name__ == "__main__":
    unittest.main()
