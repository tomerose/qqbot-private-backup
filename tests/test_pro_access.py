import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


PLUGINS_DIR = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS_DIR))

from draw_command.pro_access import is_active_pro  # noqa: E402
from pro_application.pro_store import ProStore  # noqa: E402


REVIEWER = "1211000567"
APPLICANT = "2000000000"


def activate(store: ProStore, qq_id: str, now: float = 1_000):
    app = store.create_application(qq_id, now=now)
    store.mark_sent(app.application_id, qq_id, now=now + 1)
    store.request_approval(app.application_id, REVIEWER, 90, now=now + 2)
    code = store.confirm_approval(app.application_id, REVIEWER, now=now + 3)
    store.verify(qq_id, code, now=now + 4)


class ProAccessTests(unittest.TestCase):
    def test_only_active_unexpired_membership_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pro_members.db"
            store = ProStore(path, reviewer_id=REVIEWER)
            activate(store, APPLICANT)

            self.assertTrue(is_active_pro(APPLICANT, path, now=1_005))
            self.assertFalse(is_active_pro(APPLICANT, path, now=1_005 + 91 * 86400))

    def test_invalid_membership_signature_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pro_members.db"
            store = ProStore(path, reviewer_id=REVIEWER)
            activate(store, APPLICANT)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE applications SET membership_signature = 'invalid' WHERE qq_id = ?",
                    (APPLICANT,),
                )
                connection.commit()
            finally:
                connection.close()

            self.assertFalse(store.is_active_pro(APPLICANT, now=1_005))
            self.assertFalse(is_active_pro(APPLICANT, path, now=1_005))

    def test_malformed_membership_record_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pro_members.db"
            store = ProStore(path, reviewer_id=REVIEWER)
            activate(store, APPLICANT)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE applications SET pro_expires_at = 'not-a-timestamp' WHERE qq_id = ?",
                    (APPLICANT,),
                )
                connection.commit()
            finally:
                connection.close()

            self.assertFalse(is_active_pro(APPLICANT, path, now=1_005))

    def test_missing_or_corrupt_database_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.db"
            broken = Path(tmp) / "broken.db"
            broken.write_text("not sqlite", encoding="utf-8")

            self.assertFalse(is_active_pro(APPLICANT, missing, now=1_000))
            self.assertFalse(is_active_pro(APPLICANT, broken, now=1_000))


if __name__ == "__main__":
    unittest.main()
