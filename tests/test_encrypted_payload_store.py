import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_PROJ_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = _PROJ_ROOT / "astrbot" / "data" / "plugins" / "claude_code_agent"
sys.path.insert(0, str(PLUGIN_DIR))

from encrypted_payload_store import (  # noqa: E402
    EncryptedJobPayload,
    EncryptedPayloadStore,
    PayloadIntegrityError,
)


@unittest.skipUnless(os.name == "nt", "DPAPI is Windows-only")
class EncryptedPayloadStoreTests(unittest.TestCase):
    def _payload(self):
        return EncryptedJobPayload(
            task=r"读取 C:\private\secret.txt 并生成报告",
            scope="aiocqhttp:GroupMessage:900000002",
            backend="claude",
            work_dir_relative="qqbot",
            recovery="replay_safe",
            delivery_cursor=("a" * 64,),
            plan=(
                {
                    "task_id": "abc123",
                    "index": 0,
                    "instruction": "读取项目",
                    "action_class": "read_only",
                    "expected_artifact": False,
                },
            ),
            step_cursor=0,
        )

    def test_payload_roundtrip_contains_no_plaintext_and_deletes_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "private"
            store = EncryptedPayloadStore(root)
            payload = self._payload()
            store.write("abc123", payload)

            path = root / "abc123.bin"
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(b"XNJ1"))
            self.assertNotIn(payload.task.encode("utf-8"), raw)
            self.assertNotIn(payload.scope.encode("utf-8"), raw)
            self.assertNotIn(b"private", raw.lower())
            self.assertEqual(store.read("abc123"), payload)
            self.assertTrue(store.exists("abc123"))

            store.delete("abc123")
            self.assertFalse(store.exists("abc123"))

    def test_tampered_payload_fails_with_generic_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "private"
            store = EncryptedPayloadStore(root)
            store.write("abc123", self._payload())
            path = root / "abc123.bin"
            raw = path.read_bytes()
            path.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))

            with self.assertRaises(PayloadIntegrityError) as caught:
                store.read("abc123")
            message = str(caught.exception)
            self.assertEqual(message, "加密任务载荷无效")
            self.assertNotIn("private", message.lower())

    def test_job_id_and_relative_workspace_are_strictly_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EncryptedPayloadStore(Path(tmp) / "private")
            with self.assertRaisesRegex(ValueError, "任务编号"):
                store.write("../escape", self._payload())
            absolute = EncryptedJobPayload(
                task="读取项目",
                scope="private",
                backend="claude",
                work_dir_relative=r"D:\secret",
                recovery="replay_safe",
            )
            with self.assertRaisesRegex(ValueError, "相对"):
                store.write("abc123", absolute)

    def test_recovery_root_itself_uses_dot_relative_identifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EncryptedPayloadStore(Path(tmp) / "private")
            payload = EncryptedJobPayload(
                task="读取项目",
                scope="private",
                backend="claude",
                work_dir_relative=".",
                recovery="blocked",
            )
            store.write("abc123", payload)
            self.assertEqual(store.read("abc123").work_dir_relative, ".")

    def test_private_directory_removes_inherited_user_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "private"
            EncryptedPayloadStore(root)
            output = subprocess.run(
                ["icacls.exe", str(root)],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            ).stdout.lower()
            self.assertNotIn("authenticated users", output)
            self.assertNotIn("builtin\\users", output)

    def test_plan_and_step_cursor_are_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EncryptedPayloadStore(Path(tmp) / "private")
            oversized = EncryptedJobPayload(
                task="读取项目",
                scope="private",
                backend="claude",
                work_dir_relative=".",
                recovery="replay_safe",
                plan=tuple(
                    {
                        "task_id": "abc123",
                        "index": index,
                        "instruction": "读取项目",
                        "action_class": "read_only",
                        "expected_artifact": False,
                    }
                    for index in range(9)
                ),
            )
            with self.assertRaisesRegex(ValueError, "计划"):
                store.write("abc123", oversized)

            invalid_cursor = EncryptedJobPayload(
                task="读取项目",
                scope="private",
                backend="claude",
                work_dir_relative=".",
                recovery="replay_safe",
                plan=(
                    {
                        "task_id": "abc123",
                        "index": 0,
                        "instruction": "读取项目",
                        "action_class": "read_only",
                        "expected_artifact": False,
                    },
                ),
                step_cursor=2,
            )
            with self.assertRaisesRegex(ValueError, "步骤游标"):
                store.write("abc123", invalid_cursor)


if __name__ == "__main__":
    unittest.main()
