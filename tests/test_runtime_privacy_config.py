import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

from runtime_config_fixture import ensure_runtime_configs  # noqa: E402


class RuntimePrivacyConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_runtime_configs(ROOT)

    def test_astrbot_production_log_level_does_not_persist_info_chat_lines(self):
        config = json.loads(
            (ROOT / "astrbot" / "data" / "cmd_config.json").read_text(encoding="utf-8-sig")
        )

        self.assertIn(config["log_level"], {"WARNING", "ERROR", "CRITICAL"})
        self.assertFalse(config.get("trace_log_enable", False))

    def test_acl_script_is_scoped_and_never_deletes_history(self):
        script_path = ROOT / "services" / "harden_runtime_privacy.ps1"
        script = script_path.read_text(encoding="utf-8")

        self.assertIn("$projectRoot", script)
        self.assertIn("GetFullPath", script)
        self.assertIn("/inheritance:r", script)
        self.assertIn("Authenticated Users", script)
        self.assertIn("BUILTIN\\Users", script)
        self.assertIn("Everyone", script)
        self.assertIn("/remove:g", script)
        self.assertIn('"astrbot\\data"', script)
        self.assertIn('"claude_workspace"', script)
        self.assertNotIn("Remove-Item", script)
        self.assertNotIn("Clear-Content", script)
        self.assertNotIn("Set-Content", script)

    def test_agent_logs_never_persist_runtime_exception_or_provider_text(self):
        source = (
            ROOT / "astrbot" / "data" / "plugins" / "claude_code_agent" / "main.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn("redact_sensitive_text(str(exc))", source)
        self.assertNotIn("parse_failure(raw)", source)


if __name__ == "__main__":
    unittest.main()
