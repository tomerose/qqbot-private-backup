import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


PLUGINS = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS))

from xiaoning_runtime import (  # noqa: E402
    ArtifactQualityResult,
    _normalize_qq_text_artifact,
    _record_delivery_manifest,
    inspect_local_artifact,
)


class ArtifactQualityTests(unittest.TestCase):
    def test_qq_text_is_normalized_to_utf8_bom(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            utf8 = root / "utf8.md"
            utf8.write_text("中文报告", encoding="utf-8")
            gbk = root / "gbk.txt"
            gbk.write_bytes("中文说明".encode("gb18030"))

            self.assertTrue(_normalize_qq_text_artifact(utf8))
            self.assertTrue(_normalize_qq_text_artifact(gbk))
            for artifact, expected in ((utf8, "中文报告"), (gbk, "中文说明")):
                self.assertTrue(artifact.read_bytes().startswith(b"\xef\xbb\xbf"))
                self.assertEqual(artifact.read_text(encoding="utf-8-sig"), expected)
                self.assertFalse(_normalize_qq_text_artifact(artifact))

    def test_rejects_empty_wrong_magic_and_broken_office(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            empty = root / "empty.pdf"
            empty.write_bytes(b"")
            fake_video = root / "fake.mp4"
            fake_video.write_bytes(b"not a video")
            fake_word = root / "fake.docx"
            fake_word.write_bytes(b"not a zip")
            self.assertEqual(inspect_local_artifact(empty).code, "empty")
            self.assertEqual(inspect_local_artifact(fake_video).code, "format_magic")
            self.assertEqual(inspect_local_artifact(fake_word).code, "archive_invalid")

    def test_accepts_real_format_signatures_and_office_zip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "clip.mp4"
            video.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32)
            document = root / "report.docx"
            with zipfile.ZipFile(document, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
            self.assertTrue(inspect_local_artifact(video).allowed)
            self.assertTrue(inspect_local_artifact(document).allowed)

    def test_manifest_contains_evidence_but_no_local_path_or_qq(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "answer.md"
            artifact.write_text("# answer", encoding="utf-8")
            quality = inspect_local_artifact(artifact)
            manifest = _record_delivery_manifest(
                artifact,
                quality,
                kind="file",
                channel="private",
                delivered=True,
                target_scope="private",
                root=root / "manifests",
            )
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(payload["file_name"], "answer.md")
            self.assertEqual(payload["quality_code"], "valid")
            self.assertNotIn(str(root), manifest.read_text(encoding="utf-8"))
            self.assertNotIn("1211000567", manifest.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
