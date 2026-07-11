import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["ASTRBOT_ROOT"] = str(ROOT / "astrbot")
sys.path.insert(0, str(ROOT / "astrbot" / "data" / "plugins"))

from astrbot.core.star.star_handler import star_handlers_registry  # noqa: E402
import claude_code_agent.main as agent_main  # noqa: E402


class AgentPluginRegistrationTests(unittest.TestCase):
    def test_agent_imports_through_astrbot_package_path(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from data.plugins.claude_code_agent.main import ClaudeCodeAgent; print(ClaudeCodeAgent.__name__)",
            ],
            cwd=ROOT / "astrbot",
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ClaudeCodeAgent")

    def test_agent_message_entry_is_registered_with_astrbot(self):
        handlers = star_handlers_registry.get_handlers_by_module_name(agent_main.__name__)
        names = {handler.handler_name for handler in handlers}

        self.assertIn("on_message", names)
        self.assertIn("protect_privacy_and_deliver_files", names)


if __name__ == "__main__":
    unittest.main()
