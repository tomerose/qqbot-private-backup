import asyncio
import os
import sys
import unittest
from pathlib import Path


PLUGINS = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS))
os.environ.setdefault("XIAONING_SHEN_GROUP_IDS", "900000002")

from emotional_chat.main import EmotionalChat, SHEN_GROUP_MARKER  # noqa: E402


class _GroupEvent:
    def get_sender_id(self):
        return "not-a-profiled-user"

    def get_group_id(self):
        return "900000002"

    def is_private_chat(self):
        return False


class _Request:
    system_prompt = ""


class EmotionalChatPersonaTests(unittest.TestCase):
    def test_group_persona_injection_has_no_legacy_state_dependency(self):
        async def scenario():
            request = _Request()
            await EmotionalChat.inject_partner_context(object(), _GroupEvent(), request)
            self.assertIn(SHEN_GROUP_MARKER, request.system_prompt)
            self.assertIn("我是小柠", request.system_prompt)
            self.assertIn("不把“普通网友”", request.system_prompt)
            self.assertNotIn("你就是个喜欢周深的普通网友", request.system_prompt)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
