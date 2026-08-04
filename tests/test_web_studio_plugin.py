import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot"))
sys.path.insert(0, str(ROOT / "astrbot" / "data" / "plugins"))

from data.plugins.draw_command.pro_access import Tier  # noqa: E402
from data.plugins.web_studio.main import (  # noqa: E402
    X_DAILY,
    PRO_DAILY,
    WebStudio,
    parse_web_intent,
)
from data.plugins.web_studio import main as web_main  # noqa: E402
from data.plugins.web_studio.publisher import PageSnapshot, PublishError  # noqa: E402


class FakeEvent:
    is_at_or_wake_command = False

    def __init__(self, text, owner="123456", private=True):
        self.text = text
        self.owner = owner
        self.private = private
        self.stopped = False

    def get_message_str(self):
        return self.text

    def get_sender_id(self):
        return self.owner

    def is_private_chat(self):
        return self.private

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text


async def collect(generator):
    return [item async for item in generator]


class WebStudioPluginTests(unittest.TestCase):
    def setUp(self):
        task_mirror = patch.object(
            web_main, "mirror_runtime_task_status", new=AsyncMock()
        )
        task_mirror.start()
        self.addCleanup(task_mirror.stop)

    def test_parser_claims_only_explicit_web_creation_requests(self):
        cases = {
            "/web 做一个番茄钟工具": ("create", "", "做一个番茄钟工具"),
            "/web create 一个旅行清单": ("create", "", "一个旅行清单"),
            "/web edit 0123456789 增加深色模式": ("edit", "0123456789", "增加深色模式"),
            "/web show 0123456789": ("show", "0123456789", ""),
            "帮我制作一个网页工具 记录喝水": ("create", "", "记录喝水"),
            "帮我做一个记录每日开销的网页": ("create", "", "记录每日开销"),
            "能帮我做一个整理图库的东西吗": ("create", "", "整理图库"),
            "刚才那个网页再加上导出功能": ("edit_latest", "", "加上导出功能"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                intent = parse_web_intent(text)
                self.assertIsNotNone(intent)
                self.assertEqual((intent.action, intent.page_id, intent.payload), expected)
        for text in ("这个网页讲了什么", "帮我搜索网页", "帮我生成视频一只猫"):
            with self.subTest(text=text):
                self.assertIsNone(parse_web_intent(text))

    def test_tier_limits_are_simple_and_visible(self):
        self.assertEqual(WebStudio._tier_limits(Tier.X), (X_DAILY, 3))
        self.assertEqual(WebStudio._tier_limits(Tier.PRO), (PRO_DAILY, 20))

    def _plugin(self):
        plugin = WebStudio.__new__(WebStudio)
        plugin._store = Mock()
        plugin._publisher = Mock()
        plugin._publisher.public_root = Path("public")
        plugin._publisher.page_url.side_effect = lambda page_id: f"https://site/x/{page_id}/"
        plugin._data_root = Path("data")
        plugin._pro_db = Path("pro.db")
        plugin._publish_lock = asyncio.Lock()
        return plugin

    @patch("data.plugins.web_studio.main.get_tier", return_value=Tier.ORDINARY)
    def test_ordinary_user_is_rejected_without_consuming_quota(self, _tier):
        plugin = self._plugin()
        event = FakeEvent("/web 制作一个每周菜谱网页")
        replies = asyncio.run(collect(plugin.on_message(event)))
        self.assertTrue(event.stopped)
        self.assertIn("需要 X 或 Pro", replies[-1])
        plugin._store.consume.assert_not_called()

    def test_group_request_is_sent_to_private_chat(self):
        plugin = self._plugin()
        event = FakeEvent("/web 制作一个每周菜谱网页", private=False)
        replies = asyncio.run(collect(plugin.on_message(event)))
        self.assertIn("请私聊", replies[-1])

    @patch("data.plugins.web_studio.main.get_tier", return_value=Tier.X)
    @patch("data.plugins.web_studio.main.generate_draft", side_effect=RuntimeError("boom"))
    def test_generation_failure_refunds_daily_use(self, _generate, _tier):
        plugin = self._plugin()
        plugin._store.active_count.return_value = 0
        plugin._store.consume.return_value = (True, 1, "2026-07-14")
        event = FakeEvent("/web 制作一个每周菜谱网页")
        replies = asyncio.run(collect(plugin.on_message(event)))
        self.assertIn("次数已退回", replies[-1])
        plugin._store.refund.assert_called_once_with("123456", "2026-07-14")

    def test_list_is_available_after_membership_expiry(self):
        plugin = self._plugin()
        plugin._store.list.return_value = [
            SimpleNamespace(id="0123456789", title="饮水记录")
        ]
        event = FakeEvent("/web list")
        replies = asyncio.run(collect(plugin.on_message(event)))
        self.assertIn("饮水记录", replies[-1])
        self.assertIn("https://site/x/0123456789/", replies[-1])

    @patch("data.plugins.web_studio.main.get_tier", return_value=Tier.X)
    @patch.object(WebStudio, "_edit", autospec=True)
    def test_natural_followup_edits_the_latest_owned_page(self, edit, _tier):
        plugin = self._plugin()
        plugin._store.list.return_value = [
            SimpleNamespace(id="0123456789", title="图库")
        ]

        async def fake_edit(_plugin, event, owner, tier, page_id, changes):
            yield event.plain_result(f"{owner}:{page_id}:{changes}")

        edit.side_effect = fake_edit
        event = FakeEvent("刚才那个网页再加上导出功能")
        replies = asyncio.run(collect(plugin.on_message(event)))

        self.assertEqual(replies[-1], "123456:0123456789:加上导出功能")

    @patch.object(WebStudio, "_finalize_html", new_callable=AsyncMock)
    @patch("data.plugins.web_studio.main.revise_page", return_value="draft")
    def test_edit_revalidates_after_generation_and_never_revives_deleted_page(
        self, _revise, finalize
    ):
        plugin = self._plugin()
        record = SimpleNamespace(
            id="0123456789", title="清单", prompt="制作任务清单", updated_at=10.0
        )
        plugin._store.get.side_effect = [record, None]
        plugin._store.consume.return_value = (True, 1, "2026-07-14")
        previous = PageSnapshot(document=b"old", preview=b"old-png")
        plugin._publisher.snapshot.return_value = previous
        plugin._publisher.read_app.return_value = "old app"
        finalize.return_value = ("safe app", "新清单")

        replies = asyncio.run(
            collect(plugin._edit(FakeEvent(""), "123456", Tier.X, "0123456789", "改颜色"))
        )

        self.assertIn("原页面保持不变", replies[-1])
        plugin._publisher.stage.assert_not_called()
        plugin._publisher.deploy.assert_not_called()
        plugin._store.update.assert_not_called()
        plugin._store.refund.assert_called_once_with("123456", "2026-07-14")

    @patch.object(WebStudio, "_finalize_html", new_callable=AsyncMock)
    @patch("data.plugins.web_studio.main.revise_page", return_value="draft")
    def test_edit_publish_failure_restores_html_preview_and_remote_release(
        self, _revise, finalize
    ):
        plugin = self._plugin()
        record = SimpleNamespace(
            id="0123456789", title="清单", prompt="制作任务清单", updated_at=10.0
        )
        plugin._store.get.side_effect = [record, record]
        plugin._store.consume.return_value = (True, 1, "2026-07-14")
        previous = PageSnapshot(document=b"old", preview=b"old-png")
        plugin._publisher.snapshot.side_effect = [previous, previous]
        plugin._publisher.read_app.return_value = "old app"
        plugin._publisher.deploy.side_effect = [PublishError("failed"), "ok"]
        finalize.return_value = ("safe app", "新清单")

        replies = asyncio.run(
            collect(plugin._edit(FakeEvent(""), "123456", Tier.X, "0123456789", "改颜色"))
        )

        self.assertIn("原页面保持不变", replies[-1])
        plugin._publisher.restore.assert_called_once_with("0123456789", previous)
        self.assertEqual(
            plugin._publisher.deploy.call_args_list,
            [
                unittest.mock.call("0123456789", True),
                unittest.mock.call("0123456789", True),
            ],
        )
        plugin._store.update.assert_not_called()

    @patch("data.plugins.web_studio.main.review_draft")
    def test_unsafe_first_draft_is_repaired_once(self, review):
        unsafe = (
            "<html><head><title>清单</title></head><body>"
            "<iframe src=\"data:text/html,x\"></iframe></body></html>"
        )
        repaired = (
            "<html><head><title>任务清单</title></head><body>"
            "<input><button>添加任务</button></body></html>"
        )
        review.return_value = repaired
        html, title = asyncio.run(
            WebStudio._finalize_html("制作一个任务清单网页", unsafe)
        )
        self.assertEqual(title, "任务清单")
        self.assertNotIn("<iframe", html)
        review.assert_called_once()

    @patch("data.plugins.web_studio.main.review_draft")
    def test_safety_repair_rechecks_and_never_publishes_second_unsafe_draft(self, review):
        unsafe = "<html><head><title>x</title></head><body><iframe></iframe></body></html>"
        still_unsafe = "<html><head><title>x</title></head><body><object></object></body></html>"
        repaired = (
            "<html><head><title>任务清单</title></head><body>"
            "<input><button>添加任务</button></body></html>"
        )
        review.side_effect = [still_unsafe, repaired]
        html, title = asyncio.run(WebStudio._finalize_html("制作任务清单", unsafe))
        self.assertEqual(title, "任务清单")
        self.assertNotIn("<object", html)
        self.assertEqual(review.call_count, 2)


if __name__ == "__main__":
    unittest.main()
