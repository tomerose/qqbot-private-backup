import sys
import tempfile
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
from agent_core import ApprovalRegistry  # noqa: E402
from step_policy import assess_step, step_digest  # noqa: E402
from task_planner import TaskStep  # noqa: E402


class StepPolicyTests(unittest.TestCase):
    def test_high_impact_step_needs_approval_even_when_goal_looked_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            output = root / "workspace" / "jobs" / "job123" / "outputs"
            work.mkdir()
            output.mkdir(parents=True)
            step = TaskStep(
                "job123", 1, "把报告发送给好友", ActionClass.HIGH_IMPACT, False
            )

            decision = assess_step(
                step,
                work,
                output,
                allowed_work_root=root,
                allowed_output_root=root / "workspace",
            )

            self.assertFalse(decision.allowed)
            self.assertTrue(decision.requires_approval)
            self.assertEqual(decision.code, "high_impact")

    def test_safe_step_requires_both_roots_to_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            output = root / "workspace" / "outputs"
            work.mkdir()
            output.mkdir(parents=True)
            step = TaskStep(
                "job123", 0, "读取项目", ActionClass.READ_ONLY, False
            )

            allowed = assess_step(
                step,
                work,
                output,
                allowed_work_root=root,
                allowed_output_root=root / "workspace",
            )
            blocked = assess_step(
                step,
                work,
                output,
                allowed_work_root=root / "elsewhere",
                allowed_output_root=root / "workspace",
            )

            self.assertTrue(allowed.allowed)
            self.assertFalse(blocked.allowed)
            self.assertTrue(blocked.requires_approval)
            self.assertEqual(blocked.code, "outside_work_root")

    def test_approval_cannot_cross_task_or_step(self):
        registry = ApprovalRegistry(ttl_seconds=300)
        step = TaskStep(
            "job123", 1, "发送报告", ActionClass.HIGH_IMPACT, False
        )
        digest = step_digest(step)
        pending = registry.issue(
            owner_id="1211000567",
            scope="private",
            task="发送报告",
            backend="claude",
            work_dir=Path(r"D:\work"),
            task_id="job123",
            step_digest=digest,
            now=1000,
        )

        self.assertIsNone(
            registry.consume(
                pending.token,
                "1211000567",
                "private",
                task_id="job124",
                step_digest=digest,
                now=1001,
            )
        )
        self.assertIsNone(
            registry.consume(
                pending.token,
                "1211000567",
                "private",
                task_id="job123",
                step_digest="0" * 64,
                now=1001,
            )
        )
        approved = registry.consume(
            pending.token,
            "1211000567",
            "private",
            task_id="job123",
            step_digest=digest,
            now=1001,
        )

        self.assertIsNotNone(approved)
        self.assertEqual(approved.task_id, "job123")


if __name__ == "__main__":
    unittest.main()
