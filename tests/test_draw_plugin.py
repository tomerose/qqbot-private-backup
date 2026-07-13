import asyncio
import io
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image as PillowImage


PLUGINS_DIR = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS_DIR))

from draw_command.draw_core import DrawRateLimiter, parse_draw_command  # noqa: E402
from draw_command.main import DrawCommand  # noqa: E402
from draw_command.pro_client import ProClient  # noqa: E402
from draw_command import main as draw_main  # noqa: E402
from pro_application.pro_store import ProStore  # noqa: E402


class FakeEvent:
    def __init__(self, text: str, sender: str, *, private: bool = True, wake: bool = True):
        self.text = text
        self.sender = sender
        self._private = private
        self.is_at_or_wake_command = wake
        self.stopped = False
        self.extra = {}

    def get_message_str(self):
        return self.text

    def get_sender_id(self):
        return self.sender

    def is_private_chat(self):
        return self._private

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, components):
        return ("chain", components)

    def set_extra(self, key, value):
        self.extra[key] = value

    def get_extra(self, key, default=None):
        return self.extra.get(key, default)


async def collect(generator):
    return [item async for item in generator]


class DrawPluginTests(unittest.TestCase):
    def setUp(self):
        # ponytail: clear cached ProClients so each test has clean isolation
        from draw_command.pro_access import _clients
        _clients.clear()

    def test_proxy_request_uses_current_vertex_image_model(self):
        plugin = DrawCommand.__new__(DrawCommand)

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"data": [{"b64_json": "cG5n"}]}

        with patch.object(draw_main.requests, "post", return_value=Response()) as post:
            self.assertEqual(plugin._request_image("draw a cat"), b"png")

        self.assertEqual(post.call_args.kwargs["json"]["model"], "gemini-2.5-flash-image")

    def test_clear_natural_drawing_request_is_supported(self):
        self.assertEqual(parse_draw_command("帮我画一张雨夜城市海报"), "雨夜城市海报")
        self.assertEqual(parse_draw_command("请生成一张猫咪图片"), "猫咪")
        self.assertIsNone(parse_draw_command("帮我生成一份 Word 报告"))
        self.assertEqual(parse_draw_command("生成图片"), "一张适合分享的高质量图片")

    @staticmethod
    def build_plugin(output_root: Path) -> DrawCommand:
        plugin = DrawCommand.__new__(DrawCommand)
        # ponytail: keep DB inside temp dir so each test isolates cleanly
        db_path = output_root / "pro_members.db"
        output_root.mkdir(parents=True, exist_ok=True)
        # Ensure owner has permanent Pro membership in the test DB
        ProStore(db_path, reviewer_id="1211000567")
        plugin._rate_limiter = DrawRateLimiter(cooldown_seconds=60)
        plugin._generation_lock = asyncio.Lock()
        plugin._output_root = output_root
        plugin._pro_client = ProClient(db_path)
        plugin._pro_db_path = db_path  # retained for test helpers
        plugin._daily_usage = {}
        return plugin

    @staticmethod
    def activate_pro(db_path: Path, qq_id: str):
        store = ProStore(db_path, reviewer_id="1211000567")
        now = time.time()
        app = store.create_application(qq_id, now=now)
        store.mark_sent(app.application_id, qq_id, now=now + 1)
        store.request_approval(app.application_id, "1211000567", 90, now=now + 2)
        code = store.confirm_approval(app.application_id, "1211000567", now=now + 3)
        store.verify(qq_id, code, now=now + 4)

    def test_approved_dynamic_pro_member_can_draw(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "draws"
                plugin = self.build_plugin(root)
                self.activate_pro(plugin._pro_db_path, "2000000000")
                image = PillowImage.new("RGB", (2, 2), "blue")
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                plugin._request_image = lambda _prompt: buffer.getvalue()

                replies = await collect(plugin.on_message(FakeEvent("/draw a cat", "2000000000")))

                self.assertEqual(replies[0], ("plain", "我开始画了，预计 30–90 秒。"))
                self.assertEqual(replies[-1][0], "chain")

        asyncio.run(scenario())

    def test_revoked_dynamic_pro_member_cannot_draw(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "draws"
                plugin = self.build_plugin(root)
                self.activate_pro(plugin._pro_db_path, "2000000000")
                store = ProStore(plugin._pro_db_path, reviewer_id="1211000567")
                self.assertTrue(store.revoke("2000000000", "1211000567", now=time.time()))
                plugin._request_image = lambda _prompt: self.fail("proxy should not run")

                replies = await collect(plugin.on_message(FakeEvent("/draw a cat", "2000000000")))

                self.assertEqual(replies, [("plain", "作图是 Pro/GO 功能。发送 /pro status 查看资格，或联系管理员开通")])

        asyncio.run(scenario())

    def test_non_pro_draw_request_is_stopped_without_calling_proxy(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self.build_plugin(Path(tmp))
                plugin._request_image = lambda _prompt: self.fail("proxy should not run")
                event = FakeEvent("/draw a cat", "2000000000")

                replies = await collect(plugin.on_message(event))

                self.assertTrue(event.stopped)
                self.assertEqual(replies, [("plain", "作图是 Pro/GO 功能。发送 /pro status 查看资格，或联系管理员开通")])

        asyncio.run(scenario())

    def test_group_draw_without_at_mention_is_ignored(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self.build_plugin(Path(tmp))
                event = FakeEvent("/draw a cat", "1211000567", private=False, wake=False)

                replies = await collect(plugin.on_message(event))

                self.assertFalse(event.stopped)
                self.assertEqual(replies, [])

        asyncio.run(scenario())

    def test_group_draw_delivery_uses_onebot_group_file_upload(self):
        class Bot:
            def __init__(self):
                self.calls = []

            async def call_action(self, action, **kwargs):
                self.calls.append((action, kwargs))

        class Event:
            def __init__(self):
                self.bot = Bot()

            def get_group_id(self):
                return "945598390"

            def plain_result(self, text):
                return text

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "draw.png"
            path.write_bytes(b"image")
            event = Event()
            plugin = DrawCommand.__new__(DrawCommand)
            reply = asyncio.run(plugin._deliver_image(event, path))
            self.assertIn("图片已上传到群文件", reply)
            self.assertEqual(event.bot.calls[0][0], "upload_group_file")
            self.assertEqual(event.bot.calls[0][1]["file"], str(path.resolve()))

    def test_generated_image_is_sanitized_to_private_png_and_cleanup_keeps_outside_file(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "draws"
                outside = Path(tmp) / "outside.png"
                outside.write_bytes(b"not touched")
                image = PillowImage.new("RGB", (2, 2), "red")
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                plugin = self.build_plugin(root)
                plugin._request_image = lambda _prompt: buffer.getvalue()
                event = FakeEvent("/draw a cat", "1211000567")

                replies = await collect(plugin.on_message(event))
                self.assertEqual(replies[0], ("plain", "我开始画了，预计 30–90 秒。"))
                generated = Path(event.get_extra("_pro_draw_output_paths")[0])
                self.assertTrue(generated.is_file())
                self.assertEqual(generated.suffix, ".png")
                event.set_extra("_pro_draw_output_paths", [str(generated), str(outside)])

                original_sleep = asyncio.sleep

                async def immediate_sleep(_seconds):
                    await original_sleep(0)

                with patch("draw_command.main.asyncio.sleep", new=immediate_sleep):
                    await plugin.cleanup_sent_images(event)
                    await original_sleep(0)
                    await original_sleep(0)

                self.assertFalse(generated.exists())
                self.assertTrue(outside.exists())

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
