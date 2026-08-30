"""AstrBot adapter for Xiaoning's single turn kernel.

The first release intentionally runs routing in shadow mode. It produces one
decision and one trace per turn, while legacy capability handlers still execute.
The deterministic group reply decision is already safe to consume because it
only widens replies for explicit, addressed, safety, or configured-event turns.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .memory import ConsentSnapshot, MemoryGateway
from .models import TurnEnvelope
from .models import ProactiveMode
from .relationship import (
    extract_open_loops,
    open_loop_mutation_kind,
    parse_proactive_preference,
    relevant_open_loops,
    select_open_loops_for_mutation,
)
from .router import TurnRouter
from .trace import TraceStore, new_trace_id


_SAFETY_RE = re.compile(
    r"(?:自杀|自伤|想死|不想活|活不下去|割腕|救命|紧急|报警|\b(?:110|120)\b|"
    r"kill\s*myself|suicide)",
    re.IGNORECASE,
)


class XiaoningCore(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        data_dir = Path(StarTools.get_data_dir("xiaoning_core"))
        data_dir.mkdir(parents=True, exist_ok=True)
        allowed_groups = {
            str(item).strip()
            for item in (self.config.get("allowed_group_ids", []) or [])
            if str(item).strip()
        }
        self._allowed_group_ids = frozenset(allowed_groups)
        self.router = TurnRouter(allowed_group_ids=allowed_groups)
        self.trace = TraceStore(data_dir / "events.jsonl")
        self.memory: MemoryGateway | None = None
        try:
            self.memory = MemoryGateway(data_dir / "xiaoning-memory.sqlite3")
        except Exception as exc:
            # Routing and privacy-safe tracing remain available if Windows
            # DPAPI or ACL initialization is temporarily unavailable.
            logger.error(
                "[XiaoningCore] encrypted memory unavailable: %s",
                type(exc).__name__,
            )
        self._group_threads: dict[tuple[str, str], float] = {}
        # SQLite/DPAPI work is blocking.  A per-user lock preserves causal order
        # while asyncio.to_thread keeps one slow/locked memory database from
        # freezing unrelated QQ replies.
        self._memory_locks: dict[str, asyncio.Lock] = {}

    def _process_private_relationship_turn(self, sender: str, text: str) -> dict:
        """Run one private relationship transaction outside the event loop."""

        memory = self.memory
        if memory is None:
            return {}

        preference = parse_proactive_preference(text)
        if preference is not None:
            memory.set_proactive_mode(sender, preference)

        previous_profile = memory.get_relationship_profile(sender)
        profile = memory.record_private_turn(sender, text)
        activation_notice = memory.consume_activation_notice(sender)
        mutation_kind = open_loop_mutation_kind(text)
        current_loops = memory.list_open_loops(sender, limit=20)
        deletion_ids = select_open_loops_for_mutation(current_loops, text)
        deleted = sum(
            memory.delete_open_loops(sender, loop_id=loop_id)
            for loop_id in deletion_ids
        )
        mutation_event = None
        if deleted:
            mutation_event = (
                "memory_deleted"
                if mutation_kind in {"forget_one", "forget_all"}
                else "memory_corrected"
            )
            memory.record_engagement(
                sender,
                mutation_event,
                attributes={"count": deleted},
            )

        created_loop_types: list[str] = []
        consent = memory.get_consent(sender)
        # A correction may both invalidate the old loop and provide the corrected
        # evidence.  A forget request must never be re-ingested as a new loop.
        if (
            profile.activated
            and consent.memory
            and mutation_kind not in {"forget_one", "forget_all"}
        ):
            for loop in extract_open_loops(text, now=time.time()):
                stored = memory.upsert_open_loop(sender, loop)
                memory.enqueue_open_loop_candidate(sender, stored)
                created_loop_types.append(stored.loop_type.value)

        relevant_summaries: list[str] = []
        if profile.activated:
            remaining = memory.list_open_loops(sender, limit=20)
            relevant_summaries = [
                item.evidence_summary
                for item in relevant_open_loops(remaining, text, limit=3)
            ]

        return {
            "preference": preference,
            "profile": profile,
            "activation_notice": activation_notice,
            "proactive_reply": bool(previous_profile.unanswered_proactive),
            "mutation_event": mutation_event,
            "deleted": deleted,
            "created_loop_types": created_loop_types,
            "relevant_summaries": relevant_summaries,
        }

    async def _run_private_relationship_turn(self, sender: str, text: str) -> dict:
        lock = self._memory_locks.setdefault(sender, asyncio.Lock())
        async with lock:
            return await asyncio.to_thread(
                self._process_private_relationship_turn, sender, text
            )

    def _consent_for(self, sender: str) -> ConsentSnapshot:
        if self.memory is None:
            return ConsentSnapshot()
        try:
            return self.memory.get_consent(sender)
        except Exception as exc:
            logger.warning(
                "[XiaoningCore] consent lookup failed: %s", type(exc).__name__
            )
            return ConsentSnapshot()

    @staticmethod
    def _event_text(event: AstrMessageEvent) -> str:
        return str(getattr(event, "get_message_str", lambda: "")() or "").strip()

    @staticmethod
    def _message_id(event: AstrMessageEvent) -> str:
        message_obj = getattr(event, "message_obj", None)
        value = getattr(message_obj, "message_id", None)
        if value is None:
            raw = getattr(message_obj, "raw_message", None)
            if isinstance(raw, dict):
                value = raw.get("message_id")
        return str(value) if value is not None else uuid.uuid4().hex

    @staticmethod
    def _channel(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_platform_name", None)
        if callable(getter):
            try:
                value = str(getter() or "").strip()
                if value:
                    return value[:40]
            except Exception:
                pass
        return type(event).__name__[:40] or "unknown"

    @staticmethod
    def _media_refs(event: AstrMessageEvent) -> tuple[str, ...]:
        getter = getattr(event, "get_messages", None)
        if not callable(getter):
            return ()
        try:
            # Persist only component kinds, never paths, URLs, or media contents.
            return tuple(type(item).__name__[:40] for item in list(getter() or [])[:12])
        except Exception:
            return ()

    def _community_event(self, text: str) -> bool:
        configured = self.config.get("community_event_keywords", []) or []
        value = text.casefold()
        return any(
            str(keyword).strip() and str(keyword).strip().casefold() in value
            for keyword in configured
        )

    def _event_source(self, event: AstrMessageEvent, sender: str) -> str:
        configured_bots = self.config.get("bot_sender_ids", []) or []
        if isinstance(configured_bots, str):
            configured_bots = (configured_bots,)
        known_bots = {
            str(item).strip().casefold()
            for item in configured_bots
            if str(item).strip()
        }
        self_id = str(getattr(event, "get_self_id", lambda: "")() or "").strip()
        message_obj = getattr(event, "message_obj", None)
        sender_obj = getattr(message_obj, "sender", None)
        sender_name = str(
            getattr(sender_obj, "nickname", None)
            or getattr(sender_obj, "card", None)
            or getattr(sender_obj, "name", None)
            or ""
        ).strip()
        raw = getattr(message_obj, "raw_message", None)
        raw_sender = raw.get("sender", {}) if isinstance(raw, dict) else {}
        bot_flag = bool(
            (isinstance(raw, dict) and (raw.get("is_bot") or raw.get("bot")))
            or (
                isinstance(raw_sender, dict)
                and (
                    raw_sender.get("is_bot")
                    or raw_sender.get("bot")
                    or str(raw_sender.get("role", "")).casefold() == "bot"
                )
            )
        )
        if (
            not sender
            or (self_id and sender == self_id)
            or sender.casefold() in known_bots
            or sender_name.casefold() in known_bots
            or bot_flag
        ):
            return "bot"
        return "human"

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=997)
    async def observe_turn(self, event: AstrMessageEvent):
        scope = str(getattr(event, "unified_msg_origin", "") or "").strip()
        sender = str(getattr(event, "get_sender_id", lambda: "")() or "").strip()
        if not scope or not sender:
            return
        text = self._event_text(event)
        is_group = not bool(event.is_private_chat())
        group_id = str(getattr(event, "get_group_id", lambda: "")() or "").strip()
        source = self._event_source(event, sender)
        addressed = bool(getattr(event, "is_at_or_wake_command", False))
        key = (scope, sender)
        now = time.monotonic()
        followup_seconds = max(30, int(self.config.get("group_followup_seconds", 300)))
        if (
            is_group
            and group_id in self._allowed_group_ids
            and self._group_threads.get(key, 0.0) >= now
        ):
            addressed = True

        consent = self._consent_for(sender)
        turn = TurnEnvelope(
            message_id=self._message_id(event),
            conversation_scope=scope,
            channel=self._channel(event),
            sender_id=sender,
            text=text,
            media_refs=self._media_refs(event),
            source=source,
            consent_snapshot={"memory": consent.memory, "proactive": consent.proactive},
            is_group=is_group,
            group_id=group_id,
            is_addressed=addressed,
        )
        community_event = self._community_event(text)
        decision = self.router.decide(turn, community_event=community_event)
        trace_id = new_trace_id()
        event.set_extra("xiaoning_trace_id", trace_id)
        event.set_extra("xiaoning_route_kind", decision.kind.value)
        event.set_extra("xiaoning_route_owner", decision.owner)
        event.set_extra("xiaoning_route_reason", decision.reason_code)
        event.set_extra("xiaoning_route_confidence", decision.confidence)
        event.set_extra("xiaoning_route_needs_clarification", decision.needs_clarification)
        event.set_extra(
            "xiaoning_enforce_ownership",
            bool(self.config.get("enforce_ownership", False)),
        )
        event.set_extra("xiaoning_memory_consent", consent.memory)
        event.set_extra("xiaoning_proactive_consent", consent.proactive)
        event.set_extra("xiaoning_human_source", source == "human")
        event.set_extra("xiaoning_force_group_reply", bool(decision.should_respond))

        self.trace.record_route(
            trace_id=trace_id,
            scope=scope,
            sender_id=sender,
            channel=turn.channel,
            kind=decision.kind.value,
            owner=decision.owner,
            reason_code=decision.reason_code,
            confidence=decision.confidence,
            should_respond=decision.should_respond,
            text_length=len(text),
        )

        if source != "human":
            event.stop_event()
            return

        if not is_group and self.memory is not None:
            try:
                relationship = await self._run_private_relationship_turn(sender, text)
                preference = relationship.get("preference")
                if preference is not None:
                    messages = {
                        ProactiveMode.PAUSED: "已立即暂停主动联系；正常聊天不受影响。",
                        ProactiveMode.REDUCED: "已把主动联系降到每周最多一次。",
                        ProactiveMode.NORMAL: "已恢复正常主动联系频率。",
                    }
                    event.set_extra("xiaoning_relationship_notice", messages[preference])
                    self.trace.record_engagement(
                        trace_id=trace_id,
                        scope=scope,
                        event_type="proactive_preference",
                        attributes={"mode": preference.value},
                    )

                profile = relationship["profile"]
                event.set_extra("xiaoning_relationship_activated", profile.activated)
                event.set_extra(
                    "xiaoning_relationship_temperature",
                    profile.relationship_temperature,
                )
                if relationship.get("activation_notice"):
                    event.set_extra(
                        "xiaoning_activation_notice",
                        "聊到现在，我会安全记住你明确留下的未完话题，也可能在合适时接着问；不想我主动找你，直接说一声就行。",
                    )
                    self.trace.record_engagement(
                        trace_id=trace_id,
                        scope=scope,
                        event_type="relationship_activated",
                    )
                else:
                    self.trace.record_engagement(
                        trace_id=trace_id,
                        scope=scope,
                        event_type="active_day",
                        attributes={"activated": profile.activated},
                    )
                if relationship.get("proactive_reply"):
                    self.trace.record_engagement(
                        trace_id=trace_id,
                        scope=scope,
                        event_type="proactive_reply",
                    )

                if relationship.get("mutation_event"):
                    self.trace.record_engagement(
                        trace_id=trace_id,
                        scope=scope,
                        event_type=relationship["mutation_event"],
                        attributes={"count": relationship["deleted"]},
                    )
                for loop_type in relationship.get("created_loop_types", []):
                    self.trace.record_engagement(
                        trace_id=trace_id,
                        scope=scope,
                        event_type="open_loop_created",
                        attributes={"loop_type": loop_type},
                    )

                if relationship.get("relevant_summaries"):
                    event.set_extra(
                        "xiaoning_relevant_open_loops",
                        relationship["relevant_summaries"][:3],
                    )
            except Exception as exc:
                logger.warning(
                    "[XiaoningCore] relationship continuity unavailable: %s",
                    type(exc).__name__,
                )

        if is_group and group_id in self._allowed_group_ids and decision.should_respond and (
            addressed
            or decision.kind.value in {"capability", "task", "control"}
            or _SAFETY_RE.search(text)
            or community_event
        ):
            self._group_threads[key] = now + followup_seconds
        if len(self._group_threads) > 2000:
            self._group_threads = {
                item_key: deadline
                for item_key, deadline in self._group_threads.items()
                if deadline >= now
            }

    @filter.command("主动")
    async def proactive_control(self, event: AstrMessageEvent):
        sender = str(getattr(event, "get_sender_id", lambda: "")() or "").strip()
        if not sender or not event.is_private_chat():
            yield event.plain_result("主动联系设置只可由本人在私聊中修改。")
            event.stop_event()
            return
        if self.memory is None:
            yield event.plain_result(
                "本地加密授权存储暂不可用；主动联系保持关闭，未修改任何设置。"
            )
            event.stop_event()
            return
        parts = self._event_text(event).split()
        sub = parts[1].strip() if len(parts) > 1 else ""
        if sub in {"开启", "开", "enable", "on"}:
            try:
                self.memory.set_consent(sender, proactive=True)
            except Exception:
                yield event.plain_result("主动联系设置保存失败，仍保持关闭。")
                event.stop_event()
                return
            yield event.plain_result(
                "已开启主动联系。小柠只会围绕明确承诺、日程、生日或高度相关事件联系你；随时可用 /主动 暂停。"
            )
        elif sub in {"暂停", "关闭", "停", "disable", "off"}:
            try:
                self.memory.set_consent(sender, proactive=False)
            except Exception:
                yield event.plain_result("主动联系设置保存失败；请稍后重试。")
                event.stop_event()
                return
            yield event.plain_result("已暂停主动联系，不会影响你主动找小柠聊天或办事。")
        else:
            enabled = self._consent_for(sender).proactive
            yield event.plain_result(
                f"主动联系当前为：{'已开启' if enabled else '已暂停'}。\n"
                "可用：/主动 开启 | /主动 暂停"
            )
        event.stop_event()

    async def initialize(self):
        logger.info(
            "[XiaoningCore] ready | shadow=%s ownership=%s capabilities=%d",
            bool(self.config.get("shadow_mode", True)),
            bool(self.config.get("enforce_ownership", False)),
            len(self.router.registry.all()),
        )
