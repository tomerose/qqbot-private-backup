import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AGENT_ROOT = PROJECT_ROOT / "astrbot" / "data" / "plugins" / "claude_code_agent"


class ExperienceBoundaryTests(unittest.TestCase):
    def test_experience_modules_do_not_own_tool_execution(self):
        forbidden = ("subprocess", "build_backend_command", "upload_group_file")
        for name in ("response_style.py", "progress_policy.py", "experience_memory.py"):
            source = (AGENT_ROOT / name).read_text(encoding="utf-8")
            with self.subTest(name=name):
                for token in forbidden:
                    self.assertNotIn(token, source)

    def test_access_policy_does_not_depend_on_experience_memory(self):
        source = (AGENT_ROOT / "access_policy.py").read_text(encoding="utf-8")

        self.assertNotIn("experience_memory", source)


if __name__ == "__main__":
    unittest.main()
