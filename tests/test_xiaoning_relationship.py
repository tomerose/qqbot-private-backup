from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ROOT / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS))

from xiaoning_core.memory import MemoryGateway  # noqa: E402
from xiaoning_core.models import (  # noqa: E402
    ProactiveCandidate,
    ProactiveMode,
    RelationshipProfile,
)
from xiaoning_core.persona_canon import (  # noqa: E402
    PERSONA_CANON_PROMPT,
    get_daily_persona_event,
    guard_persona_reply,
    persona_age,
)
from xiaoning_core.relationship import (  # noqa: E402
    evaluate_proactive_send,
    extract_open_loops,
    parse_proactive_preference,
    select_open_loops_for_mutation,
)


class _TestCipher:
    search_key = b"relationship-test-search-key".ljust(32, b"0")

    @staticmethod
    def _mask(context: str) -> int:
        return (sum(context.encode("utf-8")) % 251) + 1

    def encrypt(self, value: bytes, *, context: str) -> bytes:
        mask = self._mask(context)
        return bytes(item ^ mask for item in value)

    def decrypt(self, value: bytes, *, context: str) -> bytes:
        return self.encrypt(value, context=context)


UTC8 = timezone(timedelta(hours=8))


class RelationshipStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "memory.sqlite3"
        self.gateway = MemoryGateway(self.path, cipher=_TestCipher())

    def tearDown(self):
        self.temp.cleanup()

    def test_three_meaningful_private_turns_activate_without_backfill(self):
        base = datetime(2026, 8, 1, 12, tzinfo=UTC8).timestamp()
        profile = self.gateway.record_private_turn("qq:10001", "嗯", now=base)
        self.assertEqual(profile.meaningful_turns, 0)
        self.assertFalse(profile.activated)

        for offset, text in enumerate(("我下周三要面试", "最近在准备作品集", "到时候再聊面试结果"), 1):
            profile = self.gateway.record_private_turn(
                "qq:10001", text, now=base + offset * 60
            )

        self.assertTrue(profile.activated)
        self.assertEqual(profile.meaningful_turns, 3)
        self.assertEqual(profile.active_dates, ("2026-08-01",))
        self.assertTrue(self.gateway.get_consent("qq:10001").memory)
        self.assertTrue(self.gateway.get_consent("qq:10001").proactive)
        self.assertTrue(self.gateway.consume_activation_notice("qq:10001"))
        self.assertFalse(self.gateway.consume_activation_notice("qq:10001"))

    def test_open_loops_are_encrypted_scoped_and_deletable(self):
        now = datetime(2026, 8, 1, 12, tzinfo=UTC8).timestamp()
        loops = extract_open_loops("我下周三去字节面试，面完跟你说", now=now)
        self.assertEqual(len(loops), 1)
        stored = self.gateway.upsert_open_loop("qq:10001", loops[0], now=now)

        self.assertNotIn("字节面试".encode(), self.path.read_bytes())
        self.assertEqual(self.gateway.list_open_loops("qq:10002", now=now), [])
        self.assertEqual(
            self.gateway.list_open_loops("qq:10001", now=now)[0].loop_id,
            stored.loop_id,
        )

        self.assertEqual(self.gateway.delete_open_loops("qq:10002"), 0)
        self.assertEqual(self.gateway.delete_open_loops("qq:10001"), 1)
        self.assertEqual(self.gateway.list_open_loops("qq:10001", now=now), [])

        with closing(sqlite3.connect(self.path)) as connection:
            raw_scope = connection.execute(
                "SELECT scope_key FROM relationship_profiles LIMIT 1"
            ).fetchone()
        self.assertIsNone(raw_scope)

    def test_sensitive_topics_never_become_open_loops(self):
        now = datetime(2026, 8, 1, 12, tzinfo=UTC8).timestamp()
        blocked = (
            "记住我家住址是杭州市某某路 18 号，明天提醒我",
            "我的银行卡密码是 123456，下周再说",
            "医生说我可能有抑郁症，改天继续聊诊断",
            "这是我朋友的亲密隐私，过两天提醒我问她",
        )
        for text in blocked:
            self.assertEqual(extract_open_loops(text, now=now), [], text)

    def test_profile_payload_and_user_identifier_are_not_plaintext(self):
        now = datetime(2026, 8, 1, 12, tzinfo=UTC8).timestamp()
        scope = "qq:user-secret-10001"
        self.gateway.record_private_turn(scope, "我明天有考试", now=now)
        database = self.path.read_bytes()
        self.assertNotIn(scope.encode(), database)
        self.assertNotIn("我明天有考试".encode(), database)

    def test_due_candidate_is_claimed_once_and_send_is_counted(self):
        base = datetime(2026, 8, 1, 12, tzinfo=UTC8).timestamp()
        for offset, text in enumerate(("我下周面试", "我在准备作品集", "面完跟你说")):
            self.gateway.record_private_turn("qq:10001", text, now=base + offset)
        loop = extract_open_loops("我明天面试，面完跟你说", now=base)[0]
        self.gateway.upsert_open_loop("qq:10001", loop, now=base)
        candidate = self.gateway.enqueue_open_loop_candidate("qq:10001", loop, now=base)

        due = datetime.fromtimestamp(loop.not_before + 12 * 3600, tz=UTC8)
        claimed = self.gateway.claim_due_candidate("qq:10001", now=due.timestamp())
        self.assertEqual(claimed.candidate_id, candidate.candidate_id)
        self.assertIsNone(
            self.gateway.claim_due_candidate("qq:10001", now=due.timestamp())
        )

        self.gateway.mark_candidate_sent("qq:10001", claimed.candidate_id, now=due.timestamp())
        profile = self.gateway.get_relationship_profile("qq:10001")
        self.assertEqual(profile.unanswered_proactive, 1)
        self.assertTrue(profile.first_proactive_notice_sent)

    def test_pause_on_activation_turn_stays_paused_and_forget_cancels_candidate(self):
        base = datetime(2026, 8, 1, 12, tzinfo=UTC8).timestamp()
        self.gateway.record_private_turn("qq:10001", "我下周有考试", now=base)
        self.gateway.record_private_turn("qq:10001", "我在准备作品集", now=base + 1)
        self.gateway.set_proactive_mode("qq:10001", ProactiveMode.PAUSED, now=base + 2)
        profile = self.gateway.record_private_turn(
            "qq:10001", "别主动找我", now=base + 2
        )
        self.assertTrue(profile.activated)
        self.assertFalse(self.gateway.get_consent("qq:10001").proactive)

        self.gateway.set_proactive_mode("qq:10001", ProactiveMode.NORMAL, now=base + 3)
        loop = extract_open_loops("我明天去面试，面完跟你说", now=base)[0]
        self.gateway.upsert_open_loop("qq:10001", loop, now=base)
        self.gateway.enqueue_open_loop_candidate("qq:10001", loop, now=base)
        self.assertEqual(self.gateway.delete_open_loops("qq:10001"), 1)
        due = loop.not_before + 12 * 3600
        self.assertIsNone(self.gateway.claim_due_candidate("qq:10001", now=due))

    def test_forget_last_and_correction_only_remove_the_matching_loop(self):
        base = datetime(2026, 8, 1, 12, tzinfo=UTC8).timestamp()
        older = extract_open_loops("我明天有英语考试，考完跟你说", now=base)[0]
        newer = extract_open_loops("我后天参加产品面试，面完跟你说", now=base + 1)[0]
        loops = [newer, older]

        self.assertEqual(
            select_open_loops_for_mutation(loops, "忘了刚才的事"),
            [newer.loop_id],
        )
        self.assertEqual(
            select_open_loops_for_mutation(loops, "不是产品面试，是技术面试"),
            [newer.loop_id],
        )
        self.assertEqual(
            select_open_loops_for_mutation(loops, "不是这个，其实我只是随口说说"),
            [],
        )
        self.assertEqual(
            set(select_open_loops_for_mutation(loops, "把所有未完话题都忘掉")),
            {older.loop_id, newer.loop_id},
        )

    def test_deleting_one_loop_cancels_only_its_candidate(self):
        base = datetime(2026, 8, 1, 12, tzinfo=UTC8).timestamp()
        for offset, text in enumerate(("我下周面试", "我在准备作品集", "面完跟你说")):
            self.gateway.record_private_turn("qq:10001", text, now=base + offset)

        exam = extract_open_loops("我明天有英语考试，考完跟你说", now=base)[0]
        interview = extract_open_loops("我后天参加产品面试，面完跟你说", now=base + 1)[0]
        exam = self.gateway.upsert_open_loop("qq:10001", exam, now=base)
        interview = self.gateway.upsert_open_loop("qq:10001", interview, now=base + 1)
        self.gateway.enqueue_open_loop_candidate("qq:10001", exam, now=base)
        interview_candidate = self.gateway.enqueue_open_loop_candidate(
            "qq:10001", interview, now=base + 1
        )

        self.assertEqual(
            self.gateway.delete_open_loops("qq:10001", loop_id=exam.loop_id),
            1,
        )
        due = interview.not_before + 12 * 3600
        claimed = self.gateway.claim_due_candidate("qq:10001", now=due)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.candidate_id, interview_candidate.candidate_id)


