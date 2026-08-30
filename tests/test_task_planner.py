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
    def test_plan_is_a_single_step_spanning_the_full_goal(self):
        request = TaskRequest(
            "job123", "生成报告，然后运行测试，再总结结果", "codex"
        )
        plan = plan_task(request)
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0].index, 0)
        self.assertEqual(plan.steps[0].task_id, "job123")
        self.assertEqual(plan.preferred_backend, "codex")
        # ponytail: single invocation — backend LLM plans internally.
        # The full goal survives intact, not regex-split into isolated clauses.
        self.assertIn("生成报告", plan.steps[0].instruction)
        self.assertIn("运行测试", plan.steps[0].instruction)
        self.assertIn("总结结果", plan.steps[0].instruction)

    def test_simple_unknown_task_stays_one_step_and_fail_closed(self):
        plan = plan_task(TaskRequest("job124", "处理一下这个项目", "claude"))
        self.assertEqual(len(plan.steps), 1)
        self.assertIs(plan.steps[0].action_class, ActionClass.UNKNOWN)
        self.assertFalse(plan.steps[0].expected_artifact)

    def test_artifact_keyword_detection_works_on_full_goal(self):
        plan = plan_task(TaskRequest("job125", "生成 Word 报告", "claude"))
        self.assertEqual(len(plan.steps), 1)
        self.assertTrue(plan.steps[0].expected_artifact)

    def test_existing_report_reference_does_not_require_a_new_artifact(self):
        plan = plan_task(TaskRequest("job126", "发送报告", "claude"))
        self.assertFalse(plan.steps[0].expected_artifact)

    def test_explicit_target_file_request_requires_an_artifact(self):
        plan = plan_task(TaskRequest("job123", "整理成一份 PDF 报告", "claude"))
        self.assertTrue(plan.steps[0].expected_artifact)


if __name__ == "__main__":
    unittest.main()
