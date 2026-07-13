import base64
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = ROOT / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS_DIR))
sys.path.insert(0, str(ROOT / "astrbot"))

from data.plugins.music_command.main import (  # noqa: E402
    MUSIC_MEMORY,
    MusicCommand,
    _search_netease_song,
    parse_netease_song_id,
    parse_original_song_prompt,
    parse_song_search,
)


def _load_proxy():
    spec = importlib.util.spec_from_file_location("qqbot_gemini_proxy_music", ROOT / "gemini-proxy.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class MusicCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proxy = _load_proxy()

    def test_netease_song_id_accepts_id_and_share_url(self):
        self.assertEqual(parse_netease_song_id("/music 123456"), "123456")
        self.assertEqual(
            parse_netease_song_id("网易云音乐 https://music.163.com/#/song?id=654321"),
            "654321",
        )
        self.assertEqual(parse_netease_song_id("/music not-a-song"), "")
        self.assertIsNone(parse_netease_song_id("播放一首歌"))

    def test_natural_music_requests_are_explicit_and_do_not_collide(self):
        self.assertEqual(
            parse_netease_song_id("\u5c0f\u67e0\uff0c\u5e2e\u6211\u53d1\u9001\u7f51\u6613\u4e91\u97f3\u4e50 123456"),
            "123456",
        )
        self.assertEqual(
            parse_original_song_prompt("\u5c0f\u67e0\uff0c\u7ed9\u6211\u5531\u4e00\u9996\u5173\u4e8e\u590f\u5929\u7684\u539f\u521b\u6b4c"),
            "\u5173\u4e8e\u590f\u5929\u7684\u539f\u521b\u6b4c",
        )
        self.assertEqual(
            parse_original_song_prompt("/sing \u6e29\u67d4\u7684\u6c11\u8c23\u539f\u521b\u6b4c"),
            "\u6e29\u67d4\u7684\u6c11\u8c23\u539f\u521b\u6b4c",
        )
        for text in ("\u5c0f\u67e0\u4f1a\u5531\u6b4c\u5417", "\u5e2e\u6211\u627e\u4e00\u9996\u6b4c", "\u64ad\u653e\u5468\u6770\u4f26\u7684\u6b4c", "\u63a8\u8350\u97f3\u4e50"):
            with self.subTest(text=text):
                self.assertIsNone(parse_netease_song_id(text))
                self.assertIsNone(parse_original_song_prompt(text))

    def test_song_name_search_is_narrow_and_returns_a_real_netease_id(self):
        self.assertEqual(parse_song_search("小柠，帮我点歌 稻香 周杰伦"), "稻香 周杰伦")
        self.assertIsNone(parse_song_search("推荐一些适合学习的音乐"))

        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "result": {
                    "songs": [
                        {"id": 123456, "name": "稻香", "artists": [{"name": "周杰伦"}]}
                    ]
                }
            },
        )
        with patch("data.plugins.music_command.main.requests.get", return_value=response):
            result = _search_netease_song("稻香 周杰伦")
        self.assertEqual(result["song_id"], "123456")
        self.assertEqual(result["artist"], "周杰伦")

    def test_music_memory_is_added_once_to_llm_requests(self):
        class Request:
            system_prompt = "\u539f\u59cb\u4eba\u8bbe"

        async def scenario():
            request = Request()
            plugin = MusicCommand.__new__(MusicCommand)
            await plugin.inject_music_memory(object(), request)
            await plugin.inject_music_memory(object(), request)
            self.assertEqual(request.system_prompt.count("\u3010\u97f3\u4e50\u80fd\u529b\u3011"), 1)
            self.assertIn(MUSIC_MEMORY, request.system_prompt)

        import asyncio
        asyncio.run(scenario())

    def test_netease_command_returns_a_native_music_card(self):
        class Event:
            is_at_or_wake_command = False

            def get_message_str(self):
                return "/music 123456"

            def is_private_chat(self):
                return True

            def chain_result(self, chain):
                return chain

            def stop_event(self):
                self.stopped = True

        async def scenario():
            event = Event()
            event.stopped = False
            plugin = MusicCommand.__new__(MusicCommand)
            replies = [reply async for reply in plugin.on_message(event)]
            self.assertTrue(event.stopped)
            self.assertEqual(replies[0][0].toDict()["type"], "music")
            self.assertEqual(replies[0][0].toDict()["data"]["type"], "163")
            self.assertEqual(replies[0][0].toDict()["data"]["id"], 123456)

        import asyncio
        asyncio.run(scenario())

    def test_lyria_response_is_decoded_through_vertex_client(self):
        audio = base64.b64encode(b"original-song").decode("ascii")
        captured = {}
        response = SimpleNamespace(
            outputs=[SimpleNamespace(type="audio", data=audio, mime_type="audio/mpeg")]
        )
        client = SimpleNamespace(
            interactions=SimpleNamespace(
                create=lambda **kwargs: captured.update(kwargs) or response
            )
        )
        with patch.object(self.proxy.genai, "Client", return_value=client):
            payload, mime = self.proxy._generate_music("an original song")
        self.assertEqual(payload, b"original-song")
        self.assertEqual(mime, "audio/mpeg")
        self.assertEqual(
            captured["input"],
            [{"type": "text", "text": "an original song"}],
        )


if __name__ == "__main__":
    unittest.main()
