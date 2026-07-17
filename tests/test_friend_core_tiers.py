import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PLUGINS = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS))

from draw_command.pro_access import Tier  # noqa: E402
from friend_core.main import FriendCore  # noqa: E402


class Event:
    def __init__(self, sender="2000000000", *, private=True, text=""):
        self.sender = sender
        self.private = private
        self.text = text
        self.is_at_or_wake_command = False
        self.extra = {}
        self.stopped = False

    def get_sender_id(self):
        return self.sender

    def get_group_id(self):
        return "123456"

    def get_message_str(self):
        return self.text

    def is_private_chat(self):
        return self.private

    def get_extra(self, key, default=None):
        return self.extra.get(key, default)

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text


class FriendCoreTierTests(unittest.TestCase):
    def _plugin(self):
        plugin = FriendCore.__new__(FriendCore)
        plugin.enabled = True
        plugin._pro_db = Path("unused.db")
        plugin._db = None
        plugin._last_group_help_at = {}
        plugin._group_help_llm = False
        return plugin

    def test_voice_persona_uses_authoritative_tier(self):
        async def scenario():
            plugin = self._plugin()
            event = Event()
            event.extra["voice_reply_requested"] = True
            request = SimpleNamespace(system_prompt="基础")
            with patch("friend_core.main.get_tier", return_value=Tier.X):
                await plugin.inject_persona(event, request)
            self.assertIn("【当前用户资格】X", request.system_prompt)
            self.assertIn("本人记忆来个性化", request.system_prompt)

        asyncio.run(scenario())

    def test_group_help_never_offers_a_capability_above_sender_tier(self):
        async def collect(plugin, event):
            return [item async for item in plugin.offer_group_help(event)]

        event = Event(private=False, text="谁会处理这个表格文件，帮我看看")
        event.extra["_context_aware_current_message_record"] = SimpleNamespace(
            talking_to="group"
        )
        plugin = self._plugin()
        with patch("friend_core.main.get_tier", return_value=Tier.ORDINARY):
            self.assertEqual(asyncio.run(collect(plugin, event)), [])
        with patch("friend_core.main.get_tier", return_value=Tier.X):
            replies = asyncio.run(collect(plugin, event))
        self.assertEqual(len(replies), 1)
        self.assertIn("文件", replies[0])
        self.assertTrue(event.stopped)

    def test_group_help_allows_missing_context_record_for_clear_public_help(self):
        async def collect(plugin, event):
            return [item async for item in plugin.offer_group_help(event)]

        event = Event(
            private=False,
            text="\u8c01\u4f1a\u5904\u7406\u8fd9\u4e2a\u8868\u683c\u6587\u4ef6\uff0c\u5e2e\u6211\u770b\u770b",
        )
        plugin = self._plugin()
        with patch("friend_core.main.get_tier", return_value=Tier.X):
            replies = asyncio.run(collect(plugin, event))
        self.assertEqual(len(replies), 1)
        self.assertTrue(event.stopped)


if __name__ == "__main__":
    unittest.main()
