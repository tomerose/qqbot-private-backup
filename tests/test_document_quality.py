import asyncio
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


PLUGIN_DIR = (
    Path(__file__).resolve().parents[1]
    / "astrbot"
    / "data"
    / "plugins"
    / "claude_code_agent"
)
sys.path.insert(0, str(PLUGIN_DIR))

import document_quality  # noqa: E402
from document_quality import inspect_docx_quality, render_docx, requires_research_quality  # noqa: E402


def write_docx(path: Path, text: str, links=()):
    document = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    relationships = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(
            f'<Relationship Id="rId{i}" Target="{url}" TargetMode="External" Type="link"/>'
            for i, url in enumerate(links, 1)
        )
        + "</Relationships>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)
        archive.writestr("word/_rels/document.xml.rels", relationships)


class DocumentQualityTests(unittest.TestCase):
    def test_generic_word_requires_readable_document_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "good.docx"
            empty = Path(tmp) / "empty.docx"
            write_docx(good, "有效内容")
            write_docx(empty, "")

            self.assertTrue(inspect_docx_quality(good, research=False).allowed)
            self.assertEqual(inspect_docx_quality(empty, research=False).code, "docx_empty")

    def test_research_word_requires_substance_and_multiple_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            document = Path(tmp) / "research.docx"
            write_docx(
                document,
                "人工智能研究内容" * 80,
                ("https://example.com/a", "https://example.org/b"),
            )

            self.assertTrue(inspect_docx_quality(document, research=True).allowed)

            weak = Path(tmp) / "weak.docx"
            write_docx(weak, "内容" * 300, ("https://example.com/a",))
            self.assertEqual(inspect_docx_quality(weak, research=True).code, "docx_sources")

            duplicate = Path(tmp) / "duplicate.docx"
            write_docx(
                duplicate,
                "内容" * 300 + " https://example.com/a",
                ("https://example.com/a",),
            )
            self.assertEqual(
                inspect_docx_quality(duplicate, research=True).code,
                "docx_sources",
            )

    def test_recent_github_report_is_research_quality(self):
        self.assertTrue(requires_research_quality("生成最近 AI 大事件的 Word 报告，参考 GitHub"))
        self.assertFalse(requires_research_quality("生成只包含 hello 的 Word"))

    def test_render_falls_back_to_structural_only_when_soffice_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            document = Path(tmp) / "valid.docx"
            write_docx(document, "正文内容")
            qa_dir = Path(tmp) / "qa"
            original = document_quality.SOFFICE_EXE
            document_quality.SOFFICE_EXE = Path(tmp) / "definitely-missing-soffice.exe"
            try:
                decision = asyncio.run(render_docx(document, qa_dir))
            finally:
                document_quality.SOFFICE_EXE = original
            self.assertTrue(decision.allowed)
            self.assertEqual(decision.code, "docx_structural_only")


if __name__ == "__main__":
    unittest.main()
