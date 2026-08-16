import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ROOT / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS))

from xiaoning_core.models import TurnEnvelope  # noqa: E402
from xiaoning_core.router import TurnRouter  # noqa: E402


class GoldenRouteReplayTests(unittest.TestCase):
    def test_at_least_one_hundred_sixty_behavior_examples(self):
        dataset = json.loads(
            (ROOT / "tests/behavior/golden_turns.json").read_text(encoding="utf-8")
        )
        router = TurnRouter()
        total = correct = 0
        ordinary_total = ordinary_misroutes = 0
        for group in dataset["groups"]:
            inputs = group.get("input", {})
            expected = group["expected"]
            for index, text in enumerate(group["utterances"]):
                total += 1
                turn = TurnEnvelope(
                    message_id=f"golden-{total}",
                    conversation_scope=(
                        "qq:group:20001" if inputs.get("group") else "qq:private:10001"
                    ),
                    channel="qq",
                    sender_id="10001",
                    text=text,
                    is_group=bool(inputs.get("group", False)),
                    is_addressed=bool(inputs.get("addressed", False)),
                )
                decision = router.decide(turn)
                actual = {
                    "kind": decision.kind.value,
                    "owner": decision.owner,
                    "respond": decision.should_respond,
                }
                if "capability" in expected:
                    actual["capability"] = decision.capability_id
                matched = all(actual.get(key) == value for key, value in expected.items())
                with self.subTest(group=group["name"], index=index, text=text):
                    self.assertTrue(matched, f"expected={expected}, actual={actual}")
                correct += int(matched)
                if group["name"].startswith("ordinary_"):
                    ordinary_total += 1
                    ordinary_misroutes += int(decision.kind.value != "chat")

        self.assertGreaterEqual(total, 160)
        self.assertGreaterEqual(correct / total, 0.95)
        self.assertLessEqual(ordinary_misroutes / ordinary_total, 0.02)


if __name__ == "__main__":
    unittest.main()
