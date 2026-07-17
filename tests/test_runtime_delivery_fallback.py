import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


PLUGINS = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS))

from xiaoning_runtime import deliver_local_artifact, mirror_runtime_task_status  # noqa: E402


class _Bot:
    async def call_action(self, *_args, **_kwargs):
        raise RuntimeError("native upload rejected")


class _Event:
    bot = _Bot()

    def __init__(self):
        self.sent = []

    def get_group_id(self):
        return ""

    def get_sender_id(self):
        return "1211000567"

    async def send(self, chain):
        self.sent.append(chain)
        return {"retcode": 0}


class RuntimeDeliveryFallbackTests(unittest.TestCase):
    def test_task_mirror_timeout_does_not_block_plugin_reply(self):
        async def slow_to_thread(*_args, **_kwargs):
            await asyncio.sleep(2)

        async def scenario():
            started = time.monotonic()
            with patch("xiaoning_runtime.asyncio.to_thread", new=slow_to_thread):
                await mirror_runtime_task_status("1211000567", "abc", "task", "done")
            self.assertLess(time.monotonic() - started, 1.5)

        asyncio.run(scenario())

    def test_uses_file_component_after_private_upload_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "result.txt"
            artifact.write_text("ok", encoding="utf-8")
            event = _Event()

            result = asyncio.run(
                deliver_local_artifact(event, artifact, allowed_roots=[root])
            )

            self.assertTrue(result.delivered)
            self.assertEqual(result.channel, "private_component")
            self.assertEqual(event.sent[0].chain[0].name, "result.txt")


if __name__ == "__main__":
    unittest.main()
