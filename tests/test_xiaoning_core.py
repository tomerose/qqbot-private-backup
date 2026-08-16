import asyncio
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ROOT / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS))

from xiaoning_core.capabilities import CapabilityRegistry  # noqa: E402
from xiaoning_core.memory import MemoryGateway  # noqa: E402
from xiaoning_core.main import XiaoningCore  # noqa: E402
from xiaoning_core.models import RiskLevel, RouteKind, TaskRecord, TaskState, TurnEnvelope  # noqa: E402
from xiaoning_core.ownership import route_allows  # noqa: E402
from xiaoning_core.router import TurnRouter  # noqa: E402
from xiaoning_core.task_service import recovery_action, transition_task  # noqa: E402
from xiaoning_core.trace import TraceStore, read_events  # noqa: E402


class _TestCipher:
    search_key = b"unit-test-search-key".ljust(32, b"0")

    @staticmethod
    def _mask(context: str) -> int:
        return (sum(context.encode("utf-8")) % 251) + 1

    def encrypt(self, value: bytes, *, context: str) -> bytes:
        mask = self._mask(context)
        return bytes(item ^ mask for item in value)

    def decrypt(self, value: bytes, *, context: str) -> bytes:
        return self.encrypt(value, context=context)


def _turn(
    text: str,
    *,
    sender: str = "10001",
    scope: str = "private:10001",
    group: bool = False,
    addressed: bool = False,
    source: str = "user",
    group_id: str | None = None,
) -> TurnEnvelope:
    values = dict(
        message_id="message-1",
        conversation_scope=scope,
        channel="qq",
        sender_id=sender,
        text=text,
        source=source,
        is_group=group,
        is_addressed=addressed,
    )
    if group_id is not None:
        values["group_id"] = group_id
    return TurnEnvelope(**values)


class CoreContractTests(unittest.TestCase):
    def test_catalog_has_unique_typed_handlers(self):
        registry = CapabilityRegistry()
        specs = registry.all()
        self.assertGreaterEqual(len(specs), 20)
        self.assertEqual(len(specs), len({item.capability_id for item in specs}))
        self.assertTrue(all(item.owner and item.handler for item in specs))

    def test_route_priority_control_active_capability_chat(self):
        router = TurnRouter()
        self.assertEqual(router.decide(_turn("/记忆 查看")).owner, "astrbot_plugin_xiaoning_memory")
        active = router.decide(_turn("继续"), active_owner="claude_code_agent")
        self.assertEqual((active.kind, active.owner), (RouteKind.TASK, "claude_code_agent"))
        draw = router.decide(_turn("帮我画一张雨夜海报"))
        self.assertEqual((draw.kind, draw.capability_id, draw.owner), (RouteKind.CAPABILITY, "draw", "draw_command"))
        chat = router.decide(_turn("今天有点累，想跟你说说话"))
        self.assertEqual((chat.kind, chat.owner), (RouteKind.CHAT, "chat_router"))

    def test_group_reply_is_deterministic_and_bounded(self):
        router = TurnRouter(allowed_group_ids={"945598390"})
        silent = router.decide(_turn("大家今天都吃了什么", group=True))
        self.assertFalse(silent.should_respond)
        mentioned = router.decide(
            _turn("小柠你怎么看", group=True, addressed=True)
        )
        self.assertTrue(mentioned.should_respond)
        capability = router.decide(_turn("帮我搜索今天的新闻", group=True))
        self.assertTrue(capability.should_respond)
        safety = router.decide(_turn("救命，有人晕倒了", group=True))
        self.assertTrue(safety.should_respond)

        allowed_event = router.decide(
            _turn("今晚有活动", group=True, group_id="945598390"),
            community_event=True,
        )
        self.assertTrue(allowed_event.should_respond)
        blocked_event = router.decide(
            _turn("今晚有活动", group=True, group_id="999999999"),
            community_event=True,
        )
        self.assertFalse(blocked_event.should_respond)

    def test_non_human_turn_never_triggers_a_reply(self):
        router = TurnRouter(allowed_group_ids={"945598390"})
        decision = router.decide(
            _turn(
                "帮我搜索今天的新闻",
                group=True,
                group_id="945598390",
                source="bot",
            )
        )
        self.assertFalse(decision.should_respond)
        self.assertEqual(decision.reason_code, "non_human_source")

    def test_trace_has_fingerprints_but_no_chat_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            store = TraceStore(path)
            store.record_route(
                trace_id="a" * 32,
                scope="qq:private:10001",
                sender_id="10001",
                channel="qq",
                kind="chat",
                owner="chat_router",
                reason_code="ordinary_chat",
                confidence=0.8,
                should_respond=True,
                text_length=10,
            )
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("qq:private:10001", raw)
            events = read_events(path)
            self.assertEqual(events[0]["attributes"]["text_length"], 10)

    def test_legacy_handler_gate_is_feature_flagged_and_single_owner(self):
        class Event:
            def __init__(self, enforce, owner):
                self.values = {
                    "xiaoning_enforce_ownership": enforce,
                    "xiaoning_route_owner": owner,
                }

            def get_extra(self, key, default=None):
                return self.values.get(key, default)

        self.assertTrue(route_allows(Event(False, "draw_command"), "search_command"))
        self.assertTrue(route_allows(Event(True, "draw_command"), "draw_command"))
        self.assertFalse(route_allows(Event(True, "draw_command"), "search_command"))

    def test_core_degrades_to_no_consent_when_memory_is_unavailable(self):
        core = XiaoningCore.__new__(XiaoningCore)
        core.memory = None
        consent = core._consent_for("10001")
        self.assertFalse(consent.memory)
        self.assertFalse(consent.proactive)


class RelationshipOffloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_memory_work_does_not_block_event_loop_and_is_per_user_serial(self):
        core = XiaoningCore.__new__(XiaoningCore)
        core._memory_locks = {}
        active = 0
        peak = 0
        state_lock = threading.Lock()

        def slow_work(sender, text):
            nonlocal active, peak
            with state_lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.08)
            with state_lock:
                active -= 1
            return {"sender": sender, "text": text}

        core._process_private_relationship_turn = slow_work
        ticks = 0

        async def ticker():
            nonlocal ticks
            for _ in range(6):
                await asyncio.sleep(0.02)
                ticks += 1

        results = await asyncio.gather(
            core._run_private_relationship_turn("10001", "第一条"),
            core._run_private_relationship_turn("10001", "第二条"),
            ticker(),
        )
        self.assertEqual(peak, 1)
        self.assertGreaterEqual(ticks, 4)
        self.assertEqual(results[0]["text"], "第一条")
        self.assertEqual(results[1]["text"], "第二条")


class LocalMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "memory.sqlite3"
        self.gateway = MemoryGateway(self.path, cipher=_TestCipher())

    def tearDown(self):
        self.temp.cleanup()

    def test_consent_is_separate_and_cannot_grant_privilege(self):
        self.assertFalse(self.gateway.get_consent("10001").memory)
        updated = self.gateway.set_consent("10001", memory=True)
        self.assertTrue(updated.memory)
        self.assertFalse(updated.proactive)
        updated = self.gateway.set_consent("10001", proactive=True)
        self.assertEqual(updated.__dict__, {"memory": True, "proactive": True})
        with closing(sqlite3.connect(self.path)) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(consents)")}
        self.assertNotIn("tier", columns)
        self.assertNotIn("trusted", columns)

    def test_requires_authorization_and_verbatim_source(self):
        with self.assertRaises(PermissionError):
            self.gateway.add_memory(
                "10001",
                kind="preference",
                value="喜欢蓝色",
                source_type="user_quote",
                source_quote="记住我喜欢蓝色",
            )
        self.gateway.set_consent("10001", memory=True)
        with self.assertRaises(ValueError):
            self.gateway.add_memory(
                "10001",
                kind="preference",
                value="模型猜测用户喜欢红色",
                source_type="user_quote",
                source_quote="今天天气不错",
            )

    def test_encrypted_payload_hashed_fts_and_cross_user_isolation(self):
        self.gateway.set_consent("10001", memory=True)
        self.gateway.set_consent("10002", memory=True)
        record = self.gateway.add_memory(
            "10001",
            kind="preference",
            value="我喜欢蓝色",
            source_type="user_quote",
            source_quote="请记住我喜欢蓝色",
        )
        database_bytes = self.path.read_bytes()
        self.assertNotIn("我喜欢蓝色".encode("utf-8"), database_bytes)
        self.assertEqual(self.gateway.recall("10002", "喜欢什么颜色"), [])
        recalled = self.gateway.recall("10001", "我喜欢什么颜色")
        self.assertEqual(recalled[0].memory_id, record.memory_id)
        self.assertLessEqual(len(recalled), 8)

    def test_correction_supersedes_old_fact(self):
        self.gateway.set_consent("10001", memory=True)
        old = self.gateway.add_memory(
            "10001",
            kind="preference",
            value="我喜欢蓝色",
            source_type="user_quote",
            source_quote="请记住我喜欢蓝色",
            now=100,
        )
        new = self.gateway.add_memory(
            "10001",
            kind="preference",
            value="我现在喜欢绿色",
            source_type="user_quote",
            source_quote="请记住我现在喜欢绿色",
            supersedes_id=old.memory_id,
            now=200,
        )
        listed = self.gateway.list_memories("10001")
        self.assertEqual([item.memory_id for item in listed], [new.memory_id])
        with closing(sqlite3.connect(self.path)) as connection:
            valid_to = connection.execute(
                "SELECT valid_to FROM memory_items WHERE memory_id=?", (old.memory_id,)
            ).fetchone()[0]
        self.assertEqual(valid_to, 200)

    def test_individual_delete_is_scoped_and_idempotently_queued(self):
        self.gateway.set_consent("10001", memory=True)
        self.gateway.set_consent("10002", memory=True)
        own = self.gateway.add_memory(
            "10001",
            kind="preference",
            value="我喜欢徒步",
            source_type="user_quote",
            source_quote="记住我喜欢徒步",
        )
        other = self.gateway.add_memory(
            "10002",
            kind="preference",
            value="我喜欢游泳",
            source_type="user_quote",
            source_quote="记住我喜欢游泳",
        )
        with self.assertRaises(ValueError):
            self.gateway.delete_memory("10001", other.memory_id[:8])
        deleted = self.gateway.delete_memory("10001", own.memory_id[:8])
        self.assertEqual(deleted, own.memory_id)
        self.assertEqual(self.gateway.list_memories("10001"), [])
        self.assertEqual(len(self.gateway.list_memories("10002")), 1)

    def test_delete_all_requires_second_confirmation_and_enqueues_sync(self):
        self.gateway.set_consent("10001", memory=True)
        self.gateway.add_memory(
            "10001",
            kind="relationship",
            value="我把小柠当长期伙伴",
            source_type="user_quote",
            source_quote="记住我把小柠当长期伙伴",
            now=99,
        )
        token = self.gateway.request_delete_all("10001", now=100)
        self.assertFalse(self.gateway.confirm_delete_all("10001", "111111", now=101))
        self.assertEqual(len(self.gateway.list_memories("10001")), 1)
        self.assertTrue(self.gateway.confirm_delete_all("10001", token, now=101))
        self.assertEqual(self.gateway.list_memories("10001"), [])
        self.assertEqual(self.gateway.pending_sync_count(), 2)
        events = self.gateway.pending_sync()
        self.assertEqual([item.operation for item in events], ["upsert", "delete_all"])
        self.assertNotIn("source_quote", events[0].payload)
        self.gateway.mark_sync(events[0].event_id, succeeded=True)
        self.assertEqual(self.gateway.pending_sync_count(), 1)

    def test_equal_timestamp_outbox_events_keep_causal_order(self):
        self.gateway.set_consent("10001", memory=True)
        self.gateway.add_memory(
            "10001",
            kind="preference",
            value="我喜欢散步",
            source_type="user_quote",
            source_quote="记住我喜欢散步",
            now=100,
        )
        token = self.gateway.request_delete_all("10001", now=100)
        self.assertTrue(
            self.gateway.confirm_delete_all("10001", token, now=100)
        )
        self.assertEqual(
            [item.operation for item in self.gateway.pending_sync()],
            ["upsert", "delete_all"],
        )


