import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
os.environ["ASTRBOT_ROOT"] = str(ROOT / "astrbot")
sys.path.insert(0, str(ROOT / "astrbot"))
sys.path.insert(0, str(ROOT / "astrbot" / "data" / "plugins"))

from data.plugins.search_command.main import (  # noqa: E402
    ActionUsageStore,
    SearchContextStore,
    SearchCommand,
    _detect_search_mode,
    _fetch_news_rss,
    is_image_edit_intent,
    is_search_followup,
    is_video_search_intent,
    parse_action_pack,
)
from data.plugins.search_command import main as search_main  # noqa: E402
from data.plugins.draw_command.pro_access import Tier  # noqa: E402
from data.plugins.xiaoning_runtime import ArtifactDeliveryResult  # noqa: E402


class FakeOneBot:
    def __init__(self):
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        return {"retcode": 0}


class FakeEvent:
    is_at_or_wake_command = False

    def __init__(self, text, sender_id="123456"):
        self.text = text
        self.sender_id = sender_id
        self.stopped = False
        self.sent = []
        self.bot = FakeOneBot()

    def get_message_str(self):
        return self.text

    def is_private_chat(self):
        return True

    def plain_result(self, text):
        return text

    def chain_result(self, chain):
        return chain

    async def send(self, chain):
        self.sent.append(chain)

    def get_sender_id(self):
        return self.sender_id

    def get_group_id(self):
        return ""

    def stop_event(self):
        self.stopped = True


