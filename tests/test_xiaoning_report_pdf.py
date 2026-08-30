import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "astrbot/data/plugins/xiaoning_scheduled/pdf_utils.py"


def load_pdf_utils():
    spec = importlib.util.spec_from_file_location("xiaoning_report_pdf_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class XiaoningReportPdfTests(unittest.TestCase):
    def test_markdown_lists_links_and_emphasis_keep_structure(self):
        module = load_pdf_utils()
        rendered = module._md_to_html(
            "## 核心信息\n"
            "- 第一条\n"
            "- 第二条\n"
            "* 星号列表\n\n"
            "1. 第一步\n"
            "2. 第二步\n\n"
            "**判断：** 详情见 https://example.com/news?id=1&lang=zh。"
        )

        self.assertIn("<ul>", rendered)
        self.assertEqual(rendered.count("<li>"), 5)
        self.assertIn("<ol>", rendered)
        self.assertIn("<strong>判断：</strong>", rendered)
        self.assertIn('href="https://example.com/news?id=1&amp;lang=zh"', rendered)
        self.assertNotIn("&amp;amp;", rendered)

    def test_standard_markdown_links_emphasis_and_rules_render_cleanly(self):
        module = load_pdf_utils()
        rendered = module._md_to_html(
            "详情见 [Global News](https://example.com/news?id=1&lang=zh)。\n"
            "*为什么值得关注*\n\n"
            "---\n\n"
            "下一节"
        )

        self.assertIn(
            '<a href="https://example.com/news?id=1&amp;lang=zh">Global News</a>',
            rendered,
        )
        self.assertNotIn("[Global News]", rendered)
        self.assertIn("<em>为什么值得关注</em>", rendered)
        self.assertIn("<hr/>", rendered)

    def test_trailing_report_signoff_does_not_leave_a_separator_or_plain_paragraph(self):
        module = load_pdf_utils()
        rendered = module._md_to_html(
            "正文\n\n---\n由小柠自动生成 · 回复「早报关闭」可退订"
        )

        self.assertNotIn("<hr/>", rendered)
        self.assertIn('class="report-signoff"', rendered)

    def test_report_template_uses_report_specific_running_copy(self):
        module = load_pdf_utils()
        rendered = module.render_document(
            "早间简报", "每日 AI 早报", "2026-08-26", [("", "正文")]
        )

        self.assertIn("小柠定时报送", rendered)
        self.assertIn("早报 · 午报 · 晚报", rendered)
        self.assertIn('class="cover-title"', rendered)
        self.assertNotIn("<h0>", rendered)
        self.assertNotIn("小柠的心意", rendered)
        self.assertNotIn("给每一位同行者的信", rendered)

    def test_render_pdf_accepts_relative_path_and_rejects_missing_output(self):
        module = load_pdf_utils()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relative = Path(Path(directory).name) / "report.pdf"
            workdir = Path.cwd()
            try:
                import os
                os.chdir(root.parent)

                def write_pdf(command, **_kwargs):
                    output_arg = next(arg for arg in command if arg.startswith("--print-to-pdf="))
                    Path(output_arg.split("=", 1)[1]).write_bytes(b"%PDF-1.4\n" + b"x" * 128)
                    return subprocess.CompletedProcess(command, 0)

                with patch.object(module.subprocess, "run", side_effect=write_pdf) as run:
                    result = module.render_pdf("<html>ok</html>", relative)
                self.assertTrue(result.is_absolute())
                self.assertTrue(result.is_file())
                self.assertIn("file:///", run.call_args.args[0][-1])

                with patch.object(
                    module.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0),
                ):
                    with self.assertRaises(RuntimeError):
                        module.render_pdf(
                            "<html>missing</html>",
                            Path(Path(directory).name) / "missing.pdf",
                        )
            finally:
                os.chdir(workdir)


if __name__ == "__main__":
    unittest.main()
