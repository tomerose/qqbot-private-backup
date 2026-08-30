import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReportOnlyRuntimeConfigTests(unittest.TestCase):
    def test_only_shutdown_gate_and_scheduler_plugins_are_enabled(self):
        config = json.loads((ROOT / "astrbot/data/cmd_config.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(config["plugin_set"], ["chat_router", "xiaoning_scheduled"])
        self.assertTrue(config["disable_builtin_commands"])
        weixin = next(item for item in config["platform"] if item["type"] == "weixin_oc")
        self.assertFalse(weixin["enable"])

    def test_scheduler_config_explicitly_enables_only_three_reports(self):
        config = json.loads(
            (ROOT / "astrbot/data/config/xiaoning_scheduled_config.json").read_text(
                encoding="utf-8-sig"
            )
        )
        self.assertTrue(config["report_only_mode"])
        self.assertTrue(config["ai_news_enabled"])
        self.assertTrue(config["noon_report_enabled"])
        self.assertTrue(config["evening_report_enabled"])
        for key in (
            "github_trending_enabled", "morning_post_enabled", "weather_enabled",
            "beautiful_moment_enabled", "zhoushen_daily_enabled",
            "zhoushen_song_enabled", "zhoushen_meme_enabled",
        ):
            self.assertFalse(config[key], key)

        schema = json.loads(
            (ROOT / "astrbot/data/plugins/xiaoning_scheduled/_conf_schema.json").read_text(
                encoding="utf-8-sig"
            )
        )
        self.assertTrue(schema["report_only_mode"]["default"])
        self.assertTrue(schema["ai_news_enabled"]["default"])
        self.assertTrue(schema["noon_report_enabled"]["default"])
        self.assertTrue(schema["evening_report_enabled"]["default"])


if __name__ == "__main__":
    unittest.main()