class TaskLifecycleTests(unittest.TestCase):
    def _task(self) -> TaskRecord:
        return TaskRecord(
            task_id="task-1",
            owner_fingerprint="owner0001",
            scope_fingerprint="scope0001",
            idempotency_key="idempotency-0001",
            risk=RiskLevel.LOW,
        )

    def test_delivery_pending_can_retry_delivery_but_not_execution(self):
        task = self._task()
        for state in (
            TaskState.PLANNED,
            TaskState.EXECUTING,
            TaskState.VERIFYING,
            TaskState.DELIVERING,
            TaskState.DELIVERY_PENDING,
        ):
            task = transition_task(task, state)
        self.assertEqual(recovery_action(task), "retry_delivery_only")
        with self.assertRaises(ValueError):
            transition_task(task, TaskState.EXECUTING)

    def test_completed_requires_recipient_delivery_evidence(self):
        task = self._task()
        for state in (
            TaskState.PLANNED,
            TaskState.EXECUTING,
            TaskState.VERIFYING,
            TaskState.DELIVERING,
        ):
            task = transition_task(task, state)
        with self.assertRaises(ValueError):
            transition_task(task, TaskState.COMPLETED)
        completed = transition_task(
            task,
            TaskState.COMPLETED,
            evidence={"kind": "delivery_receipt", "confirmed": True, "channel": "qq"},
        )
        self.assertEqual(completed.state, TaskState.COMPLETED)


if __name__ == "__main__":
    unittest.main()
