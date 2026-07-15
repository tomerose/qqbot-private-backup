import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


PLUGINS = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS))

from ai_interview import main as interview_module  # noqa: E402
from ai_debate.main import parse_debate_topic  # noqa: E402
from smart_translate.main import parse_translate_request  # noqa: E402
from welcome_card.main import WelcomeCard  # noqa: E402
from time_capsule.main import TimeCapsule, parse_capsule_request  # noqa: E402
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
    def test_natural_feature_phrases_route_to_their_own_plugins(self):
        self.assertEqual(
            parse_translate_request("帮我把你好翻译成英文"), ("en", "你好")
        )
        self.assertEqual(
            interview_module.parse_interview_start("帮我模拟产品经理面试"), "产品经理"
        )
        self.assertEqual(
            parse_debate_topic("圆桌讨论一下人工智能是否提高生产力"),
            "人工智能是否提高生产力",
        )
        capsule = parse_capsule_request("帮我写一个6个月后的时间胶囊：继续学英语")
        self.assertIsNotNone(capsule)
        self.assertEqual(capsule[1], "继续学英语")
        self.assertEqual(XiaoningScheduled._compact_news_action("早报开启"), "开启")
        self.assertEqual(XiaoningScheduled._compact_news_action("/早报开启"), "开启")
        self.assertEqual(XiaoningScheduled._compact_news_action("关闭早报"), "关闭")
        self.assertIsNone(XiaoningScheduled._compact_news_action("/早报 开启"))
        self.assertIsNone(XiaoningScheduled._compact_news_action("今天早报讲什么"))

    def test_interview_starts_and_end_command_reaches_end_branch(self):
        plugin = interview_module.AiInterview.__new__(interview_module.AiInterview)
        plugin._pro_db = Path("unused.db")
        plugin._sessions = {}
        plugin._daily_usage = {}

        async def scenario():
            with patch.object(interview_module, "get_tier", return_value=interview_module.Tier.X), patch.object(
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

    def test_ai_news_falls_back_to_real_rss_and_retries_failed_trigger(self):
        plugin = XiaoningScheduled.__new__(XiaoningScheduled)
        headlines = [
            "- OpenAI ships an AI update\n  https://example.com/ai\n  summary",
            "- Gemini research news\n  https://example.com/gemini\n  summary",
            "- Claude agent release\n  https://example.com/claude\n  summary",
        ]
        plugin._scrape_rss = lambda: headlines
        with patch("xiaoning_scheduled.main.requests.post", side_effect=RuntimeError("offline")):
            news = plugin._fetch_ai_news()
        self.assertIn("公开 RSS 标题速览", news)
        self.assertIn("https://example.com/ai", news)
        self.assertNotIn("早报生成失败", news)

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                plugin._opt_in_file = root / "ai_news_opt_in.json"
                plugin._runtime_file = root / "runtime.json"
                plugin._runtime = {}
                plugin._push_ai_news = AsyncMock(return_value=False)
                trigger = root / "trigger_ainews"
                trigger.touch()
                await plugin._check_and_fire()
                self.assertTrue(trigger.exists())
                self.assertNotIn("ainews", plugin._runtime)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
