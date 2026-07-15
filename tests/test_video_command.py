import asyncio
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import requests


ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = ROOT / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS_DIR))
sys.path.insert(0, str(ROOT / "astrbot"))

from data.plugins.video_command.main import (  # noqa: E402
    Tier,
    VideoCommand,
    _is_search_mode,
    _parse_duration,
    _parse_video_command,
)
from data.plugins.video_agent.main import _parse_agent_command  # noqa: E402


def _load_proxy():
    spec = importlib.util.spec_from_file_location("qqbot_gemini_proxy", ROOT / "gemini-proxy.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class VideoCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proxy = _load_proxy()

    def test_natural_video_prompt_has_a_real_capture_group(self):
        self.assertEqual(_parse_video_command("帮我生成视频 一只猫 4秒"), "一只猫 4秒")
        self.assertEqual(_parse_video_command("生成sp一只猫"), "一只猫")
        self.assertEqual(_parse_video_command("生成视频一只猫"), "一只猫")
        self.assertEqual(_parse_video_command("帮我生成一个视频"), "")
        self.assertEqual(_parse_video_command("帮我生成一只猫的视频"), "一只猫")
        self.assertIsNone(_parse_video_command("你能做视频吗"))
        self.assertIsNone(_parse_video_command("小柠支持生成视频吗"))
        self.assertIsNone(_parse_video_command("生成视频很难"))
        self.assertIsNone(_parse_video_command("我觉得做视频不太靠谱"))

    def test_real_qq_video_wording_reaches_the_right_video_route(self):
        self.assertEqual(
            _parse_agent_command("使用视频agent给我生成一段如何关于成为博主的视频"),
            "如何成为博主",
        )
        self.assertEqual(
            _parse_agent_command("做一段如何成为博主的视频"),
            "如何成为博主",
        )
        self.assertEqual(_parse_video_command("用ai做一个未来视频"), "未来")
        self.assertIsNone(_parse_agent_command("视频agent怎么用"))

    def test_duration_parsing_keeps_generation_boundary_explicit(self):
        self.assertEqual(_parse_duration("一只猫 4s"), 4)
        self.assertEqual(_parse_duration("风景 1分钟"), 60)
        self.assertIsNone(_parse_duration("没有时长"))

    def test_video_without_duration_generates_and_long_video_searches(self):
        self.assertFalse(_is_search_mode("/video 一只猫", "一只猫"))
        self.assertFalse(_is_search_mode("/video 一只猫 4s", "一只猫 4s"))
        self.assertFalse(_is_search_mode("/video 小猫找妈妈", "小猫找妈妈"))
        self.assertTrue(_is_search_mode("/video 一只猫 8s", "一只猫 8s"))
        self.assertTrue(_is_search_mode("/findvideo 一只猫", "一只猫"))
        self.assertEqual(_parse_video_command("帮我找猫咪视频"), "猫咪")
        self.assertEqual(_parse_video_command("帮我找一个姆巴佩的视频"), "姆巴佩")

    def test_search_progress_does_not_stop_before_result(self):
        class Event:
            is_at_or_wake_command = False

            def get_message_str(self):
                return "帮我找一个姆巴佩的视频"

            def is_private_chat(self):
                return True

            def get_sender_id(self):
                return "123456789"

            def plain_result(self, text):
                return text

            def stop_event(self):
                self.stopped = True

        async def scenario():
            plugin = VideoCommand.__new__(VideoCommand)
            plugin._search_videos = lambda query: ("https://example.com/mbappe", [])
            event = Event()
            event.stopped = False
            with patch("data.plugins.video_command.main.get_tier", return_value=Tier.ORDINARY):
                replies = [reply async for reply in plugin.on_message(event)]
            self.assertEqual(replies[0], "正在搜索 B 站和抖音公开视频，预计 5–15 秒…")
            self.assertIn("https://example.com/mbappe", replies[1])
            self.assertTrue(event.stopped)

        asyncio.run(scenario())

    def test_search_returns_only_bilibili_links(self):
        class Response:
            def __init__(self, body):
                self.body = body

            def raise_for_status(self):
                return None

            def json(self):
                return self.body

        plugin = VideoCommand.__new__(VideoCommand)
        with patch.object(VideoCommand, "_search_douyin", return_value=("", [])), patch(
            "data.plugins.video_command.main.requests.get",
            return_value=Response(
                {"data": {"result": [{"bvid": "BV1test", "title": "<em>姆巴佩</em> 集锦"}]}}
            ),
        ):
            text, urls = plugin._search_videos("姆巴佩")
        self.assertIn("姆巴佩 集锦", text)
        self.assertEqual(urls, ["https://www.bilibili.com/video/BV1test"])

    def test_bilibili_412_falls_back_to_all_search_with_verified_bv_links(self):
        class Response:
            def __init__(self, body=None, status_code=200):
                self.body = body or {}
                self.status_code = status_code

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise requests.HTTPError(str(self.status_code))

            def json(self):
                return self.body

        plugin = VideoCommand.__new__(VideoCommand)
        with patch.object(VideoCommand, "_search_douyin", return_value=("", [])), patch(
            "data.plugins.video_command.main.requests.get",
            side_effect=[
                Response({"code": -412}, 412),
                Response(
                    {
                        "code": 0,
                        "data": {
                            "result": [
                                {
                                    "result_type": "video",
                                    "data": [
                                        {"bvid": "BV19g4y1G7uK", "title": "<em>姆巴佩</em> 集锦"},
                                        {"bvid": "invalid", "title": "should be ignored"},
                                    ],
                                }
                            ]
                        },
                    }
                ),
            ],
        ):
            text, urls = plugin._search_videos("姆巴佩踢球")
        self.assertIn("姆巴佩 集锦", text)
        self.assertEqual(urls, ["https://www.bilibili.com/video/BV19g4y1G7uK"])

    def test_all_search_rejects_unverified_bv_text(self):
        class Response:
            def __init__(self, body=None, *, status_code=200):
                self.body = body or {}
                self.status_code = status_code

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise requests.HTTPError(str(self.status_code))

            def json(self):
                return self.body

        plugin = VideoCommand.__new__(VideoCommand)
        with patch(
            "data.plugins.video_command.main.requests.get",
            return_value=Response(
                {
                    "code": 0,
                    "data": {"result": [{"result_type": "video", "data": [{"bvid": "BVLSWZN8Ar35M2s"}]}]},
                }
            ),
        ):
            _, urls = plugin._search_bilibili_all("姆巴佩踢球")
        self.assertEqual(urls, [])

    def test_search_returns_links_without_blocking_on_platform_downloads(self):
        class Event:
            is_at_or_wake_command = False

            def get_message_str(self):
                return "帮我找一个姆巴佩的视频"

            def is_private_chat(self):
                return True

            def get_sender_id(self):
                return "123456789"

            def plain_result(self, text):
                return text

            def stop_event(self):
                self.stopped = True

        async def scenario():
            plugin = VideoCommand.__new__(VideoCommand)
            plugin._search_videos = lambda query: (
                "姆巴佩集锦 - https://www.bilibili.com/video/BV1test",
                [
                    "https://www.bilibili.com/video/BV1test",
                    "https://www.bilibili.com/video/BV2test",
                ],
            )
            event = Event()
            event.stopped = False
            with patch("data.plugins.video_command.main.get_tier", return_value=Tier.ORDINARY):
                replies = [reply async for reply in plugin.on_message(event)]
            self.assertIn("https://www.bilibili.com/video/BV1test", replies[1])
            self.assertEqual(len(replies), 2)
            self.assertTrue(event.stopped)

        asyncio.run(scenario())

    def test_ordinary_users_can_search_but_cannot_generate(self):
        class Event:
            is_at_or_wake_command = False

            def get_message_str(self):
                return "帮我生成一只猫的视频"

            def is_private_chat(self):
                return True

            def get_sender_id(self):
                return "123456789"

            def plain_result(self, text):
                return text

            def stop_event(self):
                self.stopped = True

        async def scenario():
            plugin = VideoCommand.__new__(VideoCommand)
            event = Event()
            with patch("data.plugins.video_command.main.get_tier", return_value=Tier.ORDINARY):
                replies = [reply async for reply in plugin.on_message(event)]
            self.assertEqual(
                replies,
                [
                    "🎬 AI 视频生成需要 X 或 Pro 资格。\n"
                    "添加小柠为 QQ 好友即可自动获得 X资格。\n"
                    "也可以免费使用 /做视频 <主题> — 自动脚本+素材+配音+字幕合成完整短片。"
                ],
            )

        asyncio.run(scenario())

    def test_video_download_url_rejects_private_networks(self):
        with patch.object(
            self.proxy.socket,
            "getaddrinfo",
            return_value=[(2, 1, 6, "", ("127.0.0.1", 80))],
        ):
            self.assertFalse(self.proxy._is_safe_video_url("http://example.com/video.mp4"))
        with patch.object(
            self.proxy.socket,
            "getaddrinfo",
            return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
        ):
            self.assertTrue(self.proxy._is_safe_video_url("https://example.com/video.mp4"))
        self.assertFalse(self.proxy._is_safe_video_url("file:///etc/passwd"))

    def test_bilibili_api_fallback_resolves_page_to_media(self):
        class Response:
            def __init__(self, body):
                self.body = body

            def raise_for_status(self):
                return None

            def json(self):
                return self.body

        responses = [
            Response({"data": [{"cid": 12345}]}),
            Response({"data": {"durl": [{"url": "https://media.example/video.mp4", "size": 1024}]}}),
        ]
        with (
            patch.object(self.proxy.requests, "get", side_effect=responses),
            patch.object(
                self.proxy,
                "_try_direct_download",
                return_value=(b"video", "video/mp4"),
            ) as direct,
        ):
            result = self.proxy._try_bilibili_api_download(
                "https://www.bilibili.com/video/BV1EHNx6QEri"
            )
        self.assertEqual(result, (b"video", "video/mp4"))
        direct.assert_called_once_with(
            "https://media.example/video.mp4",
            {"Referer": "https://www.bilibili.com/video/BV1EHNx6QEri"},
        )

    def test_group_video_delivery_uses_native_group_file_upload(self):
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
            path = Path(tmp) / "video.mp4"
            path.write_bytes(b"video")
            event = Event()
            plugin = VideoCommand.__new__(VideoCommand)
            plugin._output_root = Path(tmp)
            delivery = asyncio.run(plugin._deliver_video(event, path))
            self.assertTrue(delivery.delivered)
            self.assertEqual(delivery.channel, "group_upload")
            self.assertEqual(event.bot.calls[0][0], "upload_group_file")
            self.assertEqual(event.bot.calls[0][1]["file"], str(path.resolve()))

    def test_video_generation_is_offloaded_from_the_event_loop(self):
        class Request:
            async def json(self):
                return {"prompt": "一只猫", "duration": 4, "aspect_ratio": "16:9"}

        expected = {"data": [{"b64_json": "AA==", "mime_type": "video/mp4"}]}

        async def scenario():
            with patch.object(
                self.proxy.asyncio, "to_thread", new=AsyncMock(return_value=expected)
            ) as offload:
                result = await self.proxy.generate_video(Request())
                self.assertEqual(result, expected)
                offload.assert_awaited_once()

        asyncio.run(scenario())

    def test_invite_web_and_bot_use_the_same_runtime_directory(self):
        self.assertEqual(
            self.proxy.INVITE_DATA_DIR.resolve(),
            (ROOT / "astrbot" / "data" / "plugin_data" / "xiaoning_pro").resolve(),
        )
        self.assertEqual(
            self.proxy.INVITE_KEY_FILE,
            self.proxy.INVITE_DB.with_suffix(".key"),
        )


if __name__ == "__main__":
    unittest.main()
