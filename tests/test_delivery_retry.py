import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
os.environ["ASTRBOT_ROOT"] = str(ROOT / "astrbot")
sys.path.insert(0, str(ROOT / "astrbot" / "data" / "plugins"))

from friend_core.main import FriendCore  # noqa: E402


class _Client:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        return self.response


class _Platform:
    def __init__(self, client):
        self.client = client

    def get_client(self):
        return self.client


class _Context:
    def __init__(self, client):
        self.platform_manager = type("Platforms", (), {"platform_insts": [_Platform(client)]})()


class DeliveryRetryTests(unittest.TestCase):
    def _plugin(self, response):
        plugin = FriendCore.__new__(FriendCore)
        client = _Client(response)
        plugin.context = _Context(client)
        return plugin, client

    def test_retry_delivery_uses_native_path_and_checks_onebot_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "answer.txt"
            path.write_text("result", encoding="utf-8")
            plugin, client = self._plugin({"retcode": 0})

            delivered = asyncio.run(
                plugin._napcat_deliver_file(
                    local_path=str(path),
                    file_name=path.name,
                    kind="file",
                    sender_id="1211000567",
                    group_id="",
                )
            )

            self.assertTrue(delivered)
            self.assertEqual(client.calls[0][0], "upload_private_file")
            self.assertEqual(
                client.calls[0][1]["file"], str(path.resolve())
            )

    def test_retry_delivery_rejects_nonzero_onebot_retcode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "answer.txt"
            path.write_text("result", encoding="utf-8")
            plugin, client = self._plugin({"retcode": 1200, "wording": "blocked"})

            with patch("friend_core.main.asyncio.sleep", new=AsyncMock()):
                delivered = asyncio.run(
                    plugin._napcat_deliver_file(
                        local_path=str(path),
                        file_name=path.name,
                        kind="file",
                        sender_id="1211000567",
                        group_id="",
                    )
                )

            self.assertFalse(delivered)
            self.assertEqual(len(client.calls), 3)


if __name__ == "__main__":
    unittest.main()
