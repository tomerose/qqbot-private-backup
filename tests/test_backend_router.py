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
from backend_router import can_retry, route_backend  # noqa: E402
from task_planner import TaskStep  # noqa: E402


def make_step(instruction, action_class=ActionClass.READ_ONLY):
    return TaskStep("job123", 0, instruction, action_class, False)


class BackendRouterTests(unittest.TestCase):
    def test_code_honors_claude_preference_then_uses_codex(self):
        step = make_step("检查并解释 Python 测试")

        first = route_backend(step, "claude", {"codex", "claude"}, set())
        second = route_backend(
            step, "claude", {"codex", "claude"}, {"claude"}
        )

        self.assertEqual(first.backend, "claude")
        self.assertEqual(second.backend, "codex")

    def test_github_research_honors_claude_preference(self):
        route = route_backend(
            make_step("参考 GitHub 公开项目生成最近 AI 大事件 Word 报告"),
            "claude",
            {"claude", "codex"},
            set(),
        )

        self.assertEqual(route.backend, "claude")

    def test_desktop_task_prefers_workbuddy(self):
        route = route_backend(
            make_step("查看桌面软件窗口"),
            "claude",
            {"claude", "workbuddy"},
            set(),
        )

        self.assertEqual(route.backend, "workbuddy")

    def test_no_available_backend_fails_closed(self):
        route = route_backend(
            make_step("分析报告"), "claude", {"claude"}, {"claude"}
        )

        self.assertIsNone(route.backend)
        self.assertEqual(route.code, "no_backend")

    def test_retry_is_limited_to_unstarted_read_only_steps(self):
        read_step = make_step("读取项目")
        write_step = make_step("生成报告", ActionClass.WORKSPACE_WRITE)

        self.assertTrue(can_retry(read_step, started_side_effect=False, attempts=1))
        self.assertFalse(can_retry(read_step, started_side_effect=False, attempts=2))
        self.assertFalse(can_retry(read_step, started_side_effect=True, attempts=1))
        self.assertTrue(can_retry(write_step, started_side_effect=False, attempts=1))
        high = make_step("发送报告", ActionClass.HIGH_IMPACT)
        self.assertFalse(can_retry(high, started_side_effect=False, attempts=1))


if __name__ == "__main__":
    unittest.main()
