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
    def __init__(self, text: str, sender: str):
        self.text = text
        self.sender = sender
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

    def tearDown(self):
        self.temp.cleanup()

    def test_apply_returns_safe_email_template_without_agent_access(self):
        event = FakeEvent("/pro apply", APPLICANT)

        replies = asyncio.run(collect(self.plugin.on_message(event)))

        self.assertTrue(event.stopped)
        self.assertEqual(len(replies), 1)
        self.assertIn("portelamicheli636@gmail.com", replies[0])
        self.assertIn("APP-", replies[0])
        self.assertIn("不包含本机 Agent", replies[0])

    def test_only_reviewer_can_approve_and_code_is_private(self):
        apply_reply = asyncio.run(
            collect(self.plugin.on_message(FakeEvent("/pro apply", APPLICANT)))
        )[0]
        application_id = re.search(r"APP-[A-Z0-9]+", apply_reply).group(0)
        asyncio.run(
            collect(self.plugin.on_message(FakeEvent(f"/pro sent {application_id}", APPLICANT)))
        )

        denied = asyncio.run(
            collect(self.plugin.on_message(FakeEvent(f"/pro approve {application_id}", "3000000000")))
        )
        self.assertIn("无权", denied[0])
        self.assertEqual(self.context.sent, [])

        approval_requested = asyncio.run(
            collect(self.plugin.on_message(FakeEvent(f"/pro approve {application_id} 90", REVIEWER)))
        )
        self.assertIn("/pro confirm", approval_requested[0])
        self.assertEqual(self.context.sent, [])

        approved = asyncio.run(
            collect(self.plugin.on_message(FakeEvent(f"/pro confirm {application_id}", REVIEWER)))
        )
        self.assertNotRegex(approved[0], r"[A-Za-z0-9_-]{12,}")
        self.assertEqual(self.context.sent[0][0], f"llbot-test:FriendMessage:{APPLICANT}")
        code = re.search(r"/pro verify ([A-Za-z0-9_-]+)", chain_text(self.context.sent[0][1])).group(1)

        verified = asyncio.run(
            collect(self.plugin.on_message(FakeEvent(f"/pro verify {code}", APPLICANT)))
        )
        self.assertIn("已开通", verified[0])

    def test_status_is_limited_to_requesting_qq(self):
        asyncio.run(collect(self.plugin.on_message(FakeEvent("/pro apply", APPLICANT))))

        other = asyncio.run(
            collect(self.plugin.on_message(FakeEvent("/pro status", "3000000000")))
        )
        own = asyncio.run(
            collect(self.plugin.on_message(FakeEvent("/pro status", APPLICANT)))
        )

        self.assertIn("暂无", other[0])
        self.assertIn("待发送邮件", own[0])
        self.assertNotIn(APPLICANT, own[0])

    def test_failed_private_code_delivery_returns_application_to_review(self):
        apply_reply = asyncio.run(
            collect(self.plugin.on_message(FakeEvent("/pro apply", APPLICANT)))
        )[0]
        application_id = re.search(r"APP-[A-Z0-9]+", apply_reply).group(0)
        asyncio.run(
            collect(self.plugin.on_message(FakeEvent(f"/pro sent {application_id}", APPLICANT)))
        )
        self.context.deliver = False

        asyncio.run(
            collect(self.plugin.on_message(FakeEvent(f"/pro approve {application_id}", REVIEWER)))
        )
        replies = asyncio.run(
            collect(self.plugin.on_message(FakeEvent(f"/pro confirm {application_id}", REVIEWER)))
        )

        self.assertIn("未送达", replies[0])
        self.assertEqual(self.plugin.store.status_for(APPLICANT, now=1_001).state, "awaiting_review")

    def test_only_reviewer_can_read_minimal_audit(self):
        apply_reply = asyncio.run(
            collect(self.plugin.on_message(FakeEvent("/pro apply", APPLICANT)))
        )[0]
        application_id = re.search(r"APP-[A-Z0-9]+", apply_reply).group(0)
        asyncio.run(
            collect(self.plugin.on_message(FakeEvent(f"/pro sent {application_id}", APPLICANT)))
        )

        denied = asyncio.run(
            collect(self.plugin.on_message(FakeEvent(f"/pro audit {application_id}", "3000000000")))
        )
        allowed = asyncio.run(
            collect(self.plugin.on_message(FakeEvent(f"/pro audit {application_id}", REVIEWER)))
        )

        self.assertIn("无权", denied[0])
        self.assertIn("created", allowed[0])
        self.assertNotIn(APPLICANT, allowed[0])


if __name__ == "__main__":
    unittest.main()
