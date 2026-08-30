import sys
import tempfile
import time
import unittest
from pathlib import Path


PLUGINS = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "astrbot"))

from data.plugins.custom_draw.main import CustomDraw  # noqa: E402


class CustomDrawStateTests(unittest.TestCase):
    def test_pending_request_and_daily_usage_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            today = time.strftime("%Y%m%d")
            original = CustomDraw.__new__(CustomDraw)
            original._state_file = state_file
            original._daily_usage = {f"900000001:{today}": 1}
            original._pending = {
                "AB12CD34": ("900000001", "画一只穿西装的猫", time.time())
            }

            self.assertTrue(original._save_state())

            restored = CustomDraw.__new__(CustomDraw)
            restored._state_file = state_file
            restored._daily_usage = {}
            restored._pending = {}
            restored._load_state()

            self.assertEqual(restored._daily_usage, original._daily_usage)
            self.assertEqual(restored._pending["AB12CD34"][0:2], original._pending["AB12CD34"][0:2])


if __name__ == "__main__":
    unittest.main()
