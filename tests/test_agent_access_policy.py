import sys
import tempfile
import unittest
from pathlib import Path


PLUGINS_DIR = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS_DIR / "claude_code_agent"))
sys.path.insert(0, str(PLUGINS_DIR))

from access_policy import AccessPolicy, AccessTier, Capability  # noqa: E402
from trusted_policy import TrustedPolicy  # noqa: E402
from pro_application.pro_store import ProStore  # noqa: E402
from claude_code_agent.main import ClaudeCodeAgent  # noqa: E402


class AgentAccessPolicyTests(unittest.TestCase):
    def test_ordinary_users_only_receive_chat_and_voice_capabilities(self):
        policy = AccessPolicy(["1211000567"])

        self.assertEqual(policy.resolve_tier("2000000000"), AccessTier.ORDINARY)
        self.assertTrue(policy.authorize("2000000000", Capability.CHAT))
        self.assertTrue(policy.authorize("2000000000", Capability.VOICE))
        self.assertFalse(policy.authorize("2000000000", Capability.LOCAL_AGENT))
        self.assertFalse(policy.authorize("2000000000", Capability.LOCAL_FILE))
        self.assertFalse(policy.authorize("2000000000", Capability.TASK_CONTROL))

    def test_pro_allowlist_is_frozen_and_grants_agent_capabilities(self):
        configured = ["1211000567"]
        policy = AccessPolicy(configured)
        configured.append("2000000000")

        self.assertEqual(policy.resolve_tier("1211000567"), AccessTier.PRO)
        for capability in Capability:
            self.assertTrue(policy.authorize("1211000567", capability))
        self.assertEqual(policy.resolve_tier("2000000000"), AccessTier.ORDINARY)

    def test_invalid_ids_and_chat_phrases_never_become_pro(self):
        policy = AccessPolicy(["1211000567", "领取Pro", "", "not-a-qq"])

        self.assertEqual(policy.pro_user_ids, frozenset({"1211000567"}))
        self.assertEqual(policy.resolve_tier("领取Pro"), AccessTier.ORDINARY)
        self.assertEqual(policy.resolve_tier(""), AccessTier.ORDINARY)

    def test_approved_public_pro_cannot_become_trusted_host_operator(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ProStore(Path(tmp) / "pro_members.db", reviewer_id="1211000567")
            application = store.create_application("2000000000", now=1_000)
            store.mark_sent(application.application_id, "2000000000", now=1_001)
            store.request_approval(application.application_id, "1211000567", 90, now=1_002)
            code = store.confirm_approval(application.application_id, "1211000567", now=1_003)
            store.verify("2000000000", code, now=1_004)
            self.assertTrue(store.is_active_pro("2000000000", now=1_005))

        policy = AccessPolicy(["1211000567", "2000000000"])
        trusted = TrustedPolicy(["1211000567"])

        self.assertEqual(policy.resolve_tier("2000000000"), AccessTier.PRO)
        self.assertTrue(policy.authorize("2000000000", Capability.LOCAL_AGENT))
        self.assertFalse(trusted.is_trusted("2000000000"))
        self.assertTrue(trusted.is_trusted("1211000567"))

    def test_signed_public_pro_is_admitted_to_the_same_hard_agent_policy(self):
        agent = ClaudeCodeAgent.__new__(ClaudeCodeAgent)
        agent._access_policy = AccessPolicy(["1211000567"])
        agent._trusted_policy = TrustedPolicy(["1211000567"])
        agent._pro_db_path = Path("missing-pro-members.db")

        class Context:
            def get_sender_id(self):
                return "2000000000"

        agent._is_public_pro = lambda _sender: True
        self.assertTrue(agent._is_owner(Context()))
        self.assertFalse(agent._can_manage_runtime(Context()))

        owner_context = type("OwnerContext", (), {"get_sender_id": lambda self: "1211000567"})()
        self.assertTrue(agent._can_manage_runtime(owner_context))


if __name__ == "__main__":
    unittest.main()
