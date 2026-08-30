import asyncio
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path


PLUGINS_DIR = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS_DIR))
os.environ.setdefault("PUBLIC_REVIEWER_ID", "900000001")

from pro_application.main import ProApplication  # noqa: E402
from pro_application.pro_store import ProStore  # noqa: E402


REVIEWER = "900000001"
APPLICANT = "2000000000"


class FakeBot:
    def __init__(self, friends=()):
        self.friends = {str(item) for item in friends}

    async def call_action(self, action: str):
        if action != "get_friend_list":
            raise AssertionError(action)
        return [{"user_id": item} for item in self.friends]


class FakeEvent:
    def __init__(self, text: str, sender: str, *, private: bool = True, friends=()):
        self.text = text
        self.sender = sender
        self.private = private
        self.bot = FakeBot(friends)
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
            collect(self.plugin.on_message(FakeEvent(f"/invite {APPLICANT} x 30", REVIEWER)))
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
        self.assertIn("X", redeemed[0])
        self.assertIn("当前资格：X", status[0])
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

    def test_x_duration_is_clamped_and_management_stays_private(self):
        group_reply = asyncio.run(
            collect(
                self.plugin.on_message(
                    FakeEvent(f"/invite {APPLICANT} x 365", REVIEWER, private=False)
                )
            )
        )
        private_reply = asyncio.run(
            collect(self.plugin.on_message(FakeEvent(f"/invite {APPLICANT} x 365", REVIEWER)))
        )

        self.assertIn("私聊", group_reply[0])
        self.assertIn("X 90 天", private_reply[0])

    def test_friend_check_grants_persistent_x_once(self):
        event = FakeEvent("你好", APPLICANT, friends={APPLICANT})
        asyncio.run(self.plugin._quick_friend_grant(event, APPLICANT))
        membership = self.plugin.store.status_for(APPLICANT, now=1_000)

        self.assertEqual(membership.tier, "x")
        self.assertGreater(membership.pro_expires_at, 1_000 + 365 * 86400)

        asyncio.run(self.plugin._quick_friend_grant(event, APPLICANT))
        events = self.plugin.store.audit_for(
            f"FRIEND-X-{APPLICANT}", REVIEWER, now=1_000
        )
        self.assertEqual(sum(item.event_type == "claimed_friend_x" for item in events), 1)

    def test_non_friend_is_not_granted_x(self):
        asyncio.run(self.plugin._quick_friend_grant(FakeEvent("你好", APPLICANT), APPLICANT))
        self.assertIsNone(self.plugin.store.status_for(APPLICANT, now=1_000))

    def test_friend_check_does_not_replace_existing_pro(self):
        self.plugin.store.grant(
            APPLICANT,
            REVIEWER,
            30,
            now=1_000,
            tier="pro",
        )
        asyncio.run(
            self.plugin._quick_friend_grant(
                FakeEvent("你好", APPLICANT, friends={APPLICANT}), APPLICANT
            )
        )
        self.assertEqual(
            self.plugin.store.status_for(APPLICANT, now=1_000).tier,
            "pro",
        )


if __name__ == "__main__":
    unittest.main()
