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
    contact_reply_for,
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
    def test_contact_and_pro_acquisition_intents_return_public_email(self):
        for text in (
            "怎么联系作者",
            "老板的联系方式",
            "Pro 怎么获取",
            "我想申请 pro 资格",
        ):
            with self.subTest(text=text):
                self.assertEqual(contact_reply_for(text), CONTACT_REPLY)

    def test_unrelated_pro_discussion_does_not_trigger(self):
        for text in ("这个 Pro 模型怎么样", "今天吃什么", "老板键是什么"):
            with self.subTest(text=text):
                self.assertIsNone(contact_reply_for(text))

    def test_handler_returns_native_result_and_stops_matching_event(self):
        async def scenario():
            plugin = ContactProInfo.__new__(ContactProInfo)
            event = FakeEvent("Pro 怎么开通")

            replies = await collect(plugin.on_message(event))

            self.assertEqual(replies, [CONTACT_REPLY])
            self.assertTrue(event.stopped)

        asyncio.run(scenario())

    def test_active_prompts_contain_contact_memory(self):
        config = json.loads(CMD_CONFIG.read_text(encoding="utf-8-sig"))
        rule = "询问联系作者、老板或获取 Pro 时"

        self.assertIn(rule, config["provider_settings"]["prompt_prefix"])
        self.assertTrue(
            any(
                persona.get("name") == "xiaoning"
                and rule in persona.get("prompt", "")
                for persona in config["persona"]
            )
        )


if __name__ == "__main__":
    unittest.main()
