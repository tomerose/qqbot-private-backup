import asyncio
import json
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = PROJECT_ROOT / "astrbot" / "data" / "plugins"
CMD_CONFIG = PROJECT_ROOT / "astrbot" / "data" / "cmd_config.json"
sys.path.insert(0, str(PLUGINS_DIR))

from contact_pro_info.main import (  # noqa: E402
    CONTACT_REPLY,
    ContactProInfo,
    MUSIC_GUIDE,
    contact_reply_for,
    version_reply_for,
    VERSION_REPLY,
    PRO_APPLICATION_GUIDE,
)


class FakeEvent:
    def __init__(self, text: str):
        self.text = text
        self.stopped = False

    def get_message_str(self):
        return self.text

    def plain_result(self, text: str):
        return text

    def stop_event(self):
        self.stopped = True


async def collect(generator):
    return [item async for item in generator]


class ContactProInfoTests(unittest.TestCase):
    def test_public_help_describes_the_explicit_music_paths(self):
        self.assertIn("\u7f51\u6613\u4e91\u5361", MUSIC_GUIDE)
        self.assertIn("\u539f\u521b\u6b4c\u66f2", VERSION_REPLY)

    def test_version_questions_return_user_facing_feature_summary(self):
        for text in (
            "普通版和Pro有什么区别",
            "Pro版功能",
            "小柠能做什么",
        ):
            with self.subTest(text=text):
                self.assertEqual(version_reply_for(text), VERSION_REPLY)

    def test_unrelated_pro_model_question_does_not_return_version_summary(self):
        self.assertIsNone(version_reply_for("这个 Pro 模型怎么样"))

    def test_contact_and_pro_acquisition_intents_return_public_email(self):
        for text in (
            "怎么联系作者",
            "老板的联系方式",
        ):
            with self.subTest(text=text):
                self.assertEqual(contact_reply_for(text), CONTACT_REPLY)
        for text in (
            "Pro 怎么获取",
            "我想申请 pro 资格",
        ):
            with self.subTest(text=text):
                self.assertEqual(contact_reply_for(text), PRO_APPLICATION_GUIDE)

    def test_unrelated_pro_discussion_does_not_trigger(self):
        for text in ("这个 Pro 模型怎么样", "今天吃什么", "老板键是什么"):
            with self.subTest(text=text):
                self.assertIsNone(contact_reply_for(text))

    def test_handler_returns_native_result_and_stops_matching_event(self):
        async def scenario():
            plugin = ContactProInfo.__new__(ContactProInfo)
            event = FakeEvent("普通版和 Pro 有什么区别")

            replies = await collect(plugin.on_message(event))

            self.assertEqual(replies, [VERSION_REPLY])
            self.assertTrue(event.stopped)

        asyncio.run(scenario())

    def test_active_prompts_contain_contact_memory(self):
        config = json.loads(CMD_CONFIG.read_text(encoding="utf-8-sig"))
        rule = "询问联系作者或老板时"

        self.assertIn(rule, config["provider_settings"]["prompt_prefix"])
        self.assertIn("Pro 在此基础上支持 AI 作图", config["provider_settings"]["prompt_prefix"])
        self.assertIn("Agent 任务", config["provider_settings"]["prompt_prefix"])
        self.assertTrue(
            any(
                persona.get("name") == "xiaoning"
                and "询问联系作者" in persona.get("prompt", "")
                and "公开邮箱" in persona.get("prompt", "")
                for persona in config["persona"]
            )
        )


if __name__ == "__main__":
    unittest.main()
