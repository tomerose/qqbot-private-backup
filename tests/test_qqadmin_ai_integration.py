import json
import sys
import unittest
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins" / "astrbot_plugin_qqadmin"


class QQAdminAIIntegrationTests(unittest.TestCase):
    def test_only_owner_can_toggle_ai_moderation(self):
        source = (_PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
        self.assertIn('OWNER_ID = "1211000567"', source)
        self.assertIn('filter.command("AI群管开")', source)
        self.assertIn('filter.command("AI群管关")', source)
        self.assertIn('filter.command("AI群管状态")', source)
        self.assertIn("_is_ai_moderation_owner", source)

    def test_kick_block_and_member_cleanup_are_permanently_disabled(self):
        source = (_PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
        self.assertIn("该能力已永久关闭", source)
        for method in ("set_group_kick", "set_group_block", "clear_group_member"):
            section = source.split(f"async def {method}", 1)[1].split("\n    async def ", 1)[0]
            self.assertNotIn("self.normal.set_group_kick", section)
            self.assertNotIn("self.normal.set_group_block", section)
            self.assertNotIn("self.member.clear_group_member", section)
        self.assertNotIn("llm_set_group_ban", source)

    def test_ai_config_schema_defaults_are_safe(self):
        schema = json.loads((_PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
        items = schema["ai_moderation"]["items"]
        self.assertFalse(items["enabled"]["default"])
        self.assertEqual(items["provider_id"]["default"], "deepseek-chat")
        self.assertEqual(items["confidence_threshold"]["default"], 0.90)
        self.assertEqual(items["timeout_seconds"]["default"], 8)
        self.assertEqual(items["context_messages"]["default"], 8)

    def test_core_exports_ai_handler_and_store(self):
        source = (_PLUGIN_DIR / "core" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("AIModerationHandler", source)
        self.assertIn("AIModerationStore", source)


if __name__ == "__main__":
    unittest.main()
