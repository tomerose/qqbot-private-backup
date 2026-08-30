import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_PARENT = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot_plugin_qqadmin.core.ai_moderation_store import AIModerationStore  # noqa: E402


class AIModerationStoreTests(unittest.TestCase):
    def test_store_never_contains_raw_ids_or_message_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "private" / "moderation.db"
            salt = Path(tmp) / "private" / "audit_salt.bin"
            store = AIModerationStore(db, salt)
            store.set_enabled("900000002", True)
            store.record_action(
                "900000002",
                "123456789",
                "recall",
                "repeated_spam",
                0.96,
                True,
                now=1000,
            )
            raw = db.read_bytes()
            for forbidden in (b"900000002", b"123456789", b"private-message-body"):
                self.assertNotIn(forbidden, raw)
            self.assertTrue(store.is_enabled("900000002"))
            self.assertFalse(store.is_enabled("111111111"))

            enabled_by_default = AIModerationStore(db, salt, default_enabled=True)
            self.assertTrue(enabled_by_default.is_enabled("111111111"))

            conn = sqlite3.connect(db)
            try:
                columns = {
                    row[1]
                    for table in ("settings", "offenses", "audit")
                    for row in conn.execute(f"PRAGMA table_info({table})")
                }
            finally:
                conn.close()
            for forbidden_column in ("message", "content", "response", "group_id", "user_id"):
                self.assertNotIn(forbidden_column, columns)

    def test_offenses_expire_after_24_hours_and_only_success_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AIModerationStore(
                Path(tmp) / "private" / "moderation.db",
                Path(tmp) / "private" / "audit_salt.bin",
            )
            store.record_action("g", "u", "recall", "repeated_spam", 0.95, True, now=1000)
            store.record_action("g", "u", "recall", "repeated_spam", 0.95, False, now=1001)
            self.assertEqual(store.offense_count("g", "u", now=1002), 1)
            self.assertEqual(store.offense_count("g", "u", now=1000 + 86401), 0)

    @unittest.skipUnless(os.name == "nt", "Windows ACL assertion")
    def test_private_directory_removes_inherited_acl(self):
        with tempfile.TemporaryDirectory() as tmp:
            private = Path(tmp) / "private"
            AIModerationStore(private / "moderation.db", private / "audit_salt.bin")
            import subprocess

            output = subprocess.run(
                ["icacls.exe", str(private)],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            ).stdout.lower()
            self.assertNotIn("authenticated users", output)
            self.assertNotIn("builtin\\users", output)


if __name__ == "__main__":
    unittest.main()
