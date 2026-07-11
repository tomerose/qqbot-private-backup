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
from task_planner import TaskRequest, plan_task  # noqa: E402


class TaskPlannerTests(unittest.TestCase):
    def test_plan_is_bounded_and_preserves_order(self):
        request = TaskRequest(
            "job123", "生成报告，然后运行测试，再总结结果", "codex"
        )

        plan = plan_task(request)

        self.assertGreaterEqual(len(plan.steps), 1)
        self.assertLessEqual(len(plan.steps), 8)
        self.assertEqual(
            [step.index for step in plan.steps], list(range(len(plan.steps)))
        )
        self.assertTrue(all(step.task_id == "job123" for step in plan.steps))
        self.assertEqual(plan.steps[0].instruction, "生成报告")
        self.assertEqual(plan.steps[-1].instruction, "总结结果")

    def test_simple_unknown_task_stays_one_step_and_fail_closed(self):
        plan = plan_task(TaskRequest("job124", "处理一下这个项目", "claude"))

        self.assertEqual(len(plan.steps), 1)
        self.assertIs(plan.steps[0].action_class, ActionClass.UNKNOWN)
        self.assertFalse(plan.steps[0].expected_artifact)

    def test_plan_never_creates_more_than_eight_steps(self):
        goal = "，然后".join(f"读取文件{i}" for i in range(12))

        plan = plan_task(TaskRequest("job125", goal, "claude"))

        self.assertEqual(len(plan.steps), 8)

    def test_existing_report_reference_does_not_require_a_new_artifact(self):
        plan = plan_task(TaskRequest("job126", "发送报告", "claude"))

        self.assertFalse(plan.steps[0].expected_artifact)


if __name__ == "__main__":
    unittest.main()
