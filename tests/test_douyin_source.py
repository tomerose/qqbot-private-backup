"""Regression tests for safe public-video cache downloads."""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "astrbot" / "data" / "plugins" / "douyin_source" / "main.py"
ASTRBOT_ROOT = str(ROOT / "astrbot")
if ASTRBOT_ROOT not in sys.path:
    sys.path.insert(0, ASTRBOT_ROOT)


def load_module():
    spec = importlib.util.spec_from_file_location("douyin_source_for_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class Response:
    def __init__(self, chunks):
        self.chunks = chunks

    def raise_for_status(self):
        return None

    def iter_content(self, _chunk_size):
        return iter(self.chunks)


class PublicVideoDownloadTests(unittest.TestCase):
    def test_rejects_html_and_removes_partial_file(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "bad.mp4"
            html = b"<!doctype html>" + b"x" * 1500
            with patch.object(module.requests, "get", return_value=Response([html])):
                self.assertFalse(module._download_public_video("https://example.com/video", dest))
            self.assertFalse(dest.exists())

    def test_keeps_a_real_mp4_payload(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "good.mp4"
            mp4 = b"\x00\x00\x00\x18ftypisom" + b"x" * 1500
            with patch.object(module.requests, "get", return_value=Response([mp4])):
                self.assertTrue(module._download_public_video("https://example.com/video", dest))
            self.assertEqual(dest.read_bytes(), mp4)


if __name__ == "__main__":
    unittest.main()
