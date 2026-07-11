import os
import sys
import unittest
from pathlib import Path

os.environ["ASTRBOT_ROOT"] = r"D:\Claudecoda学习\qqbot\astrbot"

from astrbot.api.message_components import At, Plain

PLUGIN_DIR = Path(r"D:\Claudecoda学习\qqbot\astrbot\data\plugins\claude_code_agent")
sys.path.insert(0, str(PLUGIN_DIR))

from natural_router import (  # noqa: E402
    NaturalAgentIntent,
    extract_natural_agent_text,
    route_natural_agent,
)


class NaturalAgentRouterTests(unittest.TestCase):
    def test_explicit_task_routes_without_slash_and_extracts_backend(self):
        self.assertEqual(
            route_natural_agent("帮我用 Codex 检查项目并生成报告"),
            NaturalAgentIntent("run", "检查项目并生成报告", "codex"),
        )
        self.assertEqual(
            route_natural_agent("小柠，请你用Claude整理文件"),
            NaturalAgentIntent("run", "整理文件", "claude"),
        )

    def test_chat_and_ambiguous_questions_do_not_execute(self):
        for text in (
            "你觉得这个项目怎么样",
            "能不能写代码",
            "今天心情怎么样",
            "小柠真聪明",
        ):
            self.assertIsNone(route_natural_agent(text), text)

    def test_status_cancel_and_confirm_are_supported(self):
        self.assertEqual(route_natural_agent("任务进度怎么样").action, "status")
        self.assertEqual(route_natural_agent("取消刚才的任务").action, "cancel")
        self.assertEqual(route_natural_agent("确认执行").action, "confirm")

    def test_private_text_is_allowed_but_group_requires_real_at_self(self):
        self.assertEqual(
            extract_natural_agent_text(
                "帮我生成报告", [Plain("帮我生成报告")], "3806573022", ""
            ),
            "帮我生成报告",
        )
        self.assertEqual(
            extract_natural_agent_text(
                "帮我生成报告",
                [At(qq="3806573022"), Plain("帮我生成报告")],
                "3806573022",
                "945598390",
            ),
            "帮我生成报告",
        )
        self.assertEqual(
            extract_natural_agent_text(
                "帮我生成报告",
                [Plain("@小柠 帮我生成报告")],
                "3806573022",
                "945598390",
            ),
            "",
        )
        self.assertEqual(
            extract_natural_agent_text(
                "帮我生成报告",
                [At(qq="999"), Plain("帮我生成报告")],
                "3806573022",
                "945598390",
            ),
            "",
        )

    def test_empty_or_oversized_natural_task_is_rejected(self):
        self.assertIsNone(route_natural_agent("帮我   "))
        with self.assertRaisesRegex(ValueError, "过长"):
            route_natural_agent("帮我" + "x" * 3001)


if __name__ == "__main__":
    unittest.main()
