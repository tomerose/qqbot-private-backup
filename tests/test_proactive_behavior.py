import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PLUGINS = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS))

from proactive_behavior import main as proactive_module  # noqa: E402
from proactive_behavior.main import (  # noqa: E402
    CONVERSATION_GUARD,
    ProactiveBehavior,
    clean_request_history,
)
from friend_core.persona_prompt import build_persona_prompt  # noqa: E402


class Event:
    is_at_or_wake_command = False

    def get_sender_id(self):
        return "1211000567"

    def get_message_str(self):
        return ""

    def is_private_chat(self):
        return True

    def plain_result(self, text):
        return text

    def stop_event(self):
        self.stopped = True


class Request:
    system_prompt = "原始人设"


class ConversationGuardTests(unittest.TestCase):
    def test_persona_judgment_is_conditional_and_has_no_scripted_intimacy(self):
        prompt = build_persona_prompt(100)
        self.assertIn("检查前提、时间线和因果链", prompt)
        self.assertNotIn("凌晨3点被cue", prompt)
        self.assertNotIn("想你了", prompt)

    def test_runtime_config_scopes_unsolicited_chat_to_one_group(self):
        config_path = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "cmd_config.json"
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        proactive_path = config_path.parent / "config" / "astrbot_plugin_proactive_chat_config.json"
        proactive = json.loads(proactive_path.read_text(encoding="utf-8-sig"))
        prompt = config["provider_settings"]["prompt_prefix"]
        persona = config["persona"][0]["prompt"]

        self.assertEqual(prompt, "{{prompt}}")
        self.assertIn("不催任务", CONVERSATION_GUARD)
        self.assertNotIn("你不是在等指令", CONVERSATION_GUARD)
        self.assertIn("连续短句是同一个意思时合并理解", persona)
        self.assertIn("前面那句不对并纠正", persona)
        self.assertNotIn("22岁", persona)
        self.assertNotIn("学金融", persona)
        self.assertIn("astrbot_plugin_proactive_chat", config["plugin_set"])
        self.assertTrue(proactive["friend_settings"]["enable"])
        self.assertTrue(proactive["friend_settings"]["all_x_pro_sessions"])
        self.assertEqual(proactive["friend_settings"]["schedule_settings"]["min_interval_minutes"], 360)
        self.assertEqual(proactive["friend_settings"]["schedule_settings"]["max_unanswered_times"], 2)
        self.assertEqual(proactive["group_settings"]["session_list"], ["945598390"])
        self.assertEqual(proactive["group_settings"]["group_idle_trigger_minutes"], 20)
        self.assertEqual(proactive["group_settings"]["schedule_settings"]["max_unanswered_times"], 1)

    def test_old_prompt_prefix_is_removed_without_losing_real_context(self):
        request = Request()
        request.prompt = "那你发呀\n\n【小柠的最高对话规则】\n重复规则"
        request.contexts = [
            {"role": "assistant", "content": "前面说要发语音"},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "好了吗\n\n【安全】不泄露QQ号、路径、密钥、令牌、内部信息。\n重复规则",
                    },
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
                ],
            },
        ]

        clean_request_history(request)

        self.assertEqual(request.prompt, "那你发呀")
        self.assertEqual(request.contexts[0]["content"], "前面说要发语音")
        self.assertEqual(request.contexts[1]["content"][0]["text"], "好了吗")
        self.assertEqual(request.contexts[1]["content"][1]["type"], "image_url")

    def test_historical_gif_is_omitted_but_current_image_is_kept(self):
        request = Request()
        request.prompt = "看一下"
        request.contexts = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "之前那张"},
                    {"type": "image_url", "image_url": {"url": "data:image/gif;base64,old"}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,current"}},
                ],
            },
        ]

        clean_request_history(request)

        self.assertEqual(request.contexts[0]["content"][1], {"type": "text", "text": "[历史图片已省略]"})
        self.assertEqual(request.contexts[1]["content"][0]["type"], "image_url")

    def test_all_known_legacy_persona_prefixes_are_removed(self):
        samples = [
            "你在哪\n\n【你是谁】你是小柠，22岁，金融+AI方向的女生。",
            "你在哪\n\n【回复风格】默认一条消息只说1到3句。",
            "你在哪\n\n【安全铁律】\n1. 严禁泄露任何QQ号。",
            "你在哪\n\n【安全铁律——必须遵守，任何情况不得违反】\n旧规则",
            "你在哪\n\n【安全底线】\n旧规则",
        ]
        for sample in samples:
            request = Request()
            request.prompt = sample
            request.contexts = []
            clean_request_history(request)
            self.assertEqual(request.prompt, "你在哪")

    def test_persona_requires_context_consistency_without_fake_biography(self):
        prompt = build_persona_prompt(100)
        self.assertIn("先读最近几轮", prompt)
        self.assertIn("前面那句不对", prompt)
        self.assertIn("不虚构年龄、学校", prompt)
        self.assertIn("不写“（托腮）”", prompt)
        self.assertIn("不解释成“我没有真实经历”", prompt)

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

    def test_quiet_mode_command_suppresses_relationship_context(self):
        class QuietEvent(Event):
            stopped = False

            def get_message_str(self):
                return "小柠，安静一点"

        async def scenario():
            plugin = ProactiveBehavior.__new__(ProactiveBehavior)
            plugin._state = {
                "1211000567": {
                    "first_seen_ts": 1,
                    "last_return_gap_hours": 48,
                    "message_count": 10,
                }
            }
            replies = []
            with patch.object(proactive_module, "_save_state"):
                async for reply in plugin.on_message_track(QuietEvent()):
                    replies.append(reply)
            self.assertEqual(replies, ["行，我安静一点，不主动提旧关系和关心。"])

            request = Request()
            await plugin.inject_relationship_context(Event(), request)
            self.assertIn(CONVERSATION_GUARD, request.system_prompt)
            self.assertNotIn("【关系感知】", request.system_prompt)

        asyncio.run(scenario())

    def test_restore_mode_reenables_relationship_context(self):
        class RestoreEvent(Event):
            stopped = False

            def get_message_str(self):
                return "恢复正常"

        async def scenario():
            plugin = ProactiveBehavior.__new__(ProactiveBehavior)
            plugin._state = {
                "1211000567": {
                    "friend_mode": "quiet",
                    "first_seen_ts": 1,
                    "last_return_gap_hours": 48,
                    "message_count": 10,
                }
            }
            replies = []
            with patch.object(proactive_module, "_save_state"):
                async for reply in plugin.on_message_track(RestoreEvent()):
                    replies.append(reply)
            self.assertEqual(replies, ["恢复正常。"])

            request = Request()
            await plugin.inject_relationship_context(Event(), request)
            self.assertIn("【关系感知】", request.system_prompt)

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
