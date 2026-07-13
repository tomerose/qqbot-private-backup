import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
os.environ["ASTRBOT_ROOT"] = str(ROOT / "astrbot")
sys.path.insert(0, str(ROOT / "astrbot"))
sys.path.insert(0, str(ROOT / "astrbot" / "data" / "plugins"))

from data.plugins.search_command.main import (  # noqa: E402
    SearchCommand,
    _detect_search_mode,
    is_video_search_intent,
)


class FakeEvent:
    is_at_or_wake_command = False

    def __init__(self, text):
        self.text = text
        self.stopped = False

    def get_message_str(self):
        return self.text

    def is_private_chat(self):
        return True

    def plain_result(self, text):
        return text

    def stop_event(self):
        self.stopped = True


class SearchCommandTests(unittest.TestCase):
    def test_video_search_is_reserved_for_video_delivery(self):
        for text in ("帮我找一个姆巴佩的视频", "搜索猫咪短片", "/findvideo 梅西"):
            with self.subTest(text=text):
                self.assertTrue(is_video_search_intent(text))
        self.assertFalse(is_video_search_intent("搜索姆巴佩最近的比赛结果"))

    def test_maps_does_not_combine_two_grounding_tools(self):
        self.assertEqual(
            _detect_search_mode("附近有什么咖啡店"),
            {"google_search": False, "google_maps": True, "code_execution": False},
        )

    def test_video_request_is_not_claimed(self):
        async def scenario():
            event = FakeEvent("帮我找一个姆巴佩的视频")
            plugin = SearchCommand.__new__(SearchCommand)
            replies = [reply async for reply in plugin.on_message(event)]
            self.assertEqual(replies, [])
            self.assertFalse(event.stopped)

        asyncio.run(scenario())

    def test_regular_search_returns_grounded_source(self):
        async def scenario():
            event = FakeEvent("搜索 Gemini 最新版本")
            plugin = SearchCommand.__new__(SearchCommand)
            mocked = AsyncMock(return_value=("搜索结果", [{"title": "官方", "uri": "https://example.com"}]))
            with patch("data.plugins.search_command.main._call_proxy", mocked):
                replies = [reply async for reply in plugin.on_message(event)]
            self.assertEqual(replies[0], "正在搜索…")
            self.assertIn("https://example.com", replies[1])
            self.assertTrue(event.stopped)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
