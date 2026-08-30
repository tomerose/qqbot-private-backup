import sys
import unittest
from pathlib import Path


PLUGINS = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS))

from link_summary.main import _is_public_read_command  # noqa: E402


class PublicReadCommandTests(unittest.TestCase):
    def test_public_read_aliases_are_explicit(self):
        for text in ("/summary https://example.com", "/browse https://example.com", "/浏览 https://example.com"):
            with self.subTest(text=text):
                self.assertTrue(_is_public_read_command(text))
        self.assertFalse(_is_public_read_command("帮我浏览网页"))


if __name__ == "__main__":
    unittest.main()
