import asyncio
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


PLUGINS = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS))

from astrbot_plugin_proactive_chat.core.llm_adapter import (  # noqa: E402
    LlmMixin,
    PROACTIVE_HISTORY_MARKER,
)
from astrbot_plugin_proactive_chat.core.message_events import EventsMixin  # noqa: E402
from astrbot_plugin_proactive_chat.core.session_config import (  # noqa: E402
    ConfigMixin,
    Tier,
)
from astrbot_plugin_proactive_chat.core.task_scheduler import (  # noqa: E402
    SchedulerMixin,
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


class SchedulerProbe(SchedulerMixin):
    def __init__(self, message_count):
        self.data_lock = asyncio.Lock()
        self.session_data = {
            "bot:GroupMessage:945598390": {
                "group_user_messages_since_proactive": message_count
            }
        }
        self.group_timers = {"bot:GroupMessage:945598390": TimerProbe()}
        self.timezone = timezone.utc
        self.scheduler = JobSchedulerProbe()
        self.checked_sessions = []
        self.tasks = []

    def _get_session_config(self, _session_id):
        return {"enable": True, "group_min_messages_before_proactive": 3}

    @staticmethod
    def _normalize_session_id(session_id):
        return session_id

    @staticmethod
    def _get_session_log_str(session_id, _config=None):
        return session_id

    def _track_task(self, task):
        self.tasks.append(task)
        return task

    async def check_and_chat(self, session_id):
        self.checked_sessions.append(session_id)


class TimerProbe:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class JobSchedulerProbe:
    def __init__(self):
        self.calls = []

    def add_job(self, *args, **kwargs):
        self.calls.append((args, kwargs))


class GroupEventProbe(EventsMixin):
    def __init__(self):
        self.data_lock = asyncio.Lock()
        self.session_data = {}
        self.last_message_times = {}
        self.session_temp_state = {}
        self.first_message_logged = set()
        self.scheduler = EventSchedulerProbe()
        self.plugin_start_time = 0
        self.reset_calls = []
        self.reply_calls = []

    @staticmethod
    def _normalize_session_id(session_id):
        return session_id

    @staticmethod
    def _get_session_log_str(session_id, _config=None):
        return session_id

    @staticmethod
    async def _cancel_all_related_auto_triggers(_session_id):
        return False

    @staticmethod
    def _is_persisted_task_still_valid(*_args, **_kwargs):
        return False

    @staticmethod
    def _purge_related_jobs(_session_id):
        pass

    @staticmethod
    def _clear_session_schedule_state(_session_id):
        return False

    @staticmethod
    async def _save_data_internal():
        pass

    @staticmethod
    def _get_session_config(_session_id):
        return {"enable": True, "group_min_messages_before_proactive": 2}

    async def _reset_group_silence_timer(self, session_id):
        self.reset_calls.append(session_id)

    async def _schedule_group_reply_after_threshold(self, session_id):
        self.reply_calls.append(session_id)


class EventSchedulerProbe:
    @staticmethod
    def get_job(_session_id):
        return None

    @staticmethod
    def remove_job(_session_id):
        raise KeyError


class GroupEvent:
    unified_msg_origin = "bot:GroupMessage:945598390"

    class message_obj:
        class sender:
            id = "member"

    @staticmethod
    def get_messages():
        return [object()]

    @staticmethod
    def get_self_id():
        return "bot"


class AutoTriggerProbe(SchedulerMixin):
    def __init__(self):
        self.config = {
            "friend_settings": {
                "enable": True,
                "all_friend_sessions": True,
                "session_list": ["default:FriendMessage:1211000567"],
            },
            "group_settings": {"enable": False, "session_list": []},
        }
        self.session_data = {
            "bot:FriendMessage:2000000000": {},
            "bot:GroupMessage:945598390": {},
        }
        self.setup_sessions = []
        self.scheduled_sessions = []

    @staticmethod
    def _parse_session_id(session_id):
        platform, message_type, target = session_id.split(":", 2)
        return platform, message_type, target

    async def _setup_auto_trigger_for_session_config(self, _settings, session_id):
        self.setup_sessions.append(session_id)
        return "created"

    def _get_session_config(self, _session_id):
        return {
            "enable": True,
            "schedule_settings": {"max_unanswered_times": 2},
        }

    @staticmethod
    def _has_related_persisted_task(_session_id):
        return False

    async def _schedule_next_chat_and_save(self, session_id):
        self.scheduled_sessions.append(session_id)


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

    def test_all_friend_sessions_includes_any_existing_private_conversation(self):
        probe = ConfigProbe(
            {
                "friend_settings": {
                    "enable": True,
                    "all_friend_sessions": True,
                    "session_list": [],
                },
                "group_settings": {"enable": True, "session_list": ["945598390"]},
            }
        )
        self.assertIsNotNone(probe._get_session_config("bot:FriendMessage:2000000000"))
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
        self.assertIsNone(
            probe._sanitize_proactive_response(
                "第一句。第二句。第三句。", "bot:GroupMessage:1", ""
            )
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

    def test_hybrid_context_keeps_platform_tail_and_conversation_history(self):
        class History:
            async def get(self, **_kwargs):
                return [
                    {
                        "sender_id": "1211000567",
                        "sender_name": "用户",
                        "content": {"message": [{"type": "plain", "text": "刚才提到的文件"}]},
                    }
                ]

        class Context:
            message_history_manager = History()

        async def scenario():
            probe = LlmProbe()
            probe.context = Context()
            probe.timezone = timezone.utc
            conversation = [
                {"role": "user", "content": "上文"},
                {"role": "assistant", "content": "我记得"},
            ]
            settings = {
                "source_mode": "hybrid",
                "platform_history_count": 20,
                "platform_history_prompt": "{{platform_history_lines}}",
                "include_bot_messages": True,
                "bot_identifiers": {"bot"},
                "platform_context_max_chars": 4000,
            }
            history = await probe._build_effective_history_context(
                "bot:FriendMessage:1211000567",
                conversation,
                settings,
            )
            self.assertEqual(history[1:], conversation)
            self.assertIn("刚才提到的文件", history[0]["content"])

        asyncio.run(scenario())

    def test_context_history_keeps_only_the_recent_configured_tail(self):
        async def scenario():
            probe = LlmProbe()
            conversation = [
                {"role": "user", "content": f"第 {index} 条"}
                for index in range(6)
            ]
            history = await probe._build_effective_history_context(
                "bot:FriendMessage:1211000567",
                conversation,
                {
                    "source_mode": "conversation_history",
                    "conversation_history_limit": 3,
                    "platform_history_count": 0,
                    "platform_context_max_chars": 4000,
                    "include_bot_messages": True,
                    "bot_identifiers": {"bot"},
                },
            )
            self.assertEqual(history, conversation[-3:])

        asyncio.run(scenario())

    def test_group_silence_requires_a_member_message(self):
        async def scenario():
            session_id = "bot:GroupMessage:945598390"
            quiet_probe = SchedulerProbe(message_count=0)
            await quiet_probe._handle_group_silence_callback(session_id, 20)
            self.assertEqual(quiet_probe.checked_sessions, [])

            ready_probe = SchedulerProbe(message_count=1)
            await ready_probe._handle_group_silence_callback(session_id, 20)
            await asyncio.gather(*ready_probe.tasks)
            self.assertEqual(ready_probe.checked_sessions, [session_id])

        asyncio.run(scenario())

    def test_three_member_messages_schedule_an_immediate_debounced_reply(self):
        async def scenario():
            session_id = "bot:GroupMessage:945598390"
            probe = SchedulerProbe(message_count=3)
            timer = probe.group_timers[session_id]

            self.assertTrue(
                await probe._schedule_group_reply_after_threshold(session_id)
            )
            self.assertTrue(timer.cancelled)
            self.assertEqual(len(probe.scheduler.calls), 1)
            _, kwargs = probe.scheduler.calls[0]
            self.assertEqual(kwargs["id"], session_id)
            self.assertTrue(kwargs["replace_existing"])
            self.assertLess(
                (kwargs["run_date"] - datetime.now(timezone.utc)).total_seconds(),
                2,
            )

        asyncio.run(scenario())

    def test_first_group_message_starts_the_idle_timer(self):
        async def scenario():
            probe = GroupEventProbe()
            await probe.on_group_message(GroupEvent())
            self.assertEqual(probe.reset_calls, [GroupEvent.unified_msg_origin])
            self.assertEqual(probe.reply_calls, [])

        asyncio.run(scenario())

    def test_all_private_mode_restores_known_private_sessions_on_startup(self):
        async def scenario():
            probe = AutoTriggerProbe()
            await probe._setup_auto_triggers_for_enabled_sessions()
            self.assertEqual(probe.setup_sessions, ["default:FriendMessage:1211000567"])
            self.assertEqual(probe.scheduled_sessions, ["bot:FriendMessage:2000000000"])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
