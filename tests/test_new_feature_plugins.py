import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PLUGINS = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS))

from ai_interview import main as interview_module  # noqa: E402
from welcome_card.main import WelcomeCard  # noqa: E402
from time_capsule.main import TimeCapsule  # noqa: E402
from xiaoning_scheduled.main import XiaoningScheduled  # noqa: E402


async def collect(generator):
    return [item async for item in generator]


class FakeEvent:
    def __init__(self, text="", sender="2000000000", origin="test:FriendMessage:2000000000"):
        self.text = text
        self.sender = sender
        self.unified_msg_origin = origin
        self.is_at_or_wake_command = True

    def get_message_str(self):
        return self.text

    def get_sender_id(self):
        return self.sender

    def get_group_id(self):
        return "12345678" if "GroupMessage" in self.unified_msg_origin else ""

    def stop_event(self):
        pass

    def plain_result(self, text):
        return text

    def chain_result(self, chain):
        return chain


class NewFeaturePluginTests(unittest.TestCase):
    def test_interview_starts_and_end_command_reaches_end_branch(self):
        plugin = interview_module.AiInterview.__new__(interview_module.AiInterview)
        plugin._pro_db = Path("unused.db")
        plugin._sessions = {}
        plugin._daily_usage = {}

        async def scenario():
            with patch.object(interview_module, "get_tier", return_value=interview_module.Tier.GO), patch.object(
                interview_module, "_call", return_value="请介绍一个代表项目"
            ):
                started = await collect(plugin.on_message(FakeEvent("/interview 产品经理")))
                ended = await collect(plugin.on_message(FakeEvent("/interview end")))
            self.assertIn("第 1/5 题", started[0])
            self.assertEqual(ended, ["面试已结束。"])
            self.assertEqual(plugin._sessions, {})

        asyncio.run(scenario())

    def test_group_welcome_yields_the_real_message_result(self):
        plugin = WelcomeCard.__new__(WelcomeCard)
        plugin._welcomed_friends = set()
        plugin._welcomed_groups = set()
        plugin._save_state = lambda: None
        replies = asyncio.run(
            collect(plugin.on_group_welcome(FakeEvent(origin="test:GroupMessage:12345678")))
        )
        self.assertEqual(len(replies), 1)
        self.assertIsNotNone(replies[0])

    def test_failed_capsule_delivery_is_retained_and_rescheduled(self):
        class Scheduler:
            def __init__(self):
                self.jobs = []

            def add_job(self, *args, **kwargs):
                self.jobs.append((args, kwargs))

        class Context:
            async def send_message(self, *_args, **_kwargs):
                return False

        plugin = TimeCapsule.__new__(TimeCapsule)
        plugin.context = Context()
        plugin.scheduler = Scheduler()
        cap = {
            "id": "cap-test",
            "sender_id": "2000000000",
            "platform": "test",
            "message": "hello",
            "from_str": "2026-01-01 00:00",
            "deliver_at": 1.0,
        }
        plugin._capsules = [cap]
        plugin._save = lambda: None
        asyncio.run(plugin._fire(cap.copy()))
        self.assertEqual(len(plugin._capsules), 1)
        self.assertEqual(len(plugin.scheduler.jobs), 1)

    def test_scheduled_push_requires_explicit_group_opt_in(self):
        plugin = XiaoningScheduled.__new__(XiaoningScheduled)
        self.assertEqual(asyncio.run(plugin._resolve_groups([])), [])
        self.assertEqual(asyncio.run(plugin._resolve_groups("")), [])


if __name__ == "__main__":
    unittest.main()
