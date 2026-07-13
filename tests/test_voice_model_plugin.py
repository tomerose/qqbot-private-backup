import asyncio
import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

_PROJ_ROOT = Path(__file__).resolve().parents[1]
os.environ["ASTRBOT_ROOT"] = str(_PROJ_ROOT / "astrbot")

from astrbot.api.message_components import File, Plain, Record

PLUGINS_DIR = _PROJ_ROOT / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS_DIR))

from voice_model_router.main import VoiceModelRouter  # noqa: E402


class FakeResult:
    def __init__(self, chain):
        self.chain = chain


class FakeMessageObject:
    def __init__(self, components):
        self.message = components


class FakeEvent:
    def __init__(self, text, *, private=True, wake=True, components=None, result=None):
        self.message_str = text
        self._private = private
        self.is_at_or_wake_command = wake
        self.message_obj = FakeMessageObject(components or [Plain(text)])
        self._extra = {}
        self._result = result

    def is_private_chat(self):
        return self._private

    def set_extra(self, key, value):
        self._extra[key] = value

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def get_result(self):
        return self._result


class FakeTTSClient:
    def __init__(self, paths):
        self.paths = list(paths)
        self.texts = []

    async def synthesize(self, text):
        self.texts.append(text)
        return self.paths.pop(0) if self.paths else None


def write_wav(path: Path, frames: bytes, *, rate: int = 8000) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(frames)


