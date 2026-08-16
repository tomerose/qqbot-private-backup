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
    def __init__(self, bot_role="admin", sender_role="member", sequence=None):
        self.bot_role = bot_role
        self.sender_role = sender_role
        self.calls = []
        self.member_info_calls = []
        self.sequence = sequence

    async def get_group_member_info(self, group_id, user_id, no_cache=True):
        self.member_info_calls.append((group_id, user_id))
        role = self.bot_role if user_id == 3806573022 else self.sender_role
        return {"role": role}

    async def delete_msg(self, message_id):
        self.calls.append(("delete_msg", message_id))

    async def set_group_ban(self, group_id, user_id, duration):
        self.calls.append(("set_group_ban", group_id, user_id, duration))
        if self.sequence is not None:
            self.sequence.append(("mute", duration))


class FakeEvent:
    def __init__(self, bot, sender_id="123456789", text=None, group_id="945598390", sequence=None):
        self.bot = bot
        self._sender_id = sender_id
        self._group_id = group_id
        self.message_str = text or (
            r"加群领取返现 token=very-secret C:\Users\liu\x.txt "
            "https://example.test/pay?q=private"
        )
        self.message_obj = SimpleNamespace(message_id=10001)
        self.sent = []
        self.stopped = False
        self.sequence = sequence

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    def get_self_id(self):
        return "3806573022"

    def plain_result(self, text):
        return text

    async def send(self, result):
        self.sent.append(result)
        if self.sequence is not None:
            self.sequence.append(("send", result))

    def stop_event(self):
        self.stopped = True


