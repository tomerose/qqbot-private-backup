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


REVIEWER = "900000001"
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
            self.store.request_approval(application.application_id, "2000000000", 90, now=1_002)

        self.store.request_approval(application.application_id, REVIEWER, 90, now=1_002)
        code = self.store.confirm_approval(application.application_id, REVIEWER, now=1_003)
        self.assertGreaterEqual(len(code), 12)
        self.assertEqual(self.store.verify(APPLICANT, code, now=1_004), "active")
        self.assertTrue(self.store.is_active_pro(APPLICANT, now=1_004))
        with self.assertRaisesRegex(ProStoreError, "verification_invalid"):
            self.store.verify(APPLICANT, code, now=1_005)

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
        self.store.request_approval(application.application_id, REVIEWER, 90, now=1_002)
        code = self.store.confirm_approval(application.application_id, REVIEWER, now=1_003)

        for _ in range(3):
            with self.assertRaisesRegex(ProStoreError, "verification_invalid"):
                self.store.verify(APPLICANT, "wrong-code", now=1_004)
        with self.assertRaisesRegex(ProStoreError, "verification_locked"):
            self.store.verify(APPLICANT, code, now=1_005)

    def test_application_and_membership_expire_and_revoke(self):
        application = self.store.create_application(APPLICANT, now=1_000)
        with self.assertRaisesRegex(ProStoreError, "application_expired"):
            self.store.mark_sent(application.application_id, APPLICANT, now=1_000 + 72 * 3600 + 1)

        second = self.store.create_application(APPLICANT, now=1_000 + 72 * 3600 + 2)
        self.store.mark_sent(second.application_id, APPLICANT, now=1_000 + 72 * 3600 + 3)
        self.store.request_approval(second.application_id, REVIEWER, 1, now=1_000 + 72 * 3600 + 4)
        code = self.store.confirm_approval(second.application_id, REVIEWER, now=1_000 + 72 * 3600 + 5)
        self.store.verify(APPLICANT, code, now=1_000 + 72 * 3600 + 6)
        self.assertFalse(self.store.is_active_pro(APPLICANT, now=1_000 + 72 * 3600 + 6 + 86401))

        other = "3000000000"
        third = self.store.create_application(other, now=1_000 + 72 * 3600 + 6)
        self.store.mark_sent(third.application_id, other, now=1_000 + 72 * 3600 + 7)
        self.store.request_approval(third.application_id, REVIEWER, 90, now=1_000 + 72 * 3600 + 8)
        other_code = self.store.confirm_approval(third.application_id, REVIEWER, now=1_000 + 72 * 3600 + 9)
        self.store.verify(other, other_code, now=1_000 + 72 * 3600 + 10)
        with self.assertRaisesRegex(ProStoreError, "reviewer_required"):
            self.store.revoke(other, "2000000000", now=1_000 + 72 * 3600 + 11)
        self.assertTrue(self.store.revoke(other, REVIEWER, now=1_000 + 72 * 3600 + 11))

    def test_restart_does_not_resign_tampered_active_membership(self):
        self.store.grant(APPLICANT, REVIEWER, 30, now=1_000, tier="x")
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "UPDATE applications SET tier = 'pro' WHERE qq_id = ?",
                (APPLICANT,),
            )
            connection.commit()
        finally:
            connection.close()
        restarted = ProStore(self.path, reviewer_id=REVIEWER)
        self.assertFalse(restarted.is_active_pro(APPLICANT, now=1_001))

    def test_deactivated_group_can_be_reactivated(self):
        self.store.activate_group("12345678", REVIEWER, now=1_000)
        self.assertTrue(self.store.deactivate_group("12345678", REVIEWER, now=1_001))
        self.store.activate_group("12345678", REVIEWER, now=1_002)
        self.assertTrue(self.store.is_active_group("12345678", now=1_003))

    def test_permanent_x_grant_is_signed_and_does_not_expire(self):
        self.store.grant(
            APPLICANT,
            REVIEWER,
            now=1_000,
            tier="x",
            permanent=True,
        )
        self.assertTrue(self.store.is_active_pro(APPLICANT, now=1_000 + 36500 * 86400 - 1))
        self.assertFalse(self.store.is_active_pro(APPLICANT, now=1_000 + 36500 * 86400 + 1))

    def test_duration_is_bounded_and_pending_application_is_rate_limited(self):
        application = self.store.create_application(APPLICANT, now=1_000)
        with self.assertRaisesRegex(ProStoreError, "application_pending"):
            self.store.create_application(APPLICANT, now=1_001)
        self.store.mark_sent(application.application_id, APPLICANT, now=1_002)
        with self.assertRaisesRegex(ProStoreError, "duration_invalid"):
            self.store.request_approval(application.application_id, REVIEWER, 366, now=1_003)

    def test_direct_pro_grant_allows_the_configured_520_day_ceiling(self):
        self.store.grant(APPLICANT, REVIEWER, 520, now=1_000, tier="pro")
        self.assertTrue(self.store.is_active_pro(APPLICANT, now=1_000 + 519 * 86400))
        with self.assertRaisesRegex(ProStoreError, "duration_invalid"):
            self.store.grant("3000000000", REVIEWER, 521, now=1_000, tier="pro")

    def test_status_only_returns_the_callers_latest_application(self):
        application = self.store.create_application(APPLICANT, now=1_000)

        own = self.store.status_for(APPLICANT, now=1_001)
        other = self.store.status_for("3000000000", now=1_001)

        self.assertEqual(own.application_id, application.application_id)
        self.assertIsNone(other)

    def test_reviewer_can_list_and_deny_pending_application(self):
        application = self._awaiting_review_application()

        with self.assertRaisesRegex(ProStoreError, "reviewer_required"):
            self.store.pending_for_review("2000000000", now=1_002)
        self.assertEqual(
            [item.application_id for item in self.store.pending_for_review(REVIEWER, now=1_002)],
            [application.application_id],
        )
        with self.assertRaisesRegex(ProStoreError, "reviewer_required"):
            self.store.deny(application.application_id, "2000000000", now=1_003)
        self.assertTrue(self.store.deny(application.application_id, REVIEWER, now=1_003))
        self.assertEqual(self.store.status_for(APPLICANT, now=1_004).state, "denied")

    def test_reviewer_can_reset_undelivered_verification_without_exposing_code(self):
        application = self._awaiting_review_application()
        self.store.request_approval(application.application_id, REVIEWER, 90, now=1_002)
        self.store.confirm_approval(application.application_id, REVIEWER, now=1_003)

        with self.assertRaisesRegex(ProStoreError, "reviewer_required"):
            self.store.reset_verification(application.application_id, "2000000000", now=1_004)
        target = self.store.reset_verification(application.application_id, REVIEWER, now=1_004)

        self.assertEqual(target, APPLICANT)
        self.assertEqual(self.store.status_for(APPLICANT, now=1_004).state, "awaiting_review")

    def test_approval_requires_confirm_and_expiry_returns_to_review(self):
        application = self._awaiting_review_application()

        self.store.request_approval(application.application_id, REVIEWER, 90, now=1_002)

        self.assertEqual(
            self.store.status_for(APPLICANT, now=1_003).state,
            "approval_pending_confirm",
        )
        self.assertFalse(self.store.is_active_pro(APPLICANT, now=1_003))
        self.assertEqual(
            self.store.status_for(APPLICANT, now=1_002 + 301).state,
            "awaiting_review",
        )

    def test_resend_replaces_code_and_is_rate_limited(self):
        application = self._awaiting_review_application()
        self.store.request_approval(application.application_id, REVIEWER, 90, now=1_002)
        first_code = self.store.confirm_approval(application.application_id, REVIEWER, now=1_003)
        replacement_code = self.store.resend_verification(
            application.application_id, REVIEWER, now=1_064
        )

        with self.assertRaisesRegex(ProStoreError, "verification_invalid"):
            self.store.verify(APPLICANT, first_code, now=1_065)
        with self.assertRaisesRegex(ProStoreError, "resend_rate_limited"):
            self.store.resend_verification(application.application_id, REVIEWER, now=1_065)
        self.assertEqual(self.store.verify(APPLICANT, replacement_code, now=1_066), "active")

    def test_only_reviewer_can_read_minimal_audit_events(self):
        application = self._awaiting_review_application()

        with self.assertRaisesRegex(ProStoreError, "reviewer_required"):
            self.store.audit_for(application.application_id, "2000000000", now=1_002)
        events = self.store.audit_for(application.application_id, REVIEWER, now=1_002)

        self.assertEqual(events[0].event_type, "created")
        self.assertFalse(hasattr(events[0], "qq_id"))
        self.assertFalse(hasattr(events[0], "verification_code"))

    def test_active_membership_signature_rejects_tampered_record(self):
        application = self._awaiting_review_application()
        self.store.request_approval(application.application_id, REVIEWER, 90, now=1_002)
        code = self.store.confirm_approval(application.application_id, REVIEWER, now=1_003)
        self.store.verify(APPLICANT, code, now=1_004)

        connection = sqlite3.connect(self.path)
        try:
            signature = connection.execute(
                "SELECT membership_signature FROM applications WHERE application_id = ?",
                (application.application_id,),
            ).fetchone()[0]
            self.assertTrue(signature)
            connection.execute(
                "UPDATE applications SET pro_expires_at = pro_expires_at + 86400 WHERE application_id = ?",
                (application.application_id,),
            )
            connection.commit()
        finally:
            connection.close()

        self.assertFalse(self.store.is_active_pro(APPLICANT, now=1_005))

    def test_confirmation_timeout_is_audited(self):
        application = self._awaiting_review_application()
        self.store.request_approval(application.application_id, REVIEWER, 90, now=1_002)

        events = self.store.audit_for(application.application_id, REVIEWER, now=1_303)

        self.assertEqual(self.store.status_for(APPLICANT, now=1_303).state, "awaiting_review")
        self.assertIn("approval_confirmation_expired", [event.event_type for event in events])


if __name__ == "__main__":
    unittest.main()
