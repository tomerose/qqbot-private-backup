import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


PLUGINS_DIR = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS_DIR))

from emotional_chat.main import EmotionalChat  # noqa: E402


class FakeEvent:
    def __init__(self, text):
        self.text = text
        self.stopped = False

    def get_message_str(self):
        return self.text

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text

    def add_context(self, *_args):
        return None

    def set_extra(self, *_args):
        return None


async def collect(generator):
    return [item async for item in generator]


class EmotionalChatTests(unittest.TestCase):
    def test_talk_claims_the_event_and_returns_one_conversation(self):
        async def scenario():
            plugin = EmotionalChat.__new__(EmotionalChat)
            event = FakeEvent("/talk 今天有点累")
            with patch(
                "emotional_chat.main.asyncio.to_thread",
                new=AsyncMock(return_value="哎，听着就挺累的。"),
            ):
                replies = await collect(plugin.on_message(event))
            self.assertTrue(event.stopped)
            self.assertEqual(
                replies,
                ["（放下手边的事，认真听你说…）", "哎，听着就挺累的。"],
            )

        asyncio.run(scenario())

    def test_talk_prefix_does_not_capture_regular_chat(self):
        async def scenario():
            plugin = EmotionalChat.__new__(EmotionalChat)
            event = FakeEvent("/talkative 是什么意思")
            self.assertEqual(await collect(plugin.on_message(event)), [])
            self.assertFalse(event.stopped)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
