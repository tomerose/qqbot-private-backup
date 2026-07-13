import asyncio
import re
import sys
import tempfile
import unittest
from pathlib import Path


PLUGINS_DIR = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS_DIR))

from pro_application.main import ProApplication  # noqa: E402
from pro_application.pro_store import ProStore  # noqa: E402


REVIEWER = "1211000567"
APPLICANT = "2000000000"


class FakeEvent:
    def __init__(self, text: str, sender: str, *, private: bool = True):
        self.text = text
        self.sender = sender
        self.private = private
        self.unified_msg_origin = f"llbot-test:FriendMessage:{sender}"
        self.stopped = False

    def get_message_str(self):
        return self.text

    def get_sender_id(self):
        return self.sender

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text

    def is_private_chat(self):
        return self.private


class FakeContext:
    def __init__(self):
        self.sent = []
        self.deliver = True

    async def send_message(self, session, chain):
        self.sent.append((session, chain))
        return self.deliver


async def collect(generator):
    return [item async for item in generator]


def chain_text(chain):
    components = getattr(chain, "chain", chain)
    return "\n".join(str(getattr(item, "text", "")) for item in components)


class ProApplicationPluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.context = FakeContext()
        self.plugin = ProApplication.__new__(ProApplication)
        self.plugin.context = self.context
        self.plugin.store = ProStore(Path(self.temp.name) / "pro_members.db", reviewer_id=REVIEWER)
        self.plugin._clock = lambda: 1_000.0
        self.plugin._auth_sessions: dict[str, float] = {REVIEWER: float("inf")}
        self.plugin._auth_failures: dict[str, tuple[int, float]] = {}
        self.plugin._invite_lock = asyncio.Lock()
        self.plugin._invite_file = Path(self.temp.name) / "invites.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_invite_flow_grants_bound_user_without_exposing_other_status(self):
        created = asyncio.run(
            collect(self.plugin.on_message(FakeEvent(f"/invite {APPLICANT} go 30", REVIEWER)))
        )
        code = re.search(r"XIAONING-[A-F0-9]+", created[0]).group(0)

        other = asyncio.run(
            collect(self.plugin.on_message(FakeEvent(f"/redeem {code}", "3000000000")))
        )
        redeemed = asyncio.run(
            collect(self.plugin.on_message(FakeEvent(f"/redeem {code}", APPLICANT)))
        )
        status = asyncio.run(
            collect(self.plugin.on_message(FakeEvent("/pro status", APPLICANT)))
        )

        self.assertIn("与你不同", other[0])
        self.assertIn("GO", redeemed[0])
        self.assertIn("当前资格：GO", status[0])
        self.assertNotIn(APPLICANT, status[0])

    def test_only_authenticated_reviewer_can_generate_invites(self):
        denied = asyncio.run(
            collect(self.plugin.on_message(FakeEvent(f"/invite {APPLICANT} pro 30", "3000000000")))
        )
        self.plugin._auth_sessions.clear()
        unauthenticated = asyncio.run(
            collect(self.plugin.on_message(FakeEvent(f"/invite {APPLICANT} pro 30", REVIEWER)))
        )

        self.assertIn("拥有者", denied[0])
        self.assertIn("/pro auth", unauthenticated[0])
        self.assertFalse(self.plugin._invite_file.exists())

    def test_invite_cannot_be_replayed_after_used_flag_tampering(self):
        created = asyncio.run(
            collect(self.plugin.on_message(FakeEvent(f"/invite {APPLICANT} pro 30", REVIEWER)))
        )
        code = re.search(r"XIAONING-[A-F0-9]+", created[0]).group(0)
        asyncio.run(collect(self.plugin.on_message(FakeEvent(f"/redeem {code}", APPLICANT))))

        import json
        data = json.loads(self.plugin._invite_file.read_text(encoding="utf-8"))
        data["codes"][code]["used"] = False
        self.plugin._invite_file.write_text(json.dumps(data), encoding="utf-8")

        replay = asyncio.run(
            collect(self.plugin.on_message(FakeEvent(f"/redeem {code}", APPLICANT)))
        )
        self.assertIn("无效", replay[0])

    def test_go_duration_is_clamped_and_management_stays_private(self):
        group_reply = asyncio.run(
            collect(
                self.plugin.on_message(
                    FakeEvent(f"/invite {APPLICANT} go 365", REVIEWER, private=False)
                )
            )
        )
        private_reply = asyncio.run(
            collect(self.plugin.on_message(FakeEvent(f"/invite {APPLICANT} go 365", REVIEWER)))
        )

        self.assertIn("私聊", group_reply[0])
        self.assertIn("GO 90 天", private_reply[0])


if __name__ == "__main__":
    unittest.main()