class SearchCommandTests(unittest.TestCase):
    def setUp(self):
        task_mirror = patch.object(
            search_main, "mirror_runtime_task_status", new=AsyncMock()
        )
        task_mirror.start()
        self.addCleanup(task_mirror.stop)

    def test_action_words_route_to_the_right_feature(self):
        cases = {
            "/research 手机端本地AI发展": ("research", "手机端本地AI发展"),
            "帮我深度研究今年大学生就业趋势": ("deepresearch", "今年大学生就业趋势"),
            "小柠，帮我比较 iPhone 和 Pixel": ("decision", "iPhone 和 Pixel"),
            "帮我规划旅行 杭州三天两晚": ("trip", "杭州三天两晚"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_action_pack(text), expected)
        self.assertIsNone(parse_action_pack("今天心情不太好"))
        self.assertIsNone(parse_action_pack("帮我找杭州旅行视频"))

    def test_existing_action_parser_remains_unchanged_for_meta_guard_layer(self):
        self.assertEqual(
            parse_action_pack("帮我深度研究今年大学生就业趋势"),
            ("deepresearch", "今年大学生就业趋势"),
        )

    def test_usage_is_persistent_atomic_and_refundable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.db"
            store = ActionUsageStore(path, clock=lambda: 1_784_000_000)
            self.assertEqual(store.consume("123", 2), (True, 1))
            self.assertEqual(store.consume("123", 2), (True, 2))
            self.assertEqual(ActionUsageStore(path, clock=lambda: 1_784_000_000).consume("123", 2), (False, 2))
            store.refund("123")
            self.assertEqual(store.consume("123", 2), (True, 2))

    def test_video_search_is_reserved_for_video_delivery(self):
        for text in ("帮我找一个姆巴佩的视频", "搜索猫咪短片", "/findvideo 梅西"):
            with self.subTest(text=text):
                self.assertTrue(is_video_search_intent(text))
        self.assertFalse(is_video_search_intent("搜索姆巴佩最近的比赛结果"))

    def test_removed_watermark_intent_is_not_reserved_for_draw_delivery(self):
        self.assertFalse(is_image_edit_intent("去s水印"))

        async def scenario():
            event = FakeEvent("去s水印")
            plugin = SearchCommand.__new__(SearchCommand)
            replies = [reply async for reply in plugin.on_message(event)]
            self.assertEqual(replies, [])
            self.assertFalse(event.stopped)

        asyncio.run(scenario())

    def test_search_followups_are_explicit_not_generic_chat(self):
        self.assertTrue(is_search_followup("再核实一下官方来源"))
        self.assertTrue(is_search_followup("有没有可靠来源"))
        self.assertFalse(is_search_followup("这个呢"))
        self.assertFalse(is_search_followup("刚才我有点难过"))

    def test_search_context_is_scoped_and_expires(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [1000.0]
            store = SearchContextStore(
                Path(directory) / "context.db", ttl_seconds=60, clock=lambda: now[0]
            )
            store.remember("private:one", "Gemini 最新版本")
            self.assertEqual(store.get("private:one"), ("Gemini 最新版本", "text"))
            self.assertIsNone(store.get("private:two"))
            now[0] = 1061.0
            self.assertIsNone(store.get("private:one"))

    def test_scoped_followup_reuses_only_same_senders_search(self):
        async def scenario(directory):
            plugin = SearchCommand.__new__(SearchCommand)
            plugin._search_context = SearchContextStore(Path(directory) / "context.db")
            mocked = AsyncMock(return_value=("搜索结果", []))
            with patch("data.plugins.search_command.main._call_proxy", mocked):
                first = FakeEvent("检索一下 Gemini 最新版本", sender_id="111111")
                self.assertTrue([reply async for reply in plugin.on_message(first)])
                followup = FakeEvent("再核实一下官方来源", sender_id="111111")
                self.assertTrue([reply async for reply in plugin.on_message(followup)])
                other = FakeEvent("再核实一下官方来源", sender_id="222222")
                self.assertEqual([reply async for reply in plugin.on_message(other)], [])

            self.assertEqual(mocked.await_count, 2)
            self.assertIn("上一轮搜索主题", mocked.await_args_list[1].args[0])
            self.assertIn("Gemini 最新版本", mocked.await_args_list[1].args[0])

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))

    def test_realtime_question_routes_without_fake_model_knowledge(self):
        async def scenario():
            event = FakeEvent("Gemini 最新版本是什么")
            plugin = SearchCommand.__new__(SearchCommand)
            mocked = AsyncMock(return_value=(("官方结果"), []))
            with patch("data.plugins.search_command.main._call_proxy", mocked):
                replies = [reply async for reply in plugin.on_message(event)]
            self.assertEqual(replies[0], "正在搜索…")
            self.assertTrue(event.stopped)

        asyncio.run(scenario())

    def test_international_news_falls_back_to_source_linked_rss(self):
        async def scenario():
            event = FakeEvent("搜索国际新闻")
            plugin = SearchCommand.__new__(SearchCommand)
            with patch(
                "data.plugins.search_command.main._call_proxy",
                new=AsyncMock(return_value=("", [])),
            ), patch(
                "data.plugins.search_command.main._fetch_news_rss",
                return_value="RSS 国际新闻\nhttps://example.com/news",
            ):
                replies = [reply async for reply in plugin.on_message(event)]
            self.assertEqual(replies[0], "正在搜索…")
            self.assertIn("https://example.com/news", replies[1])
            self.assertTrue(event.stopped)

        asyncio.run(scenario())

    def test_news_rss_parser_keeps_title_source_time_and_link(self):
        rss = b"""<?xml version='1.0' encoding='UTF-8'?>
        <rss><channel><item><title>Verified headline</title>
        <link>https://example.com/story</link><pubDate>Wed, 15 Jul 2026 09:53:20 GMT</pubDate>
        <source>Reuters</source></item></channel></rss>"""
        response = unittest.mock.Mock(content=rss)
        response.raise_for_status.return_value = None
        with patch("data.plugins.search_command.main.requests.get", return_value=response):
            result = _fetch_news_rss("国际新闻")
        self.assertIn("Verified headline", result)
        self.assertIn("来源：Reuters", result)
        self.assertIn("07-15 17:53", result)
        self.assertIn("https://example.com/story", result)

    def test_maps_does_not_combine_two_grounding_tools(self):
        self.assertEqual(
            _detect_search_mode("附近有什么咖啡店"),
            {"google_search": False, "google_maps": True, "code_execution": False},
        )

    def test_video_request_is_not_claimed(self):
        async def scenario():
            event = FakeEvent("帮我找一个姆巴佩的视频")
            plugin = SearchCommand.__new__(SearchCommand)
            replies = [reply async for reply in plugin.on_message(event)]
            self.assertEqual(replies, [])
            self.assertFalse(event.stopped)

        asyncio.run(scenario())

    def test_regular_search_returns_grounded_source(self):
        async def scenario():
            event = FakeEvent("搜索 Gemini 最新版本")
            plugin = SearchCommand.__new__(SearchCommand)
            mocked = AsyncMock(return_value=("搜索结果", [{"title": "官方", "uri": "https://example.com"}]))
            with patch("data.plugins.search_command.main._call_proxy", mocked):
                replies = [reply async for reply in plugin.on_message(event)]
            self.assertEqual(replies[0], "正在搜索…")
            self.assertIn("https://example.com", replies[1])
            self.assertTrue(event.stopped)

        asyncio.run(scenario())

    def test_search_always_follows_progress_when_proxy_raises_unexpected_error(self):
        async def scenario():
            event = FakeEvent("搜索 Gemini 最新版本")
            plugin = SearchCommand.__new__(SearchCommand)
            with patch(
                "data.plugins.search_command.main._call_proxy",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ):
                replies = [reply async for reply in plugin.on_message(event)]
            self.assertEqual(replies[0], "正在搜索…")
            self.assertIn("请稍后再试", replies[-1])

        asyncio.run(scenario())

    def test_search_always_follows_progress_when_rss_fallback_raises(self):
        async def scenario():
            event = FakeEvent("搜索国际新闻")
            plugin = SearchCommand.__new__(SearchCommand)
            with patch(
                "data.plugins.search_command.main._call_proxy",
                new=AsyncMock(return_value=("", [])),
            ), patch(
                "data.plugins.search_command.main._fetch_news_rss",
                side_effect=RuntimeError("rss boom"),
            ):
                replies = [reply async for reply in plugin.on_message(event)]
            self.assertEqual(replies[0], "正在搜索…")
            self.assertGreaterEqual(len(replies), 2)

        asyncio.run(scenario())

    def test_ordinary_user_gets_clear_action_upgrade_route(self):
        async def scenario():
            event = FakeEvent("/research 新能源车保值率")
            plugin = SearchCommand.__new__(SearchCommand)
            plugin._pro_db = Path("unused.db")
            with patch("data.plugins.search_command.main.get_tier", return_value=Tier.ORDINARY):
                replies = [reply async for reply in plugin.on_message(event)]
            self.assertEqual(len(replies), 1)
            self.assertIn("需要 X 或 Pro", replies[0])
            self.assertTrue(event.stopped)

        asyncio.run(scenario())

    def test_go_action_returns_markdown_file_to_qq(self):
        async def scenario(directory):
            event = FakeEvent("/compare Pixel 和 iPhone 怎么选")
            plugin = SearchCommand.__new__(SearchCommand)
            plugin._pro_db = Path("unused.db")
            plugin._usage = ActionUsageStore(Path(directory) / "usage.db")
            plugin._output_root = Path(directory) / "reports"
            generated = AsyncMock(return_value=("# 推荐\n选 Pixel。", [{"title": "依据", "uri": "https://example.com"}]))
            with patch("data.plugins.search_command.main.get_tier", return_value=Tier.X), patch.object(
                plugin, "_generate_action", generated
            ):
                replies = [reply async for reply in plugin.on_message(event)]
            self.assertIn("今日 1/3", replies[0])
            self.assertIn("已发送到当前私聊", replies[1])
            self.assertEqual(event.bot.calls[0][0], "upload_private_file")
            self.assertEqual(event.bot.calls[0][1]["user_id"], 123456)
            reports = list((Path(directory) / "reports").glob("*.md"))
            self.assertEqual(len(reports), 1)
            report = reports[0].read_text(encoding="utf-8")
            self.assertIn("Pixel 和 iPhone", report)
            self.assertIn("https://example.com", report)

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))

    def test_action_delivery_failure_is_reported_separately_and_refunded(self):
        async def scenario(directory):
            event = FakeEvent("/research 可穿戴设备趋势")
            plugin = SearchCommand.__new__(SearchCommand)
            plugin._pro_db = Path("unused.db")
            plugin._usage = ActionUsageStore(Path(directory) / "usage.db")
            plugin._output_root = Path(directory) / "reports"
            generated = AsyncMock(return_value=("报告正文", []))
            failed_delivery = AsyncMock(
                return_value=ArtifactDeliveryResult(False, "retained")
            )
            with patch(
                "data.plugins.search_command.main.get_tier", return_value=Tier.X
            ), patch.object(plugin, "_generate_action", generated), patch.object(
                plugin, "_deliver_report", failed_delivery
            ):
                replies = [reply async for reply in plugin.on_message(event)]

                self.assertIn("任务未完成", replies[-1])
            self.assertNotIn("生成失败", replies[-1])
            self.assertEqual(plugin._usage.consume("123456", 3), (True, 1))

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))

    def test_pro_research_runs_independent_second_verification(self):
        async def scenario():
            plugin = SearchCommand.__new__(SearchCommand)
            mocked = AsyncMock(side_effect=[
                ("初稿", [{"title": "A", "uri": "https://a.example"}]),
                ("复核后的完整报告", [{"title": "B", "uri": "https://b.example"}]),
            ])
            with patch("data.plugins.search_command.main._call_proxy", mocked):
                content, sources = await plugin._generate_action("research", "AI就业影响", Tier.PRO)
            self.assertEqual(content, "复核后的完整报告")
            self.assertEqual(mocked.await_count, 2)
            self.assertEqual(len(sources), 2)
            self.assertIn("待复核草稿", mocked.await_args_list[1].args[0])

        asyncio.run(scenario())

    def test_action_downgrades_tools_after_provider_empty_response(self):
        async def scenario():
            plugin = SearchCommand.__new__(SearchCommand)
            mocked = AsyncMock(side_effect=[ValueError("empty model response"), ("降级后成功", [])])
            with patch("data.plugins.search_command.main._call_proxy", mocked):
                content, _ = await plugin._generate_action("decision", "A和B怎么选", Tier.X)
            self.assertEqual(content, "降级后成功")
            self.assertEqual(mocked.await_count, 2)
            first_flags = mocked.await_args_list[0].args[1]
            second_flags = mocked.await_args_list[1].args[1]
            self.assertTrue(first_flags["code_execution"])
            self.assertFalse(second_flags["code_execution"])
            self.assertTrue(second_flags["google_search"])

        asyncio.run(scenario())

    def test_pro_trip_never_combines_maps_and_search(self):
        async def scenario():
            plugin = SearchCommand.__new__(SearchCommand)
            flags_seen = []

            async def fake_call(query, flags, **kwargs):
                flags_seen.append(dict(flags))
                return "资料或最终行程", [{"title": "来源", "uri": f"https://example.com/{len(flags_seen)}"}]

            with patch("data.plugins.search_command.main._call_proxy", side_effect=fake_call):
                content, sources = await plugin._generate_action("trip", "北京三天预算3000", Tier.PRO)
            self.assertTrue(content)
            self.assertTrue(sources)
            self.assertEqual(len(flags_seen), 3)
            self.assertTrue(any(item.get("google_maps") for item in flags_seen))
            self.assertTrue(any(item.get("google_search") for item in flags_seen))
            self.assertTrue(any(item.get("code_execution") for item in flags_seen))
            self.assertTrue(all(not (item.get("google_maps") and item.get("google_search")) for item in flags_seen))

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
