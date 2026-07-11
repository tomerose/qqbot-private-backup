import sys
import unittest
from pathlib import Path


PLUGIN_DIR = (
    Path(__file__).resolve().parents[1]
    / "astrbot"
    / "data"
    / "plugins"
    / "claude_code_agent"
)
sys.path.insert(0, str(PLUGIN_DIR))

from action_policy import ActionClass  # noqa: E402
from isolation_policy import choose_isolation  # noqa: E402
from task_planner import TaskStep  # noqa: E402


class IsolationPolicyTests(unittest.TestCase):
    def test_unknown_step_requires_validated_isolation_adapter(self):
        step = TaskStep("job123", 0, "处理一下", ActionClass.UNKNOWN, False)

        unavailable = choose_isolation(step, sandbox_ready=False)
        ready = choose_isolation(step, sandbox_ready=True)

        self.assertEqual(unavailable.mode, "blocked")
        self.assertEqual(unavailable.code, "isolation_unavailable")
        self.assertEqual(ready.mode, "windows_sandbox")

    def test_known_actions_keep_existing_host_policy(self):
        for action in (
            ActionClass.READ_ONLY,
            ActionClass.WORKSPACE_WRITE,
            ActionClass.HIGH_IMPACT,
        ):
            with self.subTest(action=action):
                step = TaskStep("job123", 0, "known", action, False)
                self.assertEqual(choose_isolation(step, False).mode, "host")


if __name__ == "__main__":
    unittest.main()
