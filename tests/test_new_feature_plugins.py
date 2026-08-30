import asyncio
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


PLUGINS = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS))

from ai_interview import main as interview_module  # noqa: E402
from ai_debate import main as debate_module  # noqa: E402
from ai_debate.main import parse_debate_topic  # noqa: E402
from pdf_analysis import main as pdf_module  # noqa: E402
from smart_translate.main import parse_translate_request  # noqa: E402
from welcome_card.main import WelcomeCard, _private_sender_id  # noqa: E402
from time_capsule.main import TimeCapsule, parse_capsule_request  # noqa: E402
from xiaoning_scheduled import email_utils as email_module  # noqa: E402
from xiaoning_scheduled import main as scheduled_module  # noqa: E402
from xiaoning_scheduled.main import DEFAULT_CONFIG, XiaoningScheduled  # noqa: E402


async def collect(generator):
    return [item async for item in generator]


class FakeEvent:
    def __init__(self, text="", sender="2000000000", origin="test:FriendMessage:2000000000"):
        self.text = text
        self.sender = sender
        self.unified_msg_origin = origin
        self.is_at_or_wake_command = True
        self.stopped = False

    def get_message_str(self):
        return self.text

    def get_sender_id(self):
        return self.sender

    def get_group_id(self):
        return "12345678" if "GroupMessage" in self.unified_msg_origin else ""

    def is_private_chat(self):
        return "GroupMessage" not in self.unified_msg_origin

    def stop_event(self):
        self.stopped = True

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

    def test_ordinary_group_users_are_rejected_for_x_only_features(self):
        async def scenario():
            interview = interview_module.AiInterview.__new__(interview_module.AiInterview)
            interview._pro_db = Path("unused.db")
            interview._sessions = {}
            interview._daily_usage = {}
            group_event = FakeEvent("/interview PM", origin="test:GroupMessage:12345678")
            with patch.object(interview_module, "get_tier", return_value=interview_module.Tier.ORDINARY), patch.object(
                interview_module, "is_active_pro_group", return_value=False
            ), patch.object(interview_module, "_call") as call:
                replies = await collect(interview.on_message(group_event))
            self.assertEqual(replies, [interview_module.PRO_REQUIRED_MSG])
            call.assert_not_called()

            debate = debate_module.AiDebate.__new__(debate_module.AiDebate)
            debate._pro_db = Path("unused.db")
            debate._daily_free = {}
            group_event = FakeEvent("/debate abcdefg", origin="test:GroupMessage:12345678")
            with patch.object(debate_module, "get_tier", return_value=debate_module.Tier.ORDINARY), patch.object(
                debate_module, "is_active_pro_group", return_value=False
            ), patch.object(debate_module, "_call_gemini") as call:
                replies = await collect(debate.on_message(group_event))
            self.assertEqual(replies, [debate_module.REQUIRED_MSG])
            call.assert_not_called()

            pdf = pdf_module.PdfAnalysis.__new__(pdf_module.PdfAnalysis)
            pdf._pro_db = Path("unused.db")
            group_event = FakeEvent("/analysis summarize", origin="test:GroupMessage:12345678")
            # 开放契约：无文件+群聊非 @ 直接 return，不再有 tier 拒绝
            replies = await collect(pdf.on_message(group_event))
            self.assertEqual(replies, [])

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

    def test_private_welcome_uses_origin_when_sender_id_is_missing(self):
        event = FakeEvent(sender="", origin="llbot-test:FriendMessage:2000000000")
        self.assertEqual(_private_sender_id(event), "2000000000")

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

    def test_group_summary_feature_is_removed_from_active_scheduler(self):
        self.assertFalse(any(key.startswith("group_summary") for key in DEFAULT_CONFIG))
        self.assertFalse(hasattr(XiaoningScheduled, "_push_group_summary"))

    def test_beautiful_moment_targets_only_the_designated_group(self):
        self.assertTrue(DEFAULT_CONFIG["beautiful_moment_enabled"])
        self.assertEqual(DEFAULT_CONFIG["beautiful_moment_time"], "23:00")
        self.assertEqual(DEFAULT_CONFIG["beautiful_moment_groups"], [])

        async def scenario():
            plugin = XiaoningScheduled.__new__(XiaoningScheduled)
            plugin.config = DEFAULT_CONFIG.copy()
            plugin._resolve_groups = AsyncMock(return_value=["900000002"])
            plugin._get_bot = AsyncMock()
            plugin._load_today_group_messages = AsyncMock(
                return_value=["今天一起解决了一个小问题", "大家互相说了晚安"]
            )
            plugin._generate_beautiful_moment = lambda _messages: (
                "🌙 今日美好时刻：一起解决问题，也认真道了晚安。"
            )
            bot = plugin._get_bot.return_value
            bot.call_action = AsyncMock(return_value={"status": "ok", "retcode": 0})

            delivered = await plugin._push_beautiful_moment()

            self.assertTrue(delivered)
            bot.call_action.assert_awaited_once()
            self.assertEqual(bot.call_action.await_args.args[0], "send_group_msg")
            self.assertEqual(bot.call_action.await_args.kwargs["group_id"], 900000002)
            self.assertIn(
                "今日美好时刻", bot.call_action.await_args.kwargs["message"]
            )

        asyncio.run(scenario())

    def test_group_send_treats_message_id_receipt_as_success_without_retry(self):
        async def scenario():
            plugin = XiaoningScheduled.__new__(XiaoningScheduled)
            bot = SimpleNamespace(
                call_action=AsyncMock(return_value={"message_id": 123456789})
            )

            with patch("xiaoning_scheduled.main.asyncio.sleep", new=AsyncMock()) as sleep:
                delivered = await plugin._send_group_message_with_retry(
                    bot, "900000002", "test", attempts=2
                )

            self.assertTrue(delivered)
            bot.call_action.assert_awaited_once()
            sleep.assert_not_awaited()

        asyncio.run(scenario())

    def test_beautiful_moment_manual_trigger_is_consumed_when_already_sent_today(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                plugin = XiaoningScheduled.__new__(XiaoningScheduled)
                plugin._opt_in_file = root / "ai_news_opt_in.json"
                plugin._runtime_file = root / "runtime.json"
                plugin._runtime = {
                    "beautiful_moment": datetime.now().strftime("%Y-%m-%d")
                }
                plugin._push_beautiful_moment = AsyncMock(return_value=True)
                plugin._save_json = lambda *_args: None
                trigger = root / "trigger_beautiful_moment"
                trigger.touch()

                await plugin._check_and_fire()

                self.assertFalse(trigger.exists())
                plugin._push_beautiful_moment.assert_not_awaited()

        asyncio.run(scenario())

    def test_failed_beautiful_moment_trigger_is_kept_for_retry(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                plugin = XiaoningScheduled.__new__(XiaoningScheduled)
                plugin._opt_in_file = root / "ai_news_opt_in.json"
                plugin._runtime_file = root / "runtime.json"
                plugin._runtime = {}
                plugin._push_beautiful_moment = AsyncMock(return_value=False)
                trigger = root / "trigger_beautiful_moment"
                trigger.touch()

                await plugin._check_and_fire()

                self.assertTrue(trigger.exists())
                self.assertNotIn("beautiful_moment", plugin._runtime)

        asyncio.run(scenario())

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

    def test_report_only_mode_schedules_exactly_early_noon_and_evening_reports(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                current = datetime.now().strftime("%H:%M")
                plugin = XiaoningScheduled.__new__(XiaoningScheduled)
                plugin.config = {
                    **DEFAULT_CONFIG,
                    "report_only_mode": True,
                    "ai_news_enabled": True,
                    "ai_news_time": current,
                    "noon_report_enabled": True,
                    "noon_report_time": current,
                    "evening_report_enabled": True,
                    "evening_report_time": current,
                    "github_trending_enabled": True,
                    "github_trending_time": current,
                    "morning_post_enabled": True,
                    "morning_time": current,
                    "weather_enabled": True,
                    "weather_time": current,
                    "beautiful_moment_enabled": True,
                    "beautiful_moment_time": current,
                    "zhoushen_daily_enabled": True,
                    "zhoushen_daily_time": current,
                    "zhoushen_song_enabled": True,
                    "zhoushen_song_time": current,
                    "zhoushen_meme_enabled": True,
                    "zhoushen_meme_time": current,
                }
                plugin._opt_in_file = root / "ai_news_opt_in.json"
                plugin._runtime_file = root / "runtime.json"
                plugin._runtime = {}
                plugin._save_json = lambda *_args: True
                report_handlers = []
                legacy_handlers = []
                for name in ("_push_ai_news", "_push_noon_report", "_push_evening_report"):
                    handler = AsyncMock(return_value=True)
                    setattr(plugin, name, handler)
                    report_handlers.append(handler)
                for name in (
                    "_push_github_trending", "_push_morning_post", "_push_weather",
                    "_push_beautiful_moment", "_push_zhoushen_daily",
                    "_push_zhoushen_song", "_push_zhoushen_meme",
                ):
                    handler = AsyncMock(return_value=True)
                    setattr(plugin, name, handler)
                    legacy_handlers.append(handler)

                await plugin._check_and_fire()

                for handler in report_handlers:
                    handler.assert_awaited_once()
                for handler in legacy_handlers:
                    handler.assert_not_awaited()

        asyncio.run(scenario())

    def test_report_only_mode_never_consumes_or_sends_legacy_manual_trigger(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                plugin = XiaoningScheduled.__new__(XiaoningScheduled)
                plugin.config = {
                    **DEFAULT_CONFIG,
                    "report_only_mode": True,
                    "ai_news_enabled": False,
                    "noon_report_enabled": False,
                    "evening_report_enabled": False,
                }
                plugin._opt_in_file = root / "ai_news_opt_in.json"
                plugin._runtime_file = root / "runtime.json"
                plugin._runtime = {}
                plugin._save_json = lambda *_args: True
                plugin._send_farewells = AsyncMock()
                trigger = root / "trigger_farewell"
                trigger.touch()

                await plugin._check_and_fire()

                plugin._send_farewells.assert_not_awaited()
                self.assertTrue(trigger.exists())

        asyncio.run(scenario())

    def test_early_report_text_delivery_is_success_when_pdf_attachment_fails(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                pdf = Path(tmp) / "early.pdf"
                pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 128)
                plugin = XiaoningScheduled.__new__(XiaoningScheduled)
                plugin.config = {**DEFAULT_CONFIG, "report_email_enabled": False}
                plugin._get_eligible_news_subscribers = lambda: ["900000001"]
                plugin._fetch_ai_news = lambda: "早报正文"
                plugin._render_report_pdf = lambda *_args: pdf
                bot = SimpleNamespace(
                    send_private_msg=AsyncMock(
                        side_effect=[None, RuntimeError("pdf unavailable")]
                    )
                )
                plugin._get_bot = AsyncMock(return_value=bot)

                delivered = await plugin._push_ai_news()

                self.assertTrue(delivered)
                bot.send_private_msg.assert_awaited_once_with(
                    user_id=900000001, message="早报正文"
                )

        asyncio.run(scenario())

    def test_report_pdf_uses_private_file_upload_and_is_also_emailed(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                pdf = Path(tmp) / "early.pdf"
                pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 128)
                plugin = XiaoningScheduled.__new__(XiaoningScheduled)
                plugin.config = {**DEFAULT_CONFIG, "report_email_enabled": True}
                plugin._get_eligible_news_subscribers = lambda: ["900000001"]
                bot = SimpleNamespace(
                    send_private_msg=AsyncMock(return_value=None),
                    call_action=AsyncMock(return_value={"retcode": 0}),
                )
                plugin._get_bot = AsyncMock(return_value=bot)
                plugin._send_report_email = Mock(return_value=True)

                delivered = await plugin._send_to_subscribers(
                    "早报正文", pdf, email_subject="小柠每日早报"
                )

                self.assertTrue(delivered)
                bot.send_private_msg.assert_awaited_once_with(
                    user_id=900000001, message="早报正文"
                )
                bot.call_action.assert_awaited_once_with(
                    "upload_private_file",
                    user_id=900000001,
                    file=str(pdf.resolve()),
                    name="early.pdf",
                )
                plugin._send_report_email.assert_called_once_with(
                    "小柠每日早报", "早报正文", pdf
                )

        asyncio.run(scenario())

    def test_report_email_contains_the_generated_pdf_attachment(self):
        class FakeSmtp:
            def __init__(self):
                self.login_args = None
                self.message = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def login(self, username, password):
                self.login_args = (username, password)

            def ehlo(self):
                return 250, b"ok"

            def starttls(self, *, context):
                self.tls_context = context

            def send_message(self, message):
                self.message = message

        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "early.pdf"
            pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 128)
            plugin = XiaoningScheduled.__new__(XiaoningScheduled)
            plugin.config = {
                **DEFAULT_CONFIG,
                "report_email_enabled": True,
                "report_email_to": "reader@example.com",
                "report_smtp_host": "smtp.example.com",
                "report_smtp_port": 587,
                "report_smtp_username": "sender@example.com",
            }
            smtp = FakeSmtp()

            with patch.dict(
                scheduled_module.os.environ,
                {"XIAONING_REPORT_SMTP_PASSWORD": "app-password"},
                clear=False,
            ), patch.object(email_module.smtplib, "SMTP", return_value=smtp):
                sent = plugin._send_report_email("小柠每日早报", "早报正文", pdf)

            self.assertTrue(sent)
            self.assertEqual(smtp.login_args, ("sender@example.com", "app-password"))
            self.assertEqual(smtp.message["To"], "reader@example.com")
            attachments = list(smtp.message.iter_attachments())
            self.assertEqual(len(attachments), 1)
            self.assertEqual(attachments[0].get_filename(), "early.pdf")
            self.assertEqual(attachments[0].get_content_type(), "application/pdf")

    def test_smtp_proxy_uses_standard_http_connect_tunnel(self):
        class FakeSocket:
            def __init__(self):
                self.sent = b""
                self.response = bytearray(
                    b"HTTP/1.1 200 Connection established\r\n\r\n"
                    b"220 smtp.gmail.com ESMTP ready\r\n"
                )

            def sendall(self, data):
                self.sent += data

            def recv(self, size):
                chunk = bytes(self.response[:size])
                del self.response[:size]
                return chunk

        fake_socket = FakeSocket()
        smtp = email_module._ProxySMTP.__new__(email_module._ProxySMTP)
        smtp._proxy_url = "http://127.0.0.1:7890"

        with patch.object(
            email_module.socket,
            "create_connection",
            return_value=fake_socket,
        ):
            result = smtp._get_socket("smtp.gmail.com", 587, 10)

        self.assertIs(result, fake_socket)
        self.assertIn(
            b"CONNECT smtp.gmail.com:587 HTTP/1.1\r\n", fake_socket.sent
        )
        self.assertIn(b"Proxy-Connection: Keep-Alive\r\n", fake_socket.sent)
        self.assertEqual(
            fake_socket.recv(4096), b"220 smtp.gmail.com ESMTP ready\r\n"
        )

    def test_report_email_retries_a_transient_smtp_disconnect(self):
        class FakeSmtp:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def ehlo(self):
                return 250, b"ok"

            def starttls(self, *, context):
                return 220, b"ready"

            def login(self, _username, _password):
                return 235, b"ok"

            def send_message(self, _message):
                return {}

        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "early.pdf"
            pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 128)
            plugin = XiaoningScheduled.__new__(XiaoningScheduled)
            plugin.config = {
                **DEFAULT_CONFIG,
                "report_email_enabled": True,
                "report_email_to": "reader@example.com",
                "report_smtp_host": "smtp.example.com",
                "report_smtp_port": 587,
                "report_smtp_username": "sender@example.com",
            }
            smtp = FakeSmtp()

            with patch.dict(
                scheduled_module.os.environ,
                {"XIAONING_REPORT_SMTP_PASSWORD": "app-password"},
                clear=False,
            ), patch.object(
                email_module.smtplib,
                "SMTP",
                side_effect=[
                    email_module.smtplib.SMTPServerDisconnected("transient"),
                    smtp,
                ],
            ) as factory, patch.object(email_module.time, "sleep"):
                sent = plugin._send_report_email("小柠每日早报", "正文", pdf)

            self.assertTrue(sent)
            self.assertEqual(factory.call_count, 2)

    def test_noon_report_uses_truthful_public_data_fallback_when_model_is_down(self):
        async def scenario():
            plugin = XiaoningScheduled.__new__(XiaoningScheduled)
            plugin._proxy_chat = lambda *_args, **_kwargs: None
            plugin._fetch_github_trending = lambda: (
                "【GitHub 今日热门】\n"
                "1. example/project ⭐123 — 示例\n"
                "   https://github.com/example/project"
            )
            plugin._render_report_pdf = lambda *_args: None
            plugin._send_to_subscribers = AsyncMock(return_value=True)

            delivered = await plugin._push_noon_report()

            self.assertTrue(delivered)
            text = plugin._send_to_subscribers.await_args.args[0]
            self.assertIn("公开数据降级版", text)
            self.assertIn("https://github.com/example/project", text)
            self.assertIn("15 分钟", text)

        asyncio.run(scenario())

    def test_evening_report_uses_non_fabricated_study_card_when_model_is_down(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = XiaoningScheduled.__new__(XiaoningScheduled)
                plugin.config = {**DEFAULT_CONFIG, "book_list": ["《测试书》测试作者"]}
                plugin._runtime = {}
                plugin._runtime_file = Path(tmp) / "runtime.json"
                plugin._save_json = lambda *_args: True
                plugin._proxy_chat = lambda *_args, **_kwargs: None
                plugin._render_report_pdf = lambda *_args: None
                plugin._send_to_subscribers = AsyncMock(return_value=True)

                delivered = await plugin._push_evening_report()

                self.assertTrue(delivered)
                text = plugin._send_to_subscribers.await_args.args[0]
                self.assertIn("《测试书》测试作者", text)
                self.assertIn("离线学习卡", text)
                self.assertIn("不提供未经核验的原文", text)

        asyncio.run(scenario())

    def test_evening_progress_advances_only_after_a_report_is_delivered(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = XiaoningScheduled.__new__(XiaoningScheduled)
                plugin.config = {**DEFAULT_CONFIG, "book_list": ["《测试书》测试作者"]}
                original = {"book_idx": 0, "day": 2, "date": "2026-08-01"}
                plugin._runtime = {"book_progress": original.copy()}
                plugin._runtime_file = Path(tmp) / "runtime.json"
                plugin._save_json = lambda *_args: True
                plugin._proxy_chat = lambda *_args, **_kwargs: "有效晚报正文"
                plugin._render_report_pdf = lambda *_args: None
                plugin._send_to_subscribers = AsyncMock(return_value=False)

                delivered = await plugin._push_evening_report()

                self.assertFalse(delivered)
                self.assertEqual(plugin._runtime["book_progress"], original)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
