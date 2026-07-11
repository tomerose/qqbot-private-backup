import importlib.util
import asyncio
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "gemini-proxy.py"
SPEC = importlib.util.spec_from_file_location("gemini_proxy", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class GeminiAudioTests(unittest.TestCase):
    def test_audio_data_url_becomes_vertex_inline_audio_part(self):
        _, contents = MODULE._to_contents(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "audio_url",
                            "audio_url": {"url": "data:audio/ogg;base64,SGVsbG8="},
                        }
                    ],
                }
            ]
        )
        part = contents[0].parts[0]
        self.assertEqual(part.inline_data.mime_type, "audio/ogg")
        self.assertEqual(part.inline_data.data, b"Hello")

    def test_model_capabilities_include_audio(self):
        payload = asyncio.run(MODULE.list_models())
        self.assertTrue(all(item["capabilities"].get("audio") for item in payload["data"]))


if __name__ == "__main__":
    unittest.main()
