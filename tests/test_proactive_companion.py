import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PLUGINS = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS))

from astrbot_plugin_proactive_chat.core.llm_adapter import (  # noqa: E402
    LlmMixin,
    PROACTIVE_HISTORY_MARKER,
)
from astrbot_plugin_proactive_chat.core.session_config import (  # noqa: E402
    ConfigMixin,
    Tier,
)
from friend_core.relationship_state import (  # noqa: E402
    QUIET_MODE,
    can_send_proactive,
    record_proactive_send,
    set_friend_mode,
)


class ConfigProbe(ConfigMixin):
    def __init__(self, config):
        self.config = config
        self.session_override_manager = None

    @staticmethod
    def _parse_session_id(session_id):
        platform, message_type, target = session_id.split(":", 2)
        return platform, message_type, target

    @staticmethod
    def _normalize_session_id(session_id):
        return session_id


class LlmProbe(LlmMixin):
    @staticmethod
    def _parse_session_id(session_id):
        platform, message_type, target = session_id.split(":", 2)
        return platform, message_type, target


class ProactiveCompanionTests(unittest.TestCase):
    def test_all_x_pro_sessions_excludes_ordinary_users_and_keeps_group_whitelist(self):
        probe = ConfigProbe(
            {
                "friend_settings": {
                    "enable": True,
                    "all_x_pro_sessions": True,
                    "session_list": [],
                },
                "group_settings": {"enable": True, "session_list": ["945598390"]},
            }
        )
        with patch(
            "astrbot_plugin_proactive_chat.core.session_config.get_tier",
            return_value=Tier.X,
        ):
            self.assertIsNotNone(probe._get_session_config("bot:FriendMessage:1211000567"))
        with patch(
            "astrbot_plugin_proactive_chat.core.session_config.get_tier",
            return_value=Tier.ORDINARY,
        ):
            self.assertIsNone(probe._get_session_config("bot:FriendMessage:2000000000"))
        self.assertIsNotNone(probe._get_session_config("bot:GroupMessage:945598390"))
        self.assertIsNone(probe._get_session_config("bot:GroupMessage:999999999"))

    def test_shared_cooldown_and_quiet_mode_stop_private_outreach(self):
        state = {}
        self.assertTrue(can_send_proactive(state, "1211000567", 6 * 3600, now=100))
        record_proactive_send(state, "1211000567", now=100)
        self.assertFalse(can_send_proactive(state, "1211000567", 6 * 3600, now=200))
        self.assertTrue(can_send_proactive(state, "1211000567", 6 * 3600, now=22000))
        set_friend_mode(state, "1211000567", QUIET_MODE)
        self.assertFalse(can_send_proactive(state, "1211000567", 0, now=22000))

    def test_output_filter_blocks_dependency_group_mentions_and_no_send(self):
        probe = LlmProbe()
        self.assertIsNone(
            probe._sanitize_proactive_response("NO_SEND", "bot:FriendMessage:1", "")
        )
        self.assertIsNone(
            probe._sanitize_proactive_response("我想你了，快回我。", "bot:FriendMessage:1", "")
        )
        self.assertIsNone(
            probe._sanitize_proactive_response("@小王 刚才那事咋样", "bot:GroupMessage:1", "")
        )
        self.assertEqual(
            probe._sanitize_proactive_response("上次那个方案后来顺了吗？", "bot:FriendMessage:1", ""),
            "上次那个方案后来顺了吗？",
        )

    def test_internal_prompt_marker_is_not_reused_as_user_context(self):
        probe = LlmProbe()
        history = [
            {"role": "user", "content": "上次的报告怎么样"},
            {"role": "user", "content": PROACTIVE_HISTORY_MARKER},
            {"role": "assistant", "content": "我没法确认交付状态。"},
        ]
        sanitized = probe._sanitize_history_content(history)
        self.assertEqual(len(sanitized), 2)
        self.assertNotIn(PROACTIVE_HISTORY_MARKER, [item["content"] for item in sanitized])

    def test_proactive_memory_is_requested_only_for_private_context(self):
        class MemoryPlugin:
            async def build_proactive_memory_block(self, sender, hint):
                return f"memory:{sender}:{hint}"

        MemoryPlugin.__module__ = "astrbot_plugin_xiaoning_memory.main"

        class Context:
            def get_all_stars(self):
                return [MemoryPlugin()]

        async def scenario():
            probe = LlmProbe()
            probe.context = Context()
            history = [{"role": "user", "content": "上次那个项目卡住了"}]
            self.assertIn(
                "memory:1211000567",
                await probe._get_proactive_memory_block("bot:FriendMessage:1211000567", history),
            )
            self.assertEqual(
                await probe._get_proactive_memory_block("bot:GroupMessage:945598390", history),
                "",
            )

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
