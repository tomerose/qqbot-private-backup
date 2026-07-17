import sys
import tempfile
import unittest
from pathlib import Path


PLUGINS = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS))

from friend_core.relationship_state import (  # noqa: E402
    NORMAL_MODE,
    QUIET_MODE,
    get_snapshot,
    load_state,
    parse_friend_mode,
    record_interaction,
    save_state,
    set_friend_mode,
)


class RelationshipStateTests(unittest.TestCase):
    def test_records_interaction_and_return_gap(self):
        state = {}
        record_interaction(state, "1211000567", now=100)
        entry = record_interaction(state, "1211000567", now=100 + 21 * 3600)

        self.assertEqual(entry["message_count"], 2)
        self.assertEqual(entry["last_return_gap_hours"], 21.0)
        self.assertEqual(get_snapshot(state, "1211000567", now=100)["friend_mode"], NORMAL_MODE)

    def test_mode_parse_and_set_are_plain_state_not_permission(self):
        state = {}
        self.assertEqual(parse_friend_mode("小柠，安静一点"), QUIET_MODE)
        self.assertEqual(parse_friend_mode("恢复正常"), NORMAL_MODE)

        set_friend_mode(state, "1211000567", QUIET_MODE)

        self.assertEqual(get_snapshot(state, "1211000567")["friend_mode"], QUIET_MODE)
        self.assertNotIn("tier", state["1211000567"])

    def test_load_state_lazy_migrates_legacy_without_overwriting_primary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = root / "relationship_state.json"
            legacy = root / "flywheel_state.json"
            save_state(primary, {"11111": {"message_count": 9, "friend_mode": QUIET_MODE}})
            save_state(legacy, {"11111": {"message_count": 1}, "22222": {"message_count": 3}})

            state = load_state(primary, [legacy])

            self.assertEqual(state["11111"]["message_count"], 9)
            self.assertEqual(state["22222"]["message_count"], 3)


if __name__ == "__main__":
    unittest.main()
