import sys
import tempfile
import unittest
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins" / "claude_code_agent"
sys.path.insert(0, str(_PLUGIN_DIR))

from recovery_policy import assess_recovery  # noqa: E402


class AgentRecoveryPolicyTests(unittest.TestCase):
    def test_only_strict_read_only_work_inside_base_is_resumable(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            work = base / "project"
            work.mkdir()
            for task in (
                "读取并解释现有代码",
                "搜索项目并总结当前结构",
                "检查代码结构并分析潜在问题",
            ):
                result = assess_recovery(task, work, base)
                self.assertTrue(result.resumable, (task, result))
                self.assertEqual(result.reason, "replay_safe")

            for task in (
                "读取项目并生成新报告",
                "分析代码并运行测试",
                "总结文档并新建结果文件",
                "处理一下这个项目",
            ):
                result = assess_recovery(task, work, base)
                self.assertFalse(result.resumable, (task, result))
                self.assertEqual(result.reason, "action_not_read_only")

    def test_non_idempotent_or_external_side_effects_are_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for task in (
                "删除旧文件",
                "覆盖原始数据",
                "安装软件",
                "修改系统权限",
                "登录网站并提交表单",
                "把结果发送给其他人",
                "上传文件到网盘",
            ):
                result = assess_recovery(task, base, base)
                self.assertFalse(result.resumable, task)
                self.assertEqual(result.reason, "action_not_read_only")

    def test_work_directory_outside_recovery_root_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "allowed"
            outside = root / "outside"
            base.mkdir()
            outside.mkdir()
            result = assess_recovery("读取并解释项目", outside, base)
            self.assertFalse(result.resumable)
            self.assertEqual(result.reason, "outside_recovery_root")


if __name__ == "__main__":
    unittest.main()
