import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from image_proxy_core import (  # noqa: E402
    IMAGE_MODEL_FALLBACK,
    IMAGE_MODEL_PRIMARY,
    ImageRequestError,
    extract_first_image_bytes,
    image_model_attempts,
    normalize_image_request,
)


class ImageProxyCoreTests(unittest.TestCase):
    def test_uses_the_current_vertex_image_model(self):
        self.assertEqual(IMAGE_MODEL_PRIMARY, "gemini-3-pro-image")
        self.assertEqual(
            image_model_attempts(IMAGE_MODEL_PRIMARY),
            (IMAGE_MODEL_PRIMARY, IMAGE_MODEL_FALLBACK),
        )

    def test_normalizes_only_allowlisted_image_models_and_aspect_ratios(self):
        normalized = normalize_image_request(
            {"prompt": "draw a cat", "model": "untrusted", "size": "1024x1024"}
        )
        self.assertEqual(normalized.model, IMAGE_MODEL_PRIMARY)
        self.assertEqual(normalized.aspect_ratio, "1:1")

        portrait = normalize_image_request(
            {"prompt": "draw a portrait", "model": IMAGE_MODEL_FALLBACK, "size": "1024x1536"}
        )
        self.assertEqual(portrait.model, IMAGE_MODEL_FALLBACK)
        self.assertEqual(portrait.aspect_ratio, "2:3")

        with self.assertRaises(ImageRequestError):
            normalize_image_request({"prompt": ""})

    def test_extracts_only_inline_image_bytes_from_model_response(self):
        response = SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(
                        parts=[
                            SimpleNamespace(text="ignored", inline_data=None),
                            SimpleNamespace(
                                inline_data=SimpleNamespace(
                                    mime_type="image/png", data=b"png-bytes"
                                )
                            ),
                        ]
                    )
                )
            ]
        )
        self.assertEqual(extract_first_image_bytes(response), ("image/png", b"png-bytes"))


if __name__ == "__main__":
    unittest.main()
