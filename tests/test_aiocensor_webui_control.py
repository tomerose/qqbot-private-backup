import sys
from pathlib import Path
import unittest

_PROJ_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = _PROJ_ROOT / "astrbot" / "data" / "plugins" / "astrbot_plugin_aiocensor"
sys.path.insert(0, str(PLUGIN_DIR))

from webui_control import webui_enabled  # noqa: E402


class WebUIControlTests(unittest.TestCase):
    def test_webui_is_disabled_by_default_and_requires_explicit_true(self):
        self.assertFalse(webui_enabled({}))
        self.assertFalse(webui_enabled({"webui": {"enable": False}}))
        self.assertTrue(webui_enabled({"webui": {"enable": True}}))


if __name__ == "__main__":
    unittest.main()
