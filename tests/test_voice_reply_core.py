import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(r"D:\Claudecoda学习\qqbot\astrbot\data\plugins\voice_model_router")
sys.path.insert(0, str(PLUGIN_DIR))

from voice_reply_core import prepare_spoken_chunks, wants_voice_reply  # noqa: E402


class VoiceReplyCoreTests(unittest.TestCase):
    def test_voice_output_requires_explicit_request(self):
        for text in ("请发语音告诉我", "用语音回答", "语音回复我", "用语音说"):
            self.assertTrue(wants_voice_reply(text), text)
        for text in ("我发了一条语音", "听听这段语音", "默认文字回复", "你好"):
            self.assertFalse(wants_voice_reply(text), text)

    def test_spoken_chunks_remove_paths_secrets_and_code(self):
        raw = (
            "已经完成。文件在 D:\\private\\answer.txt。"
            "token=abcdef1234567890。```python\nprint('secret')\n```结果正常。"
        )
        chunks = prepare_spoken_chunks(raw)
        spoken = "".join(chunks)
        self.assertNotIn("D:\\", spoken)
        self.assertNotIn("abcdef", spoken)
        self.assertNotIn("print", spoken)
        self.assertIn("已经完成", spoken)
        self.assertIn("结果正常", spoken)

    def test_spoken_chunks_are_bounded_and_sentence_aware(self):
        text = "。".join(["这是一段自然句子" * 8 for _ in range(12)])
        chunks = prepare_spoken_chunks(text, max_chars=240, max_chunks=3)
        self.assertLessEqual(len(chunks), 3)
        self.assertLessEqual(sum(len(item) for item in chunks), 240)
        self.assertTrue(all(len(item) <= 120 for item in chunks))

    def test_empty_or_code_only_text_produces_no_audio_chunks(self):
        self.assertEqual(prepare_spoken_chunks("   "), [])
        self.assertEqual(prepare_spoken_chunks("```python\nprint('x')\n```"), [])


if __name__ == "__main__":
    unittest.main()
