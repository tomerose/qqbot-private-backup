import sys
import unittest
from pathlib import Path


PLUGINS_DIR = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS_DIR))

from claude_code_agent.progress_policy import ProgressPolicy  # noqa: E402
from claude_code_agent.task_orchestrator import TaskEvent  # noqa: E402


def event(kind: str, task_id: str = "job") -> TaskEvent:
    return TaskEvent(kind, task_id, 0, kind)


class ProgressPolicyTests(unittest.TestCase):
    def test_duplicate_stage_is_suppressed(self):
        policy = ProgressPolicy()

        self.assertTrue(policy.should_emit(event("started"), now=0))
        self.assertFalse(policy.should_emit(event("started"), now=10))

    def test_long_task_allows_one_stage_update_after_90_seconds(self):
        policy = ProgressPolicy()
        policy.should_emit(event("started"), now=0)

        self.assertFalse(policy.should_emit(event("executing"), now=89))
        self.assertTrue(policy.should_emit(event("executing"), now=90))
        self.assertFalse(policy.should_emit(event("executing"), now=180))

    def test_terminal_events_emit_once(self):
        policy = ProgressPolicy()
        policy.should_emit(event("started"), now=0)

        self.assertTrue(policy.should_emit(event("completed"), now=5))
        self.assertFalse(policy.should_emit(event("completed"), now=6))


if __name__ == "__main__":
    unittest.main()