class VoiceModelPluginTests(unittest.TestCase):
    def test_explicit_text_voice_request_switches_to_gemini(self):
        async def scenario():
            plugin = VoiceModelRouter.__new__(VoiceModelRouter)
            event = FakeEvent("请用语音回答我")
            await plugin.route_voice_request(event)
            self.assertTrue(event.get_extra("voice_reply_requested"))
            self.assertEqual(event.get_extra("selected_provider"), "gemini-2.5-flash")

        asyncio.run(scenario())

    def test_default_text_request_stays_text_and_deepseek(self):
        async def scenario():
            plugin = VoiceModelRouter.__new__(VoiceModelRouter)
            event = FakeEvent("正常回答我")
            await plugin.route_voice_request(event)
            self.assertIsNone(event.get_extra("voice_reply_requested"))
            self.assertIsNone(event.get_extra("selected_provider"))

        asyncio.run(scenario())

    def test_successful_synthesis_replaces_plain_text_and_preserves_files(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                first = root / "one.wav"
                second = root / "two.wav"
                write_wav(first, b"\x01\x00" * 4)
                write_wav(second, b"\x02\x00" * 6)
                plugin = VoiceModelRouter.__new__(VoiceModelRouter)
                plugin.tts_client = FakeTTSClient([first, second])
                plugin.audio_root = root
                result = FakeResult([Plain("第一句。第二句。"), File(name="answer.txt", file="answer.txt")])
                event = FakeEvent("请发语音", result=result)
                event.set_extra("voice_reply_requested", True)
                result.chain[0] = Plain("甲" * 150 + "。" + "乙" * 150 + "。")

                await plugin.synthesize_voice_reply(event)

                self.assertEqual(sum(isinstance(item, Record) for item in result.chain), 1)
                self.assertEqual(sum(isinstance(item, Plain) for item in result.chain), 0)
                self.assertEqual(sum(isinstance(item, File) for item in result.chain), 1)
                record = next(item for item in result.chain if isinstance(item, Record))
                with wave.open(str(record.file), "rb") as handle:
                    self.assertEqual(handle.getnframes(), 10)

        asyncio.run(scenario())

    def test_synthesis_failure_keeps_original_text(self):
        async def scenario():
            plugin = VoiceModelRouter.__new__(VoiceModelRouter)
            plugin.tts_client = FakeTTSClient([None])
            original = Plain("语音失败时保留文字")
            result = FakeResult([original])
            event = FakeEvent("请发语音", result=result)
            event.set_extra("voice_reply_requested", True)

            await plugin.synthesize_voice_reply(event)

            self.assertEqual(result.chain, [original])

        asyncio.run(scenario())

    def test_voice_transcript_can_request_voice_at_output_time(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                audio = Path(tmp) / "voice.wav"
                write_wav(audio, b"\x01\x00" * 4)
                plugin = VoiceModelRouter.__new__(VoiceModelRouter)
                plugin.tts_client = FakeTTSClient([audio])
                plugin.audio_root = Path(tmp)
                result = FakeResult([Plain("好的")])
                event = FakeEvent("", result=result)
                event.set_extra("_gemini_stt_transcript", "请用语音回答")

                await plugin.synthesize_voice_reply(event)

                self.assertTrue(any(isinstance(item, Record) for item in result.chain))

        asyncio.run(scenario())

    def test_sent_voice_cleanup_deletes_only_generated_audio_inside_private_root(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                audio_root = base / "audio"
                audio_root.mkdir()
                generated = audio_root / "generated.wav"
                generated.write_bytes(b"RIFF")
                outside = base / "outside.wav"
                outside.write_bytes(b"RIFF")
                plugin = VoiceModelRouter.__new__(VoiceModelRouter)
                plugin.audio_root = audio_root
                event = FakeEvent("")
                event.set_extra(
                    "_local_tts_audio_paths", [str(generated), str(outside)]
                )

                await plugin.cleanup_sent_voice(event)

                self.assertFalse(generated.exists())
                self.assertTrue(outside.exists())
                self.assertEqual(event.get_extra("_local_tts_audio_paths"), [])

        asyncio.run(scenario())

    def test_voice_message_auto_enables_voice_reply_in_private_chat(self):
        async def scenario():
            plugin = VoiceModelRouter.__new__(VoiceModelRouter)
            plugin.config = {"auto_voice_reply": True}
            # Simulate a voice message (Record component, non-empty text from STT)
            event = FakeEvent(
                "帮我查天气",
                private=True,
                components=[Record(file="voice.amr")],
            )
            await plugin.route_voice_request(event)
            self.assertTrue(event.get_extra("voice_reply_requested"))
            self.assertEqual(event.get_extra("selected_provider"), "gemini-2.5-flash")

        asyncio.run(scenario())

    def test_text_message_without_record_does_not_set_voice_reply(self):
        async def scenario():
            plugin = VoiceModelRouter.__new__(VoiceModelRouter)
            plugin.config = {"auto_voice_reply": True}
            event = FakeEvent("帮我查天气", private=True, components=[Plain("帮我查天气")])
            await plugin.route_voice_request(event)
            self.assertIsNone(event.get_extra("voice_reply_requested"))

        asyncio.run(scenario())

    def test_voice_message_in_group_without_at_does_not_set_voice_reply(self):
        async def scenario():
            plugin = VoiceModelRouter.__new__(VoiceModelRouter)
            plugin.config = {"auto_voice_reply": True}
            event = FakeEvent(
                "帮我查天气",
                private=False,
                wake=False,
                components=[Record(file="voice.amr")],
            )
            await plugin.route_voice_request(event)
            self.assertIsNone(event.get_extra("voice_reply_requested"))

        asyncio.run(scenario())

    def test_qq_ai_voice_uses_the_real_group_and_removes_duplicate_text(self):
        async def scenario():
            action = AsyncMock(side_effect=[
                [{"type": "推荐", "characters": [{"character_id": "1001", "character_name": "小柠"}]}],
                {"message_id": "456"},
            ])
            plugin = VoiceModelRouter.__new__(VoiceModelRouter)
            plugin._ai_characters_by_group = {}
            plugin.tts_client = None
            result = FakeResult([Plain("语音内容")])
            event = FakeEvent("请发语音", private=False, wake=True, result=result)
            event.bot = SimpleNamespace(call_action=action)
            event.get_group_id = lambda: "1075963106"
            event.set_extra("voice_reply_requested", True)

            await plugin.synthesize_voice_reply(event)

            self.assertEqual(result.chain, [])
            self.assertTrue(event.get_extra("_qq_ai_voice_sent"))
            self.assertEqual(action.await_args_list[0].kwargs["group_id"], "1075963106")
            self.assertEqual(action.await_args_list[1].args[0], "send_group_ai_record")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
