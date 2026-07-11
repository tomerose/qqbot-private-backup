import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


PLUGIN_DIR = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins" / "claude_code_agent"
sys.path.insert(0, str(PLUGIN_DIR))

from delivery_dlp import inspect_deliverable, strip_image_metadata  # noqa: E402


class DeliveryDLPTests(unittest.TestCase):
    def test_clean_text_is_allowed_but_secret_and_local_path_are_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clean = root / "report.txt"
            clean.write_text("测试通过：3 项。", encoding="utf-8")
            secret = root / "ordinary-name.txt"
            secret.write_text("api_key=sk-abcdefghijklmnop123456", encoding="utf-8")
            private_path = root / "notes.md"
            private_path.write_text(r"请打开 D:\private\owner\chat.txt", encoding="utf-8")

            self.assertTrue(inspect_deliverable(clean, root).allowed)
            self.assertEqual(inspect_deliverable(secret, root).code, "sensitive_content")
            self.assertEqual(inspect_deliverable(private_path, root).code, "sensitive_content")

    def test_zip_rejects_sensitive_traversal_nested_and_oversized_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = {
                "secret.zip": (".env", b"TOKEN=private-value"),
                "traversal.zip": ("../outside.txt", b"data"),
                "nested.zip": ("archive.zip", b"PK\x03\x04nested"),
                "oversized.zip": ("huge.txt", b"x" * (5 * 1024 * 1024 + 1)),
            }
            for filename, (member, content) in cases.items():
                archive = root / filename
                with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
                    handle.writestr(member, content)
                with self.subTest(filename=filename):
                    self.assertFalse(inspect_deliverable(archive, root).allowed)

    def test_clean_code_zip_is_allowed_and_unknown_binary_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "source.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
                handle.writestr("src/main.py", "print('hello')")
                handle.writestr("README.md", "safe project")
            binary = root / "payload.exe"
            binary.write_bytes(b"MZ" + b"x" * 100)

            self.assertTrue(inspect_deliverable(archive, root).allowed)
            self.assertEqual(inspect_deliverable(binary, root).code, "unsupported_type")

    def test_clean_docx_with_xml_namespace_urls_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            document = root / "report.docx"
            with zipfile.ZipFile(document, "w", zipfile.ZIP_DEFLATED) as handle:
                handle.writestr(
                    "[Content_Types].xml",
                    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types" />',
                )
                handle.writestr(
                    "_rels/.rels",
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships" />',
                )
                handle.writestr(
                    "word/document.xml",
                    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                    "<w:body><w:p><w:r><w:t>Safe report</w:t></w:r></w:p></w:body>"
                    "</w:document>",
                )

            decision = inspect_deliverable(document, root)

            self.assertTrue(decision.allowed, decision.code)

    def test_docx_with_a_real_local_path_remains_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            document = root / "private.docx"
            with zipfile.ZipFile(document, "w", zipfile.ZIP_DEFLATED) as handle:
                handle.writestr(
                    "[Content_Types].xml",
                    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types" />',
                )
                handle.writestr(
                    "word/document.xml",
                    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                    "<w:body><w:p><w:r><w:t>C:\\private\\owner\\chat.txt</w:t></w:r></w:p></w:body>"
                    "</w:document>",
                )

            decision = inspect_deliverable(document, root)

            self.assertFalse(decision.allowed)
            self.assertEqual(decision.code, "sensitive_content")

    def test_outside_root_and_symlink_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "outputs"
            root.mkdir()
            outside = base / "outside.txt"
            outside.write_text("safe", encoding="utf-8")

            self.assertEqual(inspect_deliverable(outside, root).code, "outside_root")
            link = root / "link.txt"
            try:
                link.symlink_to(outside)
            except OSError:
                return
            self.assertEqual(inspect_deliverable(link, root).code, "symlink")

    def test_image_metadata_is_removed_only_inside_output_root(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "outputs"
            root.mkdir()
            image_path = root / "result.jpg"
            image = Image.new("RGB", (4, 4), "white")
            exif = Image.Exif()
            exif[0x010E] = "private local note"
            image.save(image_path, exif=exif)

            self.assertTrue(strip_image_metadata(image_path, root))
            with Image.open(image_path) as cleaned:
                self.assertEqual(len(cleaned.getexif()), 0)

            outside = Path(tmp) / "outside.jpg"
            image.save(outside, exif=exif)
            self.assertFalse(strip_image_metadata(outside, root))
            with Image.open(outside) as untouched:
                self.assertGreater(len(untouched.getexif()), 0)

    def test_png_text_metadata_is_blocked_before_delivery(self):
        from PIL import Image, PngImagePlugin

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_path = root / "result.png"
            metadata = PngImagePlugin.PngInfo()
            metadata.add_text("Description", "private local note")
            Image.new("RGB", (4, 4), "white").save(image_path, pnginfo=metadata)

            decision = inspect_deliverable(image_path, root)

            self.assertFalse(decision.allowed)
            self.assertEqual(decision.code, "image_metadata")


if __name__ == "__main__":
    unittest.main()