class ProactivePolicyTests(unittest.TestCase):
    def _profile(self, **changes) -> RelationshipProfile:
        values = dict(
            activated=True,
            meaningful_turns=3,
            proactive_mode=ProactiveMode.NORMAL,
            last_user_at=datetime(2026, 8, 3, 10, tzinfo=UTC8).timestamp(),
            unanswered_proactive=0,
        )
        values.update(changes)
        return RelationshipProfile(**values)

    def _candidate(self, **changes) -> ProactiveCandidate:
        values = dict(
            candidate_id="candidate-1",
            open_loop_id="loop-1",
            why_now="用户今天结束面试，适合问结果",
            source_type="open_loop",
            relevance=0.95,
            timing=0.9,
            novelty=0.9,
            evidence_confidence=0.95,
            not_before=datetime(2026, 8, 3, 18, tzinfo=UTC8).timestamp(),
            expires_at=datetime(2026, 8, 5, 22, tzinfo=UTC8).timestamp(),
        )
        values.update(changes)
        return ProactiveCandidate(**values)

    def test_send_requires_time_score_gap_and_no_unanswered_message(self):
        now = datetime(2026, 8, 3, 19, tzinfo=UTC8)
        allowed = evaluate_proactive_send(self._profile(), self._candidate(), now, ())
        self.assertTrue(allowed.allowed)

        cases = (
            (self._profile(activated=False), self._candidate(), now, "not_activated"),
            (self._profile(proactive_mode=ProactiveMode.PAUSED), self._candidate(), now, "paused"),
            (self._profile(unanswered_proactive=1), self._candidate(), now, "awaiting_reply"),
            (self._profile(last_user_at=(now - timedelta(hours=5)).timestamp()), self._candidate(), now, "too_close_to_user"),
            (self._profile(), self._candidate(relevance=0.6), now, "score_below_threshold"),
            (self._profile(), self._candidate(), datetime(2026, 8, 3, 9, 29, tzinfo=UTC8), "quiet_hours"),
        )
        for profile, candidate, at, reason in cases:
            self.assertEqual(
                evaluate_proactive_send(profile, candidate, at, ()).reason, reason
            )

    def test_daily_weekly_and_reduced_frequency_limits(self):
        now = datetime(2026, 8, 9, 19, tzinfo=UTC8)
        candidate = self._candidate(
            not_before=(now - timedelta(hours=1)).timestamp(),
            expires_at=(now + timedelta(days=1)).timestamp(),
        )
        today = (now - timedelta(hours=2)).timestamp()
        self.assertEqual(
            evaluate_proactive_send(self._profile(), candidate, now, (today,)).reason,
            "daily_limit",
        )
        week = tuple((now - timedelta(days=day)).timestamp() for day in (1, 2, 3))
        self.assertEqual(
            evaluate_proactive_send(self._profile(), candidate, now, week).reason,
            "weekly_limit",
        )
        reduced = self._profile(proactive_mode=ProactiveMode.REDUCED)
        self.assertEqual(
            evaluate_proactive_send(reduced, candidate, now, (week[0],)).reason,
            "weekly_limit",
        )

    def test_natural_language_preferences_are_immediate(self):
        self.assertEqual(parse_proactive_preference("别主动找我"), ProactiveMode.PAUSED)
        self.assertEqual(parse_proactive_preference("少一点"), ProactiveMode.REDUCED)
        self.assertEqual(parse_proactive_preference("恢复正常"), ProactiveMode.NORMAL)
        self.assertIsNone(parse_proactive_preference("今天少吃了一点"))

    def test_thirty_day_virtual_clock_never_exceeds_daily_or_weekly_caps(self):
        start = datetime(2026, 8, 3, 19, tzinfo=UTC8)
        sent: list[float] = []
        for offset in range(30):
            now = start + timedelta(days=offset)
            candidate = self._candidate(
                candidate_id=f"candidate-{offset}",
                not_before=(now - timedelta(hours=1)).timestamp(),
                expires_at=(now + timedelta(hours=2)).timestamp(),
            )
            decision = evaluate_proactive_send(self._profile(), candidate, now, sent)
            if decision.allowed:
                sent.append(now.timestamp())
                duplicate = evaluate_proactive_send(
                    self._profile(), candidate, now + timedelta(minutes=1), sent
                )
                self.assertEqual(duplicate.reason, "daily_limit")

        local = [datetime.fromtimestamp(item, tz=UTC8) for item in sent]
        self.assertTrue(local)
        self.assertEqual(len(local), len({item.date() for item in local}))
        for anchor in local:
            monday = (anchor - timedelta(days=anchor.weekday())).date()
            in_week = [item for item in local if (item - timedelta(days=item.weekday())).date() == monday]
            self.assertLessEqual(len(in_week), 3)

        reduced_sent: list[float] = []
        reduced = self._profile(proactive_mode=ProactiveMode.REDUCED)
        for offset in range(30):
            now = start + timedelta(days=offset)
            candidate = self._candidate(
                candidate_id=f"reduced-{offset}",
                not_before=(now - timedelta(hours=1)).timestamp(),
                expires_at=(now + timedelta(hours=2)).timestamp(),
            )
            if evaluate_proactive_send(reduced, candidate, now, reduced_sent).allowed:
                reduced_sent.append(now.timestamp())
        reduced_local = [datetime.fromtimestamp(item, tz=UTC8) for item in reduced_sent]
        for anchor in reduced_local:
            monday = (anchor - timedelta(days=anchor.weekday())).date()
            in_week = [item for item in reduced_local if (item - timedelta(days=item.weekday())).date() == monday]
            self.assertLessEqual(len(in_week), 1)


class PersonaCanonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.gateway = MemoryGateway(
            Path(self.temp.name) / "memory.sqlite3", cipher=_TestCipher()
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_canon_is_complete_and_age_is_date_derived(self):
        for fact in ("2002年10月18日", "杭州", "数字媒体", "独立数字内容策划", "单身", "周深", "邓紫棋"):
            self.assertIn(fact, PERSONA_CANON_PROMPT)
        self.assertEqual(persona_age(date(2026, 10, 17)), 23)
        self.assertEqual(persona_age(date(2026, 10, 18)), 24)

    def test_one_daily_event_is_shared_and_persisted(self):
        day = date(2026, 8, 16)
        first = get_daily_persona_event(self.gateway, day)
        second = get_daily_persona_event(self.gateway, day)
        self.assertEqual(first, second)
        self.assertEqual(first.day, "2026-08-16")
        self.assertTrue(first.narrative)
        self.assertNotIn(first.narrative.encode(), self.gateway.path.read_bytes())

    def test_guard_rewrites_conflicting_biography_and_execution_claims(self):
        event = get_daily_persona_event(self.gateway, date(2026, 8, 16))
        guarded = guard_persona_reply(
            "我今年26岁，住在上海，在腾讯上班，还有男朋友。刚才我去你家取了快递。",
            day=date(2026, 8, 16),
            event=event,
        )
        self.assertNotIn("26岁", guarded)
        self.assertNotIn("住在上海", guarded)
        self.assertNotIn("腾讯上班", guarded)
        self.assertNotIn("男朋友", guarded)
        self.assertNotIn("去你家取了快递", guarded)
        self.assertIn("23岁", guarded)
        self.assertIn("杭州", guarded)

        private = guard_persona_reply(
            "我出生于2000年1月1日，毕业于浙江大学，我妈妈叫小芳，我住在文一路18号。",
            day=date(2026, 8, 16),
            event=event,
        )
        self.assertIn("2002年10月18日", private)
        self.assertNotIn("浙江大学", private)
        self.assertNotIn("小芳", private)
        self.assertNotIn("文一路", private)


if __name__ == "__main__":
    unittest.main()
