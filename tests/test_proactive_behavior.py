import asyncio
import json
import sys
import unittest
from pathlib import Path


PLUGINS = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS))

from proactive_behavior.main import CONVERSATION_GUARD, ProactiveBehavior  # noqa: E402
from friend_core.persona_prompt import build_persona_prompt  # noqa: E402


class Event:
    def get_sender_id(self):
        return "1211000567"


class Request:
    system_prompt = "原始人设"


class ConversationGuardTests(unittest.TestCase):
    def test_persona_judgment_is_conditional_and_has_no_scripted_intimacy(self):
        prompt = build_persona_prompt(100)
        self.assertIn("有事实或逻辑依据", prompt)
        self.assertNotIn("凌晨3点被cue", prompt)
        self.assertNotIn("想你了", prompt)

    def test_runtime_config_prefers_current_context_and_disables_unsolicited_chat(self):
        config_path = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "cmd_config.json"
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        prompt = config["provider_settings"]["prompt_prefix"]

        self.assertIn("只处理用户当前这句话", prompt)
        self.assertIn("不催任务", prompt)
        self.assertNotIn("你不是在等指令", prompt)
        self.assertNotIn("astrbot_plugin_proactive_chat", config["plugin_set"])

    def test_guard_is_always_injected_and_relationship_context_never_pushes(self):
        async def scenario():
            plugin = ProactiveBehavior.__new__(ProactiveBehavior)
            plugin._state = {
                "1211000567": {
                    "last_return_gap_hours": 48,
                    "first_seen_ts": 0,
                }
            }
            request = Request()
            await plugin.inject_relationship_context(Event(), request)

            self.assertIn(CONVERSATION_GUARD, request.system_prompt)
            self.assertIn("【关系感知】", request.system_prompt)
            self.assertNotIn("可以自然问候", request.system_prompt)
            self.assertIn("只有与当前话题自然相关时才提及", request.system_prompt)
            self.assertIn("不说“赶紧”", request.system_prompt)

        asyncio.run(scenario())

    def test_existing_top_level_guard_is_not_duplicated(self):
        async def scenario():
            plugin = ProactiveBehavior.__new__(ProactiveBehavior)
            plugin._state = {}
            request = Request()
            request.system_prompt = "【小柠的最高对话规则】\n已有规则"
            await plugin.inject_relationship_context(Event(), request)
            self.assertNotIn("【小柠对话基线】", request.system_prompt)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
