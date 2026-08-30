import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


ROOT = Path(__file__).resolve().parents[1]
os.environ["ASTRBOT_ROOT"] = str(ROOT / "astrbot")
sys.path.insert(0, str(ROOT / "astrbot"))

from data.plugins.like_back.main import LikeBack  # noqa: E402


def profile(latest_time=100, count=2):
    return {
        "voteInfo": {
            "userInfos": [
                {"uin": 123456, "nick": "测试用户", "latestTime": latest_time, "count": count}
            ]
        }
    }


class LikeBackTests(unittest.TestCase):
    def test_reads_current_napcat_profile_shape(self):
        self.assertEqual(LikeBack._like_entries(profile())[0]["uin"], 123456)

    def test_first_scan_only_establishes_a_baseline(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                api = SimpleNamespace(call_action=AsyncMock(return_value=profile()))
                plugin = LikeBack.__new__(LikeBack)
                plugin.config = {}
                plugin._store = Path(tmp) / "likes.json"
                plugin._known_likes = set()
                plugin._initialized = False
                plugin._get_api = AsyncMock(return_value=api)
                await plugin._scan_and_like_back()
                self.assertEqual(api.call_action.await_count, 1)
                self.assertTrue(plugin._store.is_file())

        asyncio.run(scenario())

    def test_new_like_is_returned_and_owner_message_is_plain_text(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                calls = []

                async def call_action(action, **params):
                    calls.append((action, params))
                    return profile(200, 3) if action == "get_profile_like" else {}

                api = SimpleNamespace(call_action=call_action)
                plugin = LikeBack.__new__(LikeBack)
                plugin.config = {"owner_qq": "999"}
                plugin._store = Path(tmp) / "likes.json"
                plugin._known_likes = {"123456:100:2"}
                plugin._initialized = True
                plugin._get_api = AsyncMock(return_value=api)
                await plugin._scan_and_like_back()
                self.assertEqual(calls[1], ("send_like", {"user_id": "123456", "times": 1}))
                self.assertIsInstance(calls[2][1]["message"], str)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
