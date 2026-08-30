import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "astrbot" / "data" / "plugins" / "voice_model_router" / "voice_router_core.py"
SPEC = importlib.util.spec_from_file_location("voice_router_core", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class Record:
    pass


class Plain:
    pass


class VoiceRouterCoreTest(unittest.TestCase):
    def test_detects_record_component(self):
        self.assertTrue(MODULE.contains_voice_component([Plain(), Record()]))

    def test_ignores_text_only_messages(self):
        self.assertFalse(MODULE.contains_voice_component([Plain()]))
        self.assertFalse(MODULE.contains_voice_component([]))

    def test_accepts_astrbot_enum_style_type(self):
        component = type("Component", (), {"type": "ComponentType.Record"})()
        self.assertTrue(MODULE.contains_voice_component([component]))


if __name__ == "__main__":
    unittest.main()
