import asyncio
import inspect
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

PLUGIN_PARENT = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot_plugin_qqadmin.core.ai_moderation_handler import AIModerationHandler  # noqa: E402

VALID_DECISION = (
    '{"decision":"recall_and_mute","category":"fraud",'
    '"confidence":0.98,"reason_code":"fraud_lure"}'
)


class FakeProvider:
    def __init__(self, outcome=VALID_DECISION, delay=0):
        self.outcome = outcome
        self.delay = delay
        self.prompts = []

    async def text_chat(self, system_prompt, prompt):
        self.prompts.append((system_prompt, prompt))
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return SimpleNamespace(completion_text=self.outcome)


class FakeContext:
    def __init__(self, provider):
        self.provider = provider

    def get_provider_by_id(self, provider_id):
        return self.provider if provider_id == "deepseek-chat" else None


class FakeStore:
    def __init__(self, prior_offenses=0, enabled=True):
        self.prior_offenses = prior_offenses
        self.enabled = enabled
        self.records = []

    def is_enabled(self, group_id):
        return self.enabled

    def offense_count(self, group_id, user_id, now=None):
        return self.prior_offenses

    def record_action(self, group_id, user_id, action, reason_code, confidence, success, now=None):
        self.records.append((action, reason_code, success))


class FakeBot:
    def __init__(self, bot_role="admin", sender_role="member"):
        self.bot_role = bot_role
        self.sender_role = sender_role
        self.calls = []

    async def get_group_member_info(self, group_id, user_id, no_cache=True):
        role = self.bot_role if user_id == 3806573022 else self.sender_role
        return {"role": role}

    async def delete_msg(self, message_id):
        self.calls.append(("delete_msg", message_id))

    async def set_group_ban(self, group_id, user_id, duration):
        self.calls.append(("set_group_ban", group_id, user_id, duration))


class FakeEvent:
    def __init__(self, bot, sender_id="123456789", text=None):
        self.bot = bot
        self._sender_id = sender_id
        self.message_str = text or (
            r"加群领取返现 token=very-secret C:\Users\liu\x.txt "
            "https://example.test/pay?q=private"
        )
        self.message_obj = SimpleNamespace(message_id=10001)
        self.sent = []

    def get_group_id(self):
        return "945598390"

    def get_sender_id(self):
        return self._sender_id

    def get_self_id(self):
        return "3806573022"

    def plain_result(self, text):
        return text

    async def send(self, result):
        self.sent.append(result)


def build_handler(bot, provider=None, store=None, timeout=8):
    provider = provider or FakeProvider()
    store = store or FakeStore()
    return AIModerationHandler(
        context=FakeContext(provider),
        store=store,
        provider_id="deepseek-chat",
        timeout_seconds=timeout,
        context_messages=8,
        owner_id="1211000567",
    )


class AIModerationHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_admin_bot_and_exempt_senders_never_trigger_actions(self):
        for bot_role, sender_id, sender_role in (
            ("member", "123456789", "member"),
            ("admin", "1211000567", "member"),
            ("admin", "123456789", "admin"),
            ("admin", "123456789", "owner"),
        ):
            with self.subTest(bot_role=bot_role, sender_id=sender_id, sender_role=sender_role):
                bot = FakeBot(bot_role, sender_role)
                provider = FakeProvider()
                await build_handler(bot, provider=provider).handle(FakeEvent(bot, sender_id))
                self.assertEqual(bot.calls, [])
                self.assertEqual(provider.prompts, [])

    async def test_valid_decision_recalls_then_uses_local_escalation_duration(self):
        bot = FakeBot()
        store = FakeStore(prior_offenses=1)
        await build_handler(bot, store=store).handle(FakeEvent(bot))
        self.assertEqual(
            bot.calls,
            [("delete_msg", 10001), ("set_group_ban", 945598390, 123456789, 60)],
        )
        self.assertEqual(store.records, [("mute", "fraud_lure", True)])

    async def test_first_offense_only_recalls_and_warns(self):
        bot = FakeBot()
        event = FakeEvent(bot)
        await build_handler(bot).handle(event)
        self.assertEqual(bot.calls, [("delete_msg", 10001)])
        self.assertTrue(event.sent)

    async def test_timeout_invalid_json_and_provider_error_fail_open(self):
        providers = (
            FakeProvider(delay=0.05),
            FakeProvider(outcome="not-json"),
            FakeProvider(outcome=RuntimeError("provider down")),
        )
        for provider in providers:
            bot = FakeBot()
            await build_handler(bot, provider=provider, timeout=0.01).handle(FakeEvent(bot))
            self.assertEqual(bot.calls, [])

    async def test_model_prompt_contains_no_owner_or_chat_identifiers(self):
        bot = FakeBot()
        provider = FakeProvider(outcome='{"decision":"none"}')
        await build_handler(bot, provider=provider).handle(FakeEvent(bot))
        prompt = "\n".join(provider.prompts[0])
        for forbidden in (
            "945598390",
            "123456789",
            "1211000567",
            "3806573022",
            "liu",
            "very-secret",
            "q=private",
        ):
            self.assertNotIn(forbidden, prompt)

    def test_handler_exposes_no_kick_or_block_action(self):
        source = inspect.getsource(AIModerationHandler)
        self.assertNotIn("set_group_kick", source)
        self.assertNotIn("set_group_block", source)


if __name__ == "__main__":
    unittest.main()
