import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


ROOT = Path(__file__).resolve().parents[1]
os.environ["ASTRBOT_ROOT"] = str(ROOT / "astrbot")
sys.path.insert(0, str(ROOT / "astrbot"))

from data.plugins.msg_reaction.main import MsgReaction, pick_reaction_emoji  # noqa: E402


class MsgReactionTests(unittest.TestCase):
    def test_only_explicit_phrases_receive_a_reaction(self):
        self.assertEqual(pick_reaction_emoji("谢谢小柠"), "76")
        self.assertIsNone(pick_reaction_emoji("今天想聊聊天"))
        self.assertIsNone(pick_reaction_emoji("宝宝我想你了"))

    def test_reaction_uses_napcat_parameter_types(self):
        async def scenario():
            action = AsyncMock()
            event = SimpleNamespace(
                bot=SimpleNamespace(call_action=action),
                message_obj=SimpleNamespace(raw_message={"message_id": 123}),
                get_message_str=lambda: "谢谢",
            )
            plugin = MsgReaction.__new__(MsgReaction)
            await plugin.add_reaction(event)
            action.assert_awaited_once_with(
                "set_msg_emoji_like", message_id="123", emoji_id="76"
            )

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
