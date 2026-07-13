import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


PLUGINS_DIR = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS_DIR))

from emotional_chat.main import (  # noqa: E402
    EMOTION_CONTEXT,
    PARTNER_CONTEXT,
    PARTNER_NAME,
    EmotionalChat,
)


class FakeEvent:
    def __init__(self, text, sender_id=""):
        self.text = text
        self.sender_id = sender_id
        self.stopped = False

    def get_message_str(self):
        return self.text

    def get_sender_id(self):
        return self.sender_id

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text

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

    def test_emotion_context_uses_llm_request_not_legacy_event_api(self):
        class Request:
            system_prompt = "\u4eba\u8bbe"

        async def scenario():
            plugin = EmotionalChat.__new__(EmotionalChat)
            request = Request()
            await plugin.inject_emotion_context(FakeEvent("\u6211\u6709\u70b9 emo"), request)
            await plugin.inject_emotion_context(FakeEvent("\u6211\u6709\u70b9 emo"), request)
            self.assertEqual(request.system_prompt.count("\u3010\u60c5\u7eea\u966a\u4f34\u3011"), 1)
            self.assertIn(EMOTION_CONTEXT, request.system_prompt)

        asyncio.run(scenario())

    def test_partner_uses_private_context_without_exposing_qq(self):
        class Request:
            system_prompt = "人设"

        async def scenario():
            plugin = EmotionalChat.__new__(EmotionalChat)
            request = Request()
            await plugin.inject_partner_context(FakeEvent("晚安", "3424575956"), request)
            self.assertIn(PARTNER_CONTEXT, request.system_prompt)
            self.assertNotIn("3424575956", request.system_prompt)

        asyncio.run(scenario())

    def test_partner_identity_reply_never_contains_qq(self):
        async def scenario():
            plugin = EmotionalChat.__new__(EmotionalChat)
            replies = await collect(plugin.on_message(FakeEvent("小柠的对象是谁？", "2000000000")))
            self.assertEqual(replies, [f"小柠的对象是{PARTNER_NAME}呀。"])
            self.assertNotIn("3424575956", replies[0])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
