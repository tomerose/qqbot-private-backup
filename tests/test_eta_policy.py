import sys
import unittest
from pathlib import Path


PLUGINS_DIR = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS_DIR))

from claude_code_agent.action_policy import ActionClass  # noqa: E402
from claude_code_agent.eta_policy import estimate_eta  # noqa: E402
from claude_code_agent.task_planner import ExecutionPlan, TaskStep  # noqa: E402


def plan(*, steps: tuple[TaskStep, ...]) -> ExecutionPlan:
    return ExecutionPlan("job", "claude", steps)


def step(
    index: int,
    *,
    action: ActionClass = ActionClass.READ_ONLY,
    artifact: bool = False,
) -> TaskStep:
    return TaskStep("job", index, "task", action, artifact)


class EtaPolicyTests(unittest.TestCase):
    def test_artifact_task_gets_a_conservative_two_to_four_minute_eta(self):
        estimate = estimate_eta(plan(steps=(step(0, artifact=True),)), queue_ahead=0)

        self.assertEqual(estimate.text, "预计约 2–4 分钟")

    def test_queue_time_is_added_without_inspecting_task_content(self):
        estimate = estimate_eta(
            plan(steps=(step(0, artifact=True),)), queue_ahead=1
        )

        self.assertEqual(estimate.text, "预计约 4–7 分钟")

    def test_unknown_or_write_task_uses_the_highest_safe_range(self):
        estimate = estimate_eta(
            plan(steps=(step(0, action=ActionClass.UNKNOWN),)), queue_ahead=0
        )

        self.assertEqual(estimate.text, "预计约 3–8 分钟")


if __name__ == "__main__":
    unittest.main()
