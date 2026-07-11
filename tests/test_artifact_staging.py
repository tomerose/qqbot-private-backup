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

from artifact_staging import (  # noqa: E402
    collect_staged_artifacts,
    expected_artifact_suffixes,
    select_execution_dir,
)


class ArtifactStagingTests(unittest.TestCase):
    def test_expected_types_are_derived_from_user_request(self):
        self.assertEqual(expected_artifact_suffixes("生成一份 Word 报告"), {".docx"})
        self.assertIn(".png", expected_artifact_suffixes("画一张图片"))
        self.assertEqual(expected_artifact_suffixes("生成 report.pdf"), {".pdf"})

    def test_standalone_artifact_runs_in_private_job_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = root / "job"
            work = root / "project"
            job.mkdir()
            work.mkdir()

            self.assertEqual(select_execution_dir("生成一份 Word 报告", work, job), job)
            self.assertEqual(select_execution_dir("修改项目代码并生成报告", work, job), work)

    def test_collects_only_matching_direct_job_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "job"
            output = job / "outputs"
            nested = job / "nested"
            output.mkdir(parents=True)
            nested.mkdir()
            (job / "report.docx").write_bytes(b"docx")
            (job / "notes.txt").write_text("notes", encoding="utf-8")
            (nested / "private.docx").write_bytes(b"private")

            copied = collect_staged_artifacts(job, output, {".docx"})

            self.assertEqual([path.name for path in copied], ["report.docx"])
            self.assertTrue((output / "report.docx").is_file())
            self.assertFalse((output / "private.docx").exists())


if __name__ == "__main__":
    unittest.main()
