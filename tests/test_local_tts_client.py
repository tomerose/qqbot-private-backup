import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_DIR = Path(r"D:\Claudecoda学习\qqbot\astrbot\data\plugins\voice_model_router")
sys.path.insert(0, str(PLUGIN_DIR))

from local_tts_client import LocalTTSClient  # noqa: E402


class LocalTTSClientTests(unittest.TestCase):
    def test_non_loopback_endpoint_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "环回"):
                LocalTTSClient("http://0.0.0.0:8766", "token", Path(tmp))
            with self.assertRaisesRegex(ValueError, "环回"):
                LocalTTSClient("https://example.com/tts", "token", Path(tmp))

    def test_primary_failure_uses_melo_fallback(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                fallback = root / "fallback.wav"
                fallback.write_bytes(b"RIFF-audio")
                calls = []

                async def transport(endpoint, payload, headers, timeout):
                    calls.append((payload["engine"], headers["X-Local-TTS-Token"]))
                    if payload["engine"] == "gpt_sovits":
                        raise TimeoutError("private text must not be included in error")
                    return {"path": str(fallback)}

                client = LocalTTSClient(
                    "http://127.0.0.1:8766", "runtime-token", root, transport=transport
                )
                result = await client.synthesize("你好")
                self.assertEqual(result, fallback.resolve())
                self.assertEqual(calls, [("gpt_sovits", "runtime-token"), ("melo", "runtime-token")])

        asyncio.run(scenario())

    def test_outside_symlink_and_missing_audio_are_rejected(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "audio"
                root.mkdir()
                outside = Path(tmp) / "outside.wav"
                outside.write_bytes(b"RIFF")

                async def transport(endpoint, payload, headers, timeout):
                    return {"path": str(outside)}

                client = LocalTTSClient(
                    "http://127.0.0.1:8766", "token", root, transport=transport
                )
                self.assertIsNone(await client.synthesize("你好"))

        asyncio.run(scenario())

    def test_empty_token_and_oversized_text_are_rejected_before_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "令牌"):
                LocalTTSClient("http://127.0.0.1:8766", "", Path(tmp))

            called = False

            async def transport(endpoint, payload, headers, timeout):
                nonlocal called
                called = True
                return {}

            client = LocalTTSClient(
                "http://127.0.0.1:8766", "token", Path(tmp), transport=transport
            )
            with self.assertRaisesRegex(ValueError, "过长"):
                asyncio.run(client.synthesize("x" * 601))
            self.assertFalse(called)


if __name__ == "__main__":
    unittest.main()
