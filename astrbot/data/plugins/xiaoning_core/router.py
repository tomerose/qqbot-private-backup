"""Deterministic, single-owner routing for one Xiaoning turn."""

from __future__ import annotations

import re

from .capabilities import CapabilityRegistry
from .models import RouteDecision, RouteKind, TurnEnvelope


_CONTROL_OWNERS = (
    (re.compile(r"^[/／]记忆(?:\s|$)", re.IGNORECASE), "astrbot_plugin_xiaoning_memory"),
    (re.compile(r"^[/／]主动(?:\s|$)", re.IGNORECASE), "xiaoning_core"),
    (
        re.compile(
            r"^[/／](?:任务|agent\s+(?:approve|cancel|status)|审批)(?:\s|$)",
            re.IGNORECASE,
        ),
        "claude_code_agent",
    ),
)
_SAFETY_RE = re.compile(
    r"(?:自杀|自伤|想死|不想活|活不下去|割腕|救命|紧急|报警|\b(?:110|120)\b|"
    r"kill\s*myself|suicide)",
    re.IGNORECASE,
)
_TASK_RE = re.compile(
    r"(?:帮我|请你|能否|可以).{0,12}(?:做一份|生成|创建|整理成|修改|交付).{0,18}"
    r"(?:文档|文件|Word|PDF|PPT|表格|报告|网页)",
    re.IGNORECASE,
)
_PERSONA_QUESTION_RE = re.compile(
    r"(?:你|小柠).{0,20}(?:多大|几岁|生日|住哪|哪座城市|哪里长大|"
    r"做什么工作|学的什么|有对象|单身|喜欢听谁|今天做了什么|父母|具体住址)",
    re.IGNORECASE,
)


class TurnRouter:
    def __init__(
        self,
        registry: CapabilityRegistry | None = None,
        allowed_group_ids: set[str] | frozenset[str] | None = None,
    ):
        self.registry = registry or CapabilityRegistry()
        self.allowed_group_ids = frozenset(
            str(item).strip() for item in (allowed_group_ids or ()) if str(item).strip()
        )

    def decide(
        self,
        turn: TurnEnvelope,
        *,
        active_owner: str | None = None,
        pending_approval_owner: str | None = None,
        community_event: bool = False,
    ) -> RouteDecision:
        text = turn.text.strip()

        if turn.source.strip().casefold() not in {"user", "human", "member"}:
            return RouteDecision(
                kind=RouteKind.CHAT,
                owner="chat_router",
                confidence=1.0,
                reason_code="non_human_source",
                should_respond=False,
            )

        for pattern, owner in _CONTROL_OWNERS:
            if pattern.search(text):
                return RouteDecision(
                    kind=RouteKind.CONTROL,
                    owner=owner,
                    confidence=1.0,
                    reason_code="explicit_control_command",
                    should_respond=True,
                )

        if pending_approval_owner and text.casefold() in {
            "同意", "批准", "继续", "取消", "拒绝", "approve", "cancel",
        }:
            return RouteDecision(
                kind=RouteKind.CONTROL,
                owner=pending_approval_owner,
                confidence=0.99,
                reason_code="pending_approval_reply",
            )

        if active_owner:
            return RouteDecision(
                kind=RouteKind.TASK,
                owner=active_owner,
                confidence=0.98,
                reason_code="active_multiturn_owner",
            )

        if _SAFETY_RE.search(text):
            return RouteDecision(
                kind=RouteKind.CHAT,
                owner="chat_router",
                confidence=1.0,
                reason_code="safety_support",
            )

        if _PERSONA_QUESTION_RE.search(text):
            return RouteDecision(
                kind=RouteKind.CHAT,
                owner="chat_router",
                confidence=0.98,
                reason_code="persona_question",
                should_respond=self._group_can_respond(
                    turn, explicit=False, community_event=community_event
                ),
            )

        matches = self.registry.matches(text)
        if matches:
            best_score = matches[0][0]
            best = [item for score, item in matches if score == best_score]
            owners = {item.owner for item in best}
            if len(owners) > 1:
                return RouteDecision(
                    kind=RouteKind.CHAT,
                    owner="chat_router",
                    confidence=0.45,
                    reason_code="ambiguous_capability",
                    needs_clarification=True,
                    should_respond=self._group_can_respond(
                        turn, explicit=True, community_event=community_event
                    ),
                )
            spec = best[0]
            route_kind = RouteKind.TASK if _TASK_RE.search(text) else spec.route_kind
            return RouteDecision(
                kind=route_kind,
                owner=spec.owner,
                capability_id=spec.capability_id,
                confidence=0.98 if best_score >= 1000 else min(0.96, 0.72 + best_score / 100),
                reason_code="explicit_command" if best_score >= 1000 else "unique_capability_match",
                should_respond=self._group_can_respond(
                    turn, explicit=True, community_event=community_event
                ),
            )

        return RouteDecision(
            kind=RouteKind.CHAT,
            owner="chat_router",
            confidence=0.85,
            reason_code="ordinary_chat",
            should_respond=self._group_can_respond(
                turn, explicit=False, community_event=community_event
            ),
        )

    def _group_can_respond(
        self, turn: TurnEnvelope, *, explicit: bool, community_event: bool
    ) -> bool:
        if not turn.is_group:
            return True
        allowed_community_event = bool(
            community_event and turn.group_id in self.allowed_group_ids
        )
        return bool(turn.is_addressed or explicit or allowed_community_event)
