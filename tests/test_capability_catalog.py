import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ROOT / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS))

from xiaoning_capabilities import CAPABILITIES, match_capability  # noqa: E402


class CapabilityCatalogTests(unittest.TestCase):
    def test_every_owner_is_enabled_and_guide_token_is_public(self):
        config = json.loads(
            (ROOT / "astrbot/data/cmd_config.json").read_text(encoding="utf-8-sig")
        )
        enabled = set(config["plugin_set"])
        guide = (PLUGINS / "contact_pro_info/user_guide.txt").read_text(
            encoding="utf-8"
        )
        for item in CAPABILITIES:
            with self.subTest(item=item.id):
                self.assertIn(item.owner, enabled)
                self.assertIn(item.guide_token, guide)

    def test_longest_specific_match_wins(self):
        self.assertEqual(match_capability("谁能帮我生成视频").id, "video_generate")
        self.assertEqual(match_capability("帮我找视频").id, "video_search")
        self.assertEqual(match_capability("需要一份 Word 报告").id, "document")


if __name__ == "__main__":
    unittest.main()
