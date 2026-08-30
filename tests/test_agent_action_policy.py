import sys
import unittest
from pathlib import Path


PLUGIN_DIR = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins" / "claude_code_agent"
sys.path.insert(0, str(PLUGIN_DIR))

from action_policy import ActionClass, classify_action  # noqa: E402


class AgentActionPolicyTests(unittest.TestCase):
    def test_known_bypass_phrases_are_never_read_only(self):
        expected = {
            "覆盖旧配置并保存": ActionClass.HIGH_IMPACT,
            "把桌面文件挪到备份目录": ActionClass.HIGH_IMPACT,
            "调用脚本给好友分享报告": ActionClass.HIGH_IMPACT,
            "实现用户登录功能": ActionClass.HIGH_IMPACT,
            "运行项目自带部署脚本": ActionClass.HIGH_IMPACT,
        }

        for task, action_class in expected.items():
            with self.subTest(task=task):
                self.assertEqual(classify_action(task).action_class, action_class)

    def test_workspace_write_and_strict_read_only_are_distinct(self):
        self.assertEqual(
            classify_action("新建一份代码审查报告").action_class,
            ActionClass.WORKSPACE_WRITE,
        )
        self.assertEqual(
            classify_action("检查并解释现有代码结构").action_class,
            ActionClass.READ_ONLY,
        )

    def test_ambiguous_execution_defaults_to_unknown(self):
        self.assertEqual(
            classify_action("处理一下这个项目").action_class,
            ActionClass.UNKNOWN,
        )


if __name__ == "__main__":
    unittest.main()
