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
    code = store.approve(app.application_id, REVIEWER, 90, now=now + 2)
    store.verify(qq_id, code, now=now + 3)


class ProAccessTests(unittest.TestCase):
    def test_only_active_unexpired_membership_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pro_members.db"
            store = ProStore(path, reviewer_id=REVIEWER)
            activate(store, APPLICANT)

            self.assertTrue(is_active_pro(APPLICANT, path, now=1_004))
            self.assertFalse(is_active_pro(APPLICANT, path, now=1_004 + 91 * 86400))

    def test_missing_or_corrupt_database_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.db"
            broken = Path(tmp) / "broken.db"
            broken.write_text("not sqlite", encoding="utf-8")

            self.assertFalse(is_active_pro(APPLICANT, missing, now=1_000))
            self.assertFalse(is_active_pro(APPLICANT, broken, now=1_000))


if __name__ == "__main__":
    unittest.main()
