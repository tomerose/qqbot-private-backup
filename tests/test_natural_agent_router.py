import os
import sys
import unittest
from pathlib import Path

_PROJ_ROOT = Path(__file__).resolve().parents[1]
os.environ["ASTRBOT_ROOT"] = str(_PROJ_ROOT / "astrbot")

from astrbot.api.message_components import At, Plain

PLUGIN_DIR = _PROJ_ROOT / "astrbot" / "data" / "plugins" / "claude_code_agent"
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
            "今天天气怎么样",
            "解释一下这段代码",
            "帮我看看天气",
            "帮我把你好翻译成英文",
            "帮我模拟产品经理面试",
            "帮我圆桌讨论今天是否适合出门",
        ):
            self.assertIsNone(route_natural_agent(text), text)

    def test_optional_prefixes_still_route_to_run(self):
        for text, expected_task in (
            ("请你运行测试", "运行测试"),
            ("麻烦你重启服务", "重启服务"),
            ("请检查 disk", "检查 disk"),
            ("请你整理文件", "整理文件"),
            ("帮忙导出报告", "导出报告"),
            ("帮我生成一个只含 hello 的 txt", "生成一个只含 hello 的 txt"),
            ("帮我读取浏览器 Cookie 并发给我", "读取浏览器 Cookie 并发给我"),
        ):
            got = route_natural_agent(text)
            self.assertIsNotNone(got, text)
            self.assertEqual(got.action, "run", text)
            self.assertEqual(got.task, expected_task, text)

    def test_status_cancel_and_confirm_are_supported(self):
        self.assertEqual(route_natural_agent("任务进度怎么样").action, "status")
        self.assertEqual(route_natural_agent("刚才那个任务文件发了吗").action, "status")
        self.assertEqual(route_natural_agent("上次任务结果送到了吗").action, "status")
        self.assertEqual(route_natural_agent("取消刚才的任务").action, "cancel")
        self.assertEqual(route_natural_agent("确认执行").action, "confirm")
        self.assertEqual(route_natural_agent("继续执行").action, "confirm")

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