def build_handler(bot, provider=None, store=None, timeout=8, **kwargs):
    provider = provider or FakeProvider()
    store = store or FakeStore()
    return AIModerationHandler(
        context=FakeContext(provider),
        store=store,
        provider_id="deepseek-chat",
        timeout_seconds=timeout,
        context_messages=8,
        owner_id="1211000567",
        **kwargs,
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

    async def test_normal_chat_skips_member_lookup_and_model(self):
        bot = FakeBot()
        provider = FakeProvider()
        await build_handler(bot, provider=provider).handle(FakeEvent(bot, text="今晚吃什么"))
        self.assertEqual(bot.member_info_calls, [])
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

    async def test_identity_attack_rebuts_before_bounded_mute_without_model(self):
        sequence = []
        bot = FakeBot(sequence=sequence)
        provider = FakeProvider(outcome=RuntimeError("model must not be called"))
        event = FakeEvent(bot, text="你就是个人机", sequence=sequence)
        handler = build_handler(
            bot,
            provider=provider,
            store=FakeStore(enabled=False),
            identity_guard_enabled=True,
            identity_guard_group_ids={"945598390"},
            identity_guard_mute_seconds=60,
        )

        await handler.handle(event)

        self.assertEqual(sequence[0][0], "send")
        self.assertIn("严正警告", sequence[0][1])
        self.assertIn("出现即触发", sequence[0][1])
        self.assertEqual(sequence[1], ("mute", 60))
        self.assertEqual(provider.prompts, [])
        self.assertTrue(event.stopped)

    async def test_identity_guard_warns_without_claiming_mute_when_bot_is_member(self):
        sequence = []
        bot = FakeBot(bot_role="member", sequence=sequence)
        provider = FakeProvider(outcome=RuntimeError("model must not be called"))
        store = FakeStore(enabled=False)
        event = FakeEvent(bot, text="什么AI？", group_id="815620109", sequence=sequence)
        handler = build_handler(
            bot,
            provider=provider,
            store=store,
            identity_guard_enabled=True,
            identity_guard_group_ids={"815620109"},
            identity_guard_mute_seconds=60,
        )

        await handler.handle(event)

        self.assertEqual(sequence[0][0], "send")
        self.assertIn("严正警告", sequence[0][1])
        self.assertFalse(any(item[0] == "mute" for item in sequence))
        self.assertEqual(store.records, [("warn", "targeted_harassment", True)])
        self.assertEqual(provider.prompts, [])
        self.assertTrue(event.stopped)

    async def test_identity_guard_skips_other_groups_admins_and_normal_discussion(self):
        other_group_bot = FakeBot()
        admin_sender_bot = FakeBot(sender_role="admin")
        unlisted_term_bot = FakeBot()
        cases = (
            (
                other_group_bot,
                FakeEvent(other_group_bot, text="你就是个人机", group_id="111"),
            ),
            (admin_sender_bot, FakeEvent(admin_sender_bot, text="你就是个人机")),
            (
                unlisted_term_bot,
                FakeEvent(unlisted_term_bot, text="今天晚上吃什么"),
            ),
        )
        for bot, event in cases:
            with self.subTest(text=event.message_str, group=event.get_group_id()):
                await build_handler(
                    bot,
                    store=FakeStore(enabled=False),
                    identity_guard_enabled=True,
                    identity_guard_group_ids={"945598390"},
                    identity_guard_mute_seconds=60,
                ).handle(event)
                self.assertEqual(bot.calls, [])
                self.assertEqual(event.sent, [])

    async def test_insult_terms_only_warn_without_recall_mute_or_model(self):
        for text in ("b啊", "你就是个傻 B", "少在这儿说sb", "真是狗东西"):
            with self.subTest(text=text):
                sequence = []
                bot = FakeBot(sequence=sequence)
                provider = FakeProvider(outcome=RuntimeError("model must not be called"))
                store = FakeStore(enabled=True)
                event = FakeEvent(bot, text=text, sequence=sequence)
                handler = build_handler(
                    bot,
                    provider=provider,
                    store=store,
                    insult_warning_enabled=True,
                    insult_warning_group_ids={"945598390"},
                    insult_warning_terms={"b啊", "傻b", "sb", "狗东西"},
                    insult_warning_text="别用辱骂代替论证。",
                )

                await handler.handle(event)

                self.assertEqual(sequence, [("send", "别用辱骂代替论证。")])
                self.assertEqual(bot.calls, [])
                self.assertEqual(provider.prompts, [])
                self.assertEqual(store.records, [("warn", "insult_language", True)])
                self.assertTrue(event.stopped)

    async def test_insult_warning_is_limited_to_designated_groups_and_members(self):
        cases = (
            (FakeBot(), "111"),
            (FakeBot(sender_role="admin"), "945598390"),
        )
        for bot, group_id in cases:
            with self.subTest(group_id=group_id, sender_role=bot.sender_role):
                event = FakeEvent(bot, text="你个傻逼", group_id=group_id)
                await build_handler(
                    bot,
                    store=FakeStore(enabled=False),
                    insult_warning_enabled=True,
                    insult_warning_group_ids={"945598390"},
                    insult_warning_terms={"傻逼"},
                ).handle(event)

                self.assertEqual(event.sent, [])
                self.assertEqual(bot.calls, [])

    async def test_literal_human_machine_discussion_triggers_the_same_warning(self):
        sequence = []
        bot = FakeBot(sequence=sequence)
        event = FakeEvent(bot, text="今天讨论人机协作", sequence=sequence)
        await build_handler(
            bot,
            store=FakeStore(enabled=False),
            identity_guard_enabled=True,
            identity_guard_group_ids={"945598390"},
            identity_guard_mute_seconds=60,
        ).handle(event)

        self.assertEqual(sequence[0][0], "send")
        self.assertEqual(sequence[1], ("mute", 60))

    async def test_configured_group_uses_ai_moderation_when_global_default_is_off(self):
        bot = FakeBot()
        store = FakeStore(prior_offenses=1, enabled=False)
        await build_handler(
            bot,
            store=store,
            ai_moderation_group_ids={"945598390"},
        ).handle(FakeEvent(bot))

        self.assertEqual(
            bot.calls,
            [("delete_msg", 10001), ("set_group_ban", 945598390, 123456789, 60)],
        )
        self.assertEqual(store.records, [("mute", "fraud_lure", True)])

    async def test_configured_ai_moderation_does_not_expand_to_other_groups(self):
        bot = FakeBot()
        provider = FakeProvider()
        await build_handler(
            bot,
            provider=provider,
            store=FakeStore(enabled=False),
            ai_moderation_group_ids={"945598390"},
        ).handle(FakeEvent(bot, group_id="111"))

        self.assertEqual(bot.calls, [])
        self.assertEqual(provider.prompts, [])


if __name__ == "__main__":
    unittest.main()
