"""Stable, typed contracts shared by Xiaoning plugins.

These models deliberately contain only bounded metadata. Raw chat content stays on
the in-process TurnEnvelope and is never included in persisted DomainEvent data.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RouteKind(str, Enum):
    CHAT = "chat"
    CAPABILITY = "capability"
    TASK = "task"
    CONTROL = "control"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TaskState(str, Enum):
    ACCEPTED = "accepted"
    PLANNED = "planned"
    AWAITING_APPROVAL = "awaiting_approval"
    QUEUED = "queued"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    DELIVERING = "delivering"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    DELIVERY_PENDING = "delivery_pending"


class ProactiveMode(str, Enum):
    NORMAL = "normal"
    REDUCED = "reduced"
    PAUSED = "paused"


class OpenLoopType(str, Enum):
    FUTURE_EVENT = "future_event"
    ONGOING_PLAN = "ongoing_plan"
    CONTINUE_LATER = "continue_later"
    REMINDER = "reminder"
    COMMITMENT = "commitment"


class OpenLoopStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    INVALIDATED = "invalidated"


class CandidateStatus(str, Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    SENT = "sent"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class TurnEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    message_id: str = Field(min_length=1, max_length=160)
    conversation_scope: str = Field(min_length=1, max_length=500)
    channel: str = Field(min_length=1, max_length=40)
    sender_id: str = Field(min_length=1, max_length=160)
    text: str = Field(default="", max_length=12000)
    media_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=12)
    source: str = Field(default="user", max_length=40)
    consent_snapshot: dict[str, bool] = Field(default_factory=dict)
    is_group: bool = False
    group_id: str = Field(default="", max_length=160)
    is_addressed: bool = False


class RouteDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: RouteKind
    owner: str = Field(min_length=1, max_length=100)
    capability_id: str | None = Field(default=None, max_length=100)
    confidence: float = Field(ge=0.0, le=1.0)
    reason_code: str = Field(min_length=1, max_length=100)
    needs_clarification: bool = False
    should_respond: bool = True


class CapabilitySpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    capability_id: str = Field(min_length=1, max_length=100)
    owner: str = Field(min_length=1, max_length=100)
    route_kind: RouteKind = RouteKind.CAPABILITY
    risk: RiskLevel = RiskLevel.LOW
    eligible_tiers: tuple[str, ...] = ("ordinary", "x", "pro")
    keywords: tuple[str, ...] = ()
    command: str = ""
    available: bool = True
    artifact_types: tuple[str, ...] = ()
    delivery_required: bool = False
    handler: str = ""


class ContextBundle(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    recent_dialogue: tuple[dict[str, Any], ...] = ()
    persona: str = ""
    relationship_facts: tuple[dict[str, Any], ...] = ()
    memories: tuple[dict[str, Any], ...] = ()
    commitments: tuple[dict[str, Any], ...] = ()
    tasks: tuple[dict[str, Any], ...] = ()
    token_budgets: dict[str, int] = Field(
        default_factory=lambda: {
            "dialogue": 2400,
            "persona": 900,
            "memory": 900,
            "tasks": 700,
        }
    )


class RelationshipProfile(BaseModel):
    """Per-private-chat relationship state; serialized only inside encrypted storage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    activated: bool = False
    meaningful_turns: int = Field(default=0, ge=0)
    active_dates: tuple[str, ...] = ()
    proactive_mode: ProactiveMode = ProactiveMode.NORMAL
    relationship_temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    last_user_at: float = Field(default=0.0, ge=0.0)
    unanswered_proactive: int = Field(default=0, ge=0)
    activation_notice_pending: bool = False
    first_proactive_notice_sent: bool = False


class OpenLoop(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    loop_id: str = Field(min_length=8, max_length=100)
    loop_type: OpenLoopType
    evidence_summary: str = Field(min_length=1, max_length=280)
    why_follow_up: str = Field(min_length=1, max_length=240)
    not_before: float = Field(ge=0.0)
    expires_at: float = Field(ge=0.0)
    sensitivity: RiskLevel = RiskLevel.LOW
    status: OpenLoopStatus = OpenLoopStatus.OPEN
    dedupe_key: str = Field(min_length=16, max_length=128)
    created_at: float = Field(ge=0.0)


class ProactiveCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str = Field(min_length=8, max_length=100)
    open_loop_id: str = Field(default="", max_length=100)
    why_now: str = Field(min_length=1, max_length=280)
    source_type: str = Field(min_length=1, max_length=60)
    relevance: float = Field(ge=0.0, le=1.0)
    timing: float = Field(ge=0.0, le=1.0)
    novelty: float = Field(ge=0.0, le=1.0)
    evidence_confidence: float = Field(ge=0.0, le=1.0)
    not_before: float = Field(ge=0.0)
    expires_at: float = Field(ge=0.0)
    status: CandidateStatus = CandidateStatus.PENDING
    idempotency_key: str = Field(default="", max_length=160)

    @property
    def score(self) -> float:
        return round(
            self.relevance * 0.40
            + self.timing * 0.20
            + self.novelty * 0.15
            + self.evidence_confidence * 0.25,
            6,
        )


class PersonaEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    category: str = Field(min_length=1, max_length=60)
    narrative: str = Field(min_length=1, max_length=280)
    canon_version: str = Field(min_length=1, max_length=40)


class EngagementEvent(BaseModel):
    """Anonymous metrics only: never include chat text or raw user identifiers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: str = Field(min_length=1, max_length=80)
    scope_fingerprint: str = Field(min_length=8, max_length=128)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    attributes: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class TaskRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(min_length=1, max_length=100)
    owner_fingerprint: str = Field(min_length=8, max_length=128)
    scope_fingerprint: str = Field(min_length=8, max_length=128)
    idempotency_key: str = Field(min_length=8, max_length=160)
    plan: tuple[dict[str, Any], ...] = ()
    risk: RiskLevel = RiskLevel.LOW
    state: TaskState = TaskState.ACCEPTED
    verification_evidence: tuple[dict[str, Any], ...] = ()
    delivery_cursor: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DomainEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_id: str = Field(min_length=16, max_length=64)
    event_type: str = Field(min_length=1, max_length=100)
    stage: str = Field(min_length=1, max_length=80)
    elapsed_ms: int = Field(default=0, ge=0)
    result_code: str = Field(default="ok", max_length=100)
    attributes: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("attributes")
    @classmethod
    def _bound_attributes(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > 24:
            raise ValueError("domain event attributes are bounded to 24 fields")
        for key, item in value.items():
            if len(str(key)) > 80 or (isinstance(item, str) and len(item) > 240):
                raise ValueError("domain event attribute is too long")
        return value


TaskTerminalState = Literal[
    TaskState.COMPLETED,
    TaskState.FAILED,
    TaskState.CANCELLED,
    TaskState.TIMEOUT,
    TaskState.DELIVERY_PENDING,
]
