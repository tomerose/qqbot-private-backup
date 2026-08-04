import sys
import tempfile
import unittest
from pathlib import Path


PLUGINS_DIR = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS_DIR))

from draw_command.pro_access import (  # noqa: E402
    Tier,
    agent_available,
    get_tier,
    is_active_pro,
    use_agent,
)


class ProAccessTests(unittest.TestCase):
    """Open-access contract: every QQ user is treated as Tier.X (unified access)."""

    def test_get_tier_returns_x_for_everyone(self):
        self.assertEqual(get_tier("2000000000", Path("missing.db")), Tier.X)

    def test_tier_ignores_missing_or_corrupt_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.db"
            broken = Path(tmp) / "broken.db"
            broken.write_text("not sqlite", encoding="utf-8")
            self.assertEqual(get_tier("2000000000", missing), Tier.X)
            self.assertEqual(get_tier("2000000000", broken), Tier.X)

    def test_agent_available_to_everyone(self):
        self.assertEqual(agent_available("2000000000", Path("missing.db")), (True, ""))

    def test_agent_usage_is_unlimited_for_all(self):
        self.assertTrue(use_agent("2000000000", Path("missing.db")))
        self.assertTrue(use_agent("2000000000", Path("missing.db")))

    def test_is_active_pro_is_true_for_all(self):
        self.assertTrue(is_active_pro("2000000000", Path("missing.db")))
        self.assertTrue(is_active_pro("2000000000", Path("missing.db")))


if __name__ == "__main__":
    unittest.main()
