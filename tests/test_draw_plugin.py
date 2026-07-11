import asyncio
import io
import sys
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image as PillowImage


PLUGINS_DIR = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS_DIR))

from draw_command.draw_core import DrawRateLimiter, parse_draw_command  # noqa: E402
from draw_command.main import DrawCommand  # noqa: E402
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
    def test_clear_natural_drawing_request_is_supported(self):
        self.assertEqual(parse_draw_command("帮我画一张雨夜城市海报"), "雨夜城市海报")
        self.assertEqual(parse_draw_command("请生成一张猫咪图片"), "猫咪")
        self.assertIsNone(parse_draw_command("帮我生成一份 Word 报告"))

    @staticmethod
    def build_plugin(output_root: Path) -> DrawCommand:
        plugin = DrawCommand.__new__(DrawCommand)
        plugin._pro_user_ids = frozenset({"1211000567"})
        plugin._rate_limiter = DrawRateLimiter(cooldown_seconds=60)
        plugin._generation_lock = asyncio.Lock()
        plugin._output_root = output_root
        plugin._pro_db_path = output_root.parent / "pro_members.db"
        return plugin

    @staticmethod
    def activate_pro(db_path: Path, qq_id: str):
        store = ProStore(db_path, reviewer_id="1211000567")
        now = time.time()
        app = store.create_application(qq_id, now=now)
        store.mark_sent(app.application_id, qq_id, now=now + 1)
        code = store.approve(app.application_id, "1211000567", 90, now=now + 2)
        store.verify(qq_id, code, now=now + 3)

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

                self.assertEqual(replies, [("plain", "作图是 Pro 功能。要开通或了解 Pro，可发邮件说明用途：portelamicheli636@gmail.com")])

        asyncio.run(scenario())

    def test_non_pro_draw_request_is_stopped_without_calling_proxy(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self.build_plugin(Path(tmp))
                plugin._request_image = lambda _prompt: self.fail("proxy should not run")
                event = FakeEvent("/draw a cat", "2000000000")

                replies = await collect(plugin.on_message(event))

                self.assertTrue(event.stopped)
                self.assertEqual(replies, [("plain", "作图是 Pro 功能。要开通或了解 Pro，可发邮件说明用途：portelamicheli636@gmail.com")])

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

                await plugin.cleanup_sent_images(event)

                self.assertFalse(generated.exists())
                self.assertTrue(outside.exists())

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
