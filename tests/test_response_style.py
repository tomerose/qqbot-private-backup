import sys
import unittest
from pathlib import Path


PLUGINS_DIR = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS_DIR))

from claude_code_agent.response_style import compact_text, format_task_reply  # noqa: E402


class ResponseStyleTests(unittest.TestCase):
    def test_completed_reply_leads_with_outcome_and_is_bounded(self):
        reply = format_task_reply("completed", "测试 12/12 通过。", "很长详情。" * 100)

        self.assertTrue(reply.startswith("已完成。"), reply)
        self.assertLessEqual(len(reply), 500)
        self.assertLessEqual(reply.count("。"), 4)

    def test_reply_redacts_paths_and_secrets_before_compaction(self):
        reply = compact_text(r"文件在 D:\private\a.txt，token=abcdef1234567890。")

        self.assertNotIn(r"D:\private", reply)
        self.assertNotIn("abcdef1234567890", reply)

    def test_failed_reply_uses_truthful_outcome_prefix(self):
        reply = format_task_reply("failed", "验证未通过。", "没有产生外发副作用。")

        self.assertTrue(reply.startswith("未完成。"), reply)

    def test_delivery_pending_is_not_reported_as_generation_failure(self):
        reply = format_task_reply("delivery_pending", "文件未成功交付。")

        self.assertTrue(reply.startswith("任务未完成，文件待交付。"), reply)


if __name__ == "__main__":
    unittest.main()
