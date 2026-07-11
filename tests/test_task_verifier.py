import asyncio
import json
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
from task_planner import TaskStep  # noqa: E402
from task_verifier import (  # noqa: E402
    select_verification_command,
    run_verification_command,
    should_run_project_verification,
    verify_step,
)


class TaskVerifierTests(unittest.TestCase):
    def test_zero_exit_without_required_artifact_is_not_complete(self):
        step = TaskStep(
            "job123", 0, "生成 report.txt", ActionClass.WORKSPACE_WRITE, True
        )

        evidence = verify_step(
            step, exit_code=0, deliverables=[], verification_exit=None
        )

        self.assertFalse(evidence.verified)
        self.assertEqual(evidence.code, "artifact_missing")

    def test_wrong_artifact_type_does_not_complete_word_or_image_task(self):
        class Item:
            def __init__(self, name):
                self.path = Path(name)

        word = TaskStep(
            "job123", 0, "生成一份 Word 报告", ActionClass.WORKSPACE_WRITE, True
        )
        image = TaskStep(
            "job123", 0, "画一张图片", ActionClass.WORKSPACE_WRITE, True
        )

        self.assertEqual(verify_step(word, 0, [Item("answer.txt")], None).code, "artifact_type")
        self.assertEqual(verify_step(image, 0, [Item("answer.docx")], None).code, "artifact_type")
        self.assertTrue(verify_step(word, 0, [Item("answer.docx")], None).verified)

    def test_failed_verification_command_blocks_completion(self):
        step = TaskStep(
            "job123", 0, "检查 Python 测试", ActionClass.READ_ONLY, False
        )

        evidence = verify_step(
            step, exit_code=0, deliverables=[], verification_exit=1
        )

        self.assertFalse(evidence.verified)
        self.assertEqual(evidence.code, "verification_failed")

    def test_success_requires_execution_and_optional_verification_to_pass(self):
        step = TaskStep(
            "job123", 0, "读取项目", ActionClass.READ_ONLY, False
        )

        self.assertTrue(verify_step(step, 0, [], None).verified)
        self.assertFalse(verify_step(step, 2, [], None).verified)

    def test_selects_only_known_project_verification_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "package.json"
            package.write_text(
                json.dumps({"scripts": {"test": "node test.js"}}), encoding="utf-8"
            )
            self.assertEqual(select_verification_command(root), ["npm", "test"])

            package.unlink()
            (root / "tests").mkdir()
            command = select_verification_command(root)
            self.assertEqual(command[1:], ["-m", "unittest", "discover", "-s", "tests"])

    def test_project_verification_runs_only_for_code_change_or_test_steps(self):
        read_step = TaskStep(
            "job123", 0, "读取并总结报告", ActionClass.READ_ONLY, False
        )
        code_step = TaskStep(
            "job123", 1, "修复代码并运行测试", ActionClass.WORKSPACE_WRITE, False
        )

        self.assertFalse(should_run_project_verification(read_step))
        self.assertTrue(should_run_project_verification(code_step))

    def test_verification_command_returns_only_bounded_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = asyncio.run(
                run_verification_command(
                    [sys.executable, "-c", "print('private output not retained')"],
                    Path(tmp),
                    timeout=5,
                )
            )

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.reason, "completed")
            self.assertFalse(hasattr(result, "stdout"))


if __name__ == "__main__":
    unittest.main()
