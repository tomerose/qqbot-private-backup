import importlib.util
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from astrbot.core.star.star_handler import star_handlers_registry


PLUGINS = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS))

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "astrbot"
    / "data"
    / "plugins"
    / "deep_think"
    / "main.py"
)
SPEC = importlib.util.spec_from_file_location("qqbot_deep_think", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class DeepThinkTriggerTests(unittest.TestCase):
    def test_message_entry_is_registered_with_astrbot(self):
        handlers = star_handlers_registry.get_handlers_by_module_name(MODULE.__name__)
        self.assertIn("on_message", {handler.handler_name for handler in handlers})

    def test_commands_and_natural_language_extract_question(self):
        self.assertEqual(MODULE.extract_question("/think 为什么天空是蓝色"), "为什么天空是蓝色")
        self.assertEqual(MODULE.extract_question("/推理  比较两个方案"), "比较两个方案")
        self.assertEqual(MODULE.extract_question("小柠，深度思考：这个方案有什么风险"), "这个方案有什么风险")

    def test_empty_command_and_unrelated_message(self):
        self.assertEqual(MODULE.extract_question("/think"), "")
        self.assertIsNone(MODULE.extract_question("今天天气怎么样"))

    def test_handler_returns_only_final_answer(self):
        class Event:
            is_at_or_wake_command = False

            def is_private_chat(self):
                return True

            def get_message_str(self):
                return "/think 比较两个方案的风险"

            def plain_result(self, text):
                return text

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "choices": [{
                        "message": {
                            "content": "🤔 不应展示的思维链\n\n━━━━━━━━━━\n\n最终答案"
                        }
                    }]
                }

        async def scenario():
            plugin = MODULE.DeepThink.__new__(MODULE.DeepThink)
            plugin._pro_db = Path("unused.db")
            with patch.object(MODULE, "get_tier", return_value=MODULE.Tier.X), patch.object(
                MODULE.requests, "post", return_value=Response()
            ) as post:
                replies = [reply async for reply in plugin.on_message(Event())]
            self.assertEqual(replies[-1], "最终答案")
            self.assertNotIn("思维链", replies[-1])
            self.assertTrue(post.call_args.kwargs["json"]["thinking"])

        asyncio.run(scenario())

    def test_ordinary_user_is_rejected_without_model_call(self):
        class Event:
            is_at_or_wake_command = True

            def is_private_chat(self):
                return False

            def get_message_str(self):
                return "/think compare two plans"

            def get_sender_id(self):
                return "2000000000"

            def plain_result(self, text):
                return text

        async def scenario():
            plugin = MODULE.DeepThink.__new__(MODULE.DeepThink)
            plugin._pro_db = Path("unused.db")
            with patch.object(MODULE, "get_tier", return_value=MODULE.Tier.ORDINARY), patch.object(
                MODULE.requests, "post"
            ) as post:
                replies = [reply async for reply in plugin.on_message(Event())]
            self.assertEqual(replies, [MODULE.REQUIRED_MSG])
            post.assert_not_called()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
