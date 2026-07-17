import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


PLUGINS_DIR = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS_DIR))

from emotional_chat.main import (  # noqa: E402
    CRISIS_CONTEXT,
    EMOTION_CONTEXT,
    TALK_SYSTEM,
    EmotionalChat,
    is_crisis_language,
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

    def is_private_chat(self):
        return True

    is_at_or_wake_command = False

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text

    def set_extra(self, *_args):
        return None


async def collect(generator):
    return [item async for item in generator]


class EmotionalChatTests(unittest.TestCase):
    def test_crisis_language_gets_a_direct_current_safety_check(self):
        for text in ("我一直在伤害自己", "我有点想自残", "我不想活了"):
            with self.subTest(text=text):
                self.assertTrue(is_crisis_language(text))
        self.assertFalse(is_crisis_language("最近有点累"))
        self.assertIn("现在是否安全", CRISIS_CONTEXT)
        self.assertIn("立即联系", CRISIS_CONTEXT)

    def test_long_talk_is_answered_as_one_contextual_reply(self):
        self.assertIn("一次说了多件事", TALK_SYSTEM)
        self.assertIn("不设固定句数", TALK_SYSTEM)
        self.assertIn("不要逐句机械答复", EMOTION_CONTEXT)

    def test_talk_model_uses_gemini_flash_for_ordinary_users(self):
        plugin = EmotionalChat.__new__(EmotionalChat)
        self.assertEqual(
            plugin._talk_model_config("ordinary"),
            (
                "http://127.0.0.1:3000/v1/chat/completions",
                "sk-gemini-vertex",
                "gemini-3.5-flash",
            ),
        )

    def test_relationship_identity_is_single_and_privacy_safe(self):
        async def scenario():
            plugin = EmotionalChat.__new__(EmotionalChat)
            replies = await collect(plugin.on_message(FakeEvent("小柠的对象是谁？", "2000000000")))
            self.assertEqual(len(replies), 1)
            self.assertIn("单身", replies[0])
            self.assertNotIn("3424575956", replies[0])

        asyncio.run(scenario())

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

    def test_private_context_has_no_legacy_partner_binding(self):
        class Request:
            system_prompt = "人设"

        async def scenario():
            plugin = EmotionalChat.__new__(EmotionalChat)
            request = Request()
            await plugin.inject_partner_context(FakeEvent("晚安", "3424575956"), request)
            self.assertIn("【小柠·私聊基础人格】", request.system_prompt)
            self.assertIn("普通网友", request.system_prompt)
            self.assertNotIn("长期伴侣", request.system_prompt)
            self.assertNotIn("3424575956", request.system_prompt)

        asyncio.run(scenario())

    def test_legacy_partner_self_query_is_not_claimed_as_task(self):
        async def scenario():
            plugin = EmotionalChat.__new__(EmotionalChat)
            replies = await collect(plugin.on_message(FakeEvent("小柠，你还认识我吗？", "3424575956")))
            self.assertEqual(replies, [])

        asyncio.run(scenario())

    def test_relationship_reply_never_contains_qq(self):
        async def scenario():
            plugin = EmotionalChat.__new__(EmotionalChat)
            replies = await collect(plugin.on_message(FakeEvent("小柠的对象是谁？", "2000000000")))
            self.assertIn("单身", replies[0])
            self.assertNotIn("3424575956", replies[0])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
