import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN_DIR = (
    Path(__file__).resolve().parents[1]
    / "astrbot"
    / "data"
    / "plugins"
    / "pro_application"
)
sys.path.insert(0, str(PLUGIN_DIR))

from pro_store import ProStore, ProStoreError  # noqa: E402


REVIEWER = "1211000567"
APPLICANT = "2000000000"


class ProStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "pro_members.db"
        self.store = ProStore(self.path, reviewer_id=REVIEWER)

    def tearDown(self):
        self.temp.cleanup()

    def _awaiting_review_application(self):
        application = self.store.create_application(APPLICANT, now=1_000)
        self.store.mark_sent(application.application_id, APPLICANT, now=1_001)
        return application

    def test_same_qq_must_confirm_email_submission(self):
        application = self.store.create_application(APPLICANT, now=1_000)

        with self.assertRaisesRegex(ProStoreError, "application_owner"):
            self.store.mark_sent(application.application_id, "3000000000", now=1_001)

        updated = self.store.mark_sent(application.application_id, APPLICANT, now=1_001)
        self.assertEqual(updated.state, "awaiting_review")

    def test_reviewer_approval_uses_one_time_hashed_code(self):
        application = self._awaiting_review_application()

        with self.assertRaisesRegex(ProStoreError, "reviewer_required"):
            self.store.approve(application.application_id, "2000000000", 90, now=1_002)

        code = self.store.approve(application.application_id, REVIEWER, 90, now=1_002)
        self.assertGreaterEqual(len(code), 12)
        self.assertEqual(self.store.verify(APPLICANT, code, now=1_003), "active")
        self.assertTrue(self.store.is_active_pro(APPLICANT, now=1_003))
        with self.assertRaisesRegex(ProStoreError, "verification_invalid"):
            self.store.verify(APPLICANT, code, now=1_004)

        connection = sqlite3.connect(self.path)
        try:
            stored = connection.execute(
                "SELECT verification_code_hash FROM applications WHERE application_id = ?",
                (application.application_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertNotEqual(stored, code)
        self.assertEqual(len(stored), 64)

    def test_codes_expire_and_lock_after_three_wrong_attempts(self):
        application = self._awaiting_review_application()
        code = self.store.approve(application.application_id, REVIEWER, 90, now=1_002)

        for _ in range(3):
            with self.assertRaisesRegex(ProStoreError, "verification_invalid"):
                self.store.verify(APPLICANT, "wrong-code", now=1_003)
        with self.assertRaisesRegex(ProStoreError, "verification_locked"):
            self.store.verify(APPLICANT, code, now=1_004)

    def test_application_and_membership_expire_and_revoke(self):
        application = self.store.create_application(APPLICANT, now=1_000)
        with self.assertRaisesRegex(ProStoreError, "application_expired"):
            self.store.mark_sent(application.application_id, APPLICANT, now=1_000 + 72 * 3600 + 1)

        second = self.store.create_application(APPLICANT, now=1_000 + 72 * 3600 + 2)
        self.store.mark_sent(second.application_id, APPLICANT, now=1_000 + 72 * 3600 + 3)
        code = self.store.approve(second.application_id, REVIEWER, 1, now=1_000 + 72 * 3600 + 4)
        self.store.verify(APPLICANT, code, now=1_000 + 72 * 3600 + 5)
        self.assertFalse(self.store.is_active_pro(APPLICANT, now=1_000 + 72 * 3600 + 5 + 86401))

        other = "3000000000"
        third = self.store.create_application(other, now=1_000 + 72 * 3600 + 6)
        self.store.mark_sent(third.application_id, other, now=1_000 + 72 * 3600 + 7)
        other_code = self.store.approve(third.application_id, REVIEWER, 90, now=1_000 + 72 * 3600 + 8)
        self.store.verify(other, other_code, now=1_000 + 72 * 3600 + 9)
        with self.assertRaisesRegex(ProStoreError, "reviewer_required"):
            self.store.revoke(other, "2000000000", now=1_000 + 72 * 3600 + 10)
        self.assertTrue(self.store.revoke(other, REVIEWER, now=1_000 + 72 * 3600 + 10))

    def test_duration_is_bounded_and_pending_application_is_rate_limited(self):
        application = self.store.create_application(APPLICANT, now=1_000)
        with self.assertRaisesRegex(ProStoreError, "application_pending"):
            self.store.create_application(APPLICANT, now=1_001)
        self.store.mark_sent(application.application_id, APPLICANT, now=1_002)
        with self.assertRaisesRegex(ProStoreError, "duration_invalid"):
            self.store.approve(application.application_id, REVIEWER, 366, now=1_003)


if __name__ == "__main__":
    unittest.main()
