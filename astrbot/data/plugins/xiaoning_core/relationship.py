"""Deterministic relationship continuity and proactive-send policy.

The model may phrase a follow-up, but it cannot grant permission to store or send.
Those decisions stay in this module and are covered by virtual-clock tests.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .models import (
    OpenLoop,
    OpenLoopType,
    ProactiveCandidate,
    ProactiveMode,
    RelationshipProfile,
    RiskLevel,
)


UTC8 = timezone(timedelta(hours=8))

_FILLER_RE = re.compile(r"^(?:嗯+|哦+|啊+|哈+|好的?|行|在吗|？|\?|。|\.|哈哈)+$")
_SENSITIVE_RE = re.compile(
    r"(?:密码|口令|验证码|token|api.?key|银行卡|身份证|护照|账号凭据|"
    r"住址|地址是|几号楼|门牌|诊断|病历|看病|复诊|医院|手术|处方|抑郁症|癌症|性经历|亲密隐私|"
    r"朋友的隐私|同事的隐私|第三方隐私)",
    re.IGNORECASE,
)
_EVENT_RE = re.compile(
    r"(?:今天|明天|后天|周[一二三四五六日天]|下周|月底|下个月|\d{1,2}[月号日])"
    r".{0,32}(?:考试|面试|出行|旅行|出差|答辩|开会|入职|复诊|比赛|发布|上线|截止|搬家|项目)"
)
_ONGOING_RE = re.compile(r"(?:正在|最近在|还在|继续|推进|准备|赶|做).{1,50}(?:项目|计划|作品集|论文|考试|面试|旅行|搬家|申请)")
_CONTINUE_RE = re.compile(r"(?:改天|下次|回头|晚点|过两天|之后).{0,16}(?:继续|再聊|告诉你|跟你说|说结果)")
_REMINDER_RE = re.compile(r"(?:提醒我|别忘了提醒|到时候叫我)")
_COMMITMENT_RE = re.compile(r"(?:我答应|我会回来|说好了|到时(?:候)?跟你说|完了跟你说|面完跟你说)")
_FORGET_ALL_RE = re.compile(
    r"(?:全部|所有|都).{0,10}(?:忘掉|删除|别记)|(?:忘掉|删除).{0,10}(?:全部|所有)"
)
_FORGET_ONE_RE = re.compile(r"别记这个|忘了刚才(?:的事)?|删除刚才|忘掉这个")
_CORRECTION_RE = re.compile(r"(?:不是|说错了|记错了|改成).{0,40}(?:其实|应该|才是|是)?")


@dataclass(frozen=True)
class ProactiveDecision:
    allowed: bool
    reason: str
    score: float


def is_meaningful_private_turn(text: object) -> bool:
    value = str(text or "").strip()
    return len(value) >= 4 and not _FILLER_RE.fullmatch(value)


def _dedupe(value: str) -> str:
    normalized = re.sub(r"\s+", "", value).casefold()
    return hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()


def extract_open_loops(text: object, *, now: float) -> list[OpenLoop]:
    """Extract one evidence-backed loop; uncertainty safely becomes no record."""

    value = str(text or "").strip()
    if len(value) < 4 or _SENSITIVE_RE.search(value):
        return []

    loop_type: OpenLoopType | None = None
    reason = ""
    if _REMINDER_RE.search(value):
        loop_type, reason = OpenLoopType.REMINDER, "用户明确建立了提醒"
    elif _EVENT_RE.search(value):
        loop_type, reason = OpenLoopType.FUTURE_EVENT, "用户提到了有时间点的未来事件"
    elif _CONTINUE_RE.search(value):
        loop_type, reason = OpenLoopType.CONTINUE_LATER, "用户明确表示之后继续"
    elif _COMMITMENT_RE.search(value):
        loop_type, reason = OpenLoopType.COMMITMENT, "用户留下了明确承诺"
    elif _ONGOING_RE.search(value):
        loop_type, reason = OpenLoopType.ONGOING_PLAN, "用户正在推进一件具体的事"
    if loop_type is None:
        return []

    start = float(now) + 18 * 3600
    end = float(now) + 72 * 3600
    summary = value[:280]
    return [
        OpenLoop(
            loop_id=f"loop-{uuid.uuid4().hex}",
            loop_type=loop_type,
            evidence_summary=summary,
            why_follow_up=reason,
            not_before=start,
            expires_at=end,
            sensitivity=RiskLevel.LOW,
            dedupe_key=_dedupe(summary),
            created_at=float(now),
        )
    ]


def parse_proactive_preference(text: object) -> ProactiveMode | None:
    value = str(text or "").strip()
    if re.search(r"(?:别|不要|停止|暂停)(?:再)?(?:主动)?(?:找我|联系我|发消息)|安静一点", value):
        return ProactiveMode.PAUSED
    if re.fullmatch(r"(?:主动联系)?少一点(?:就好)?|少联系我", value):
        return ProactiveMode.REDUCED
    if re.search(r"恢复正常|可以主动找我|继续主动联系", value):
        return ProactiveMode.NORMAL
    return None


def relevant_open_loops(
    loops: Iterable[OpenLoop], text: object, *, limit: int = 3
) -> list[OpenLoop]:
    """Return only loops that share concrete text with the current turn."""

    value = re.sub(r"\s+", "", str(text or "")).casefold()
    if len(value) < 2:
        return []
    current = {value[index : index + 2] for index in range(len(value) - 1)}
    ranked: list[tuple[int, OpenLoop]] = []
    for loop in loops:
        evidence = re.sub(r"\s+", "", loop.evidence_summary).casefold()
        tokens = {evidence[index : index + 2] for index in range(max(0, len(evidence) - 1))}
        overlap = len(current & tokens)
        if overlap >= 1:
            ranked.append((overlap, loop))
    ranked.sort(key=lambda item: (item[0], item[1].created_at), reverse=True)
    return [item[1] for item in ranked[: max(0, min(3, int(limit)))]]


def open_loop_mutation_kind(text: object) -> str | None:
    """Classify an explicit user correction/deletion without guessing intent."""

    value = str(text or "").strip()
    if _FORGET_ALL_RE.search(value):
        return "forget_all"
    if _FORGET_ONE_RE.search(value):
        return "forget_one"
    if _CORRECTION_RE.search(value):
        return "correction"
    return None


def select_open_loops_for_mutation(
    loops: Iterable[OpenLoop], text: object
) -> list[str]:
    """Return only the loop IDs an explicit correction/deletion may invalidate.

    ``loops`` is expected newest-first, matching ``MemoryGateway.list_open_loops``.
    A vague correction never wipes unrelated topics.  A direct "forget that"
    removes the newest loop when it contains no identifying words; deleting all
    requires explicit all/whole wording.
    """

    available = list(loops)
    if not available:
        return []
    kind = open_loop_mutation_kind(text)
    if kind is None:
        return []
    if kind == "forget_all":
        return [item.loop_id for item in available]

    matched = relevant_open_loops(available, text, limit=3)
    if matched:
        # A singular correction/deletion must not fan out merely because two
        # topics share generic bigrams such as "明天" or "跟你说".
        return [matched[0].loop_id]
    if kind == "forget_one":
        return [available[0].loop_id]
    return []


def _week_start(moment: datetime) -> datetime:
    local = moment.astimezone(UTC8)
    return (local - timedelta(days=local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def evaluate_proactive_send(
    profile: RelationshipProfile,
    candidate: ProactiveCandidate,
    now: datetime,
    sent_at: Iterable[float],
) -> ProactiveDecision:
    local_now = now.astimezone(UTC8)
    score = candidate.score
    if not profile.activated:
        return ProactiveDecision(False, "not_activated", score)
    if profile.proactive_mode is ProactiveMode.PAUSED:
        return ProactiveDecision(False, "paused", score)
    if profile.unanswered_proactive >= 1:
        return ProactiveDecision(False, "awaiting_reply", score)
    minute = local_now.hour * 60 + local_now.minute
    if minute < 9 * 60 + 30 or minute > 22 * 60 + 30:
        return ProactiveDecision(False, "quiet_hours", score)
    if profile.last_user_at and local_now.timestamp() - profile.last_user_at < 6 * 3600:
        return ProactiveDecision(False, "too_close_to_user", score)
    timestamp = local_now.timestamp()
    if timestamp < candidate.not_before:
        return ProactiveDecision(False, "too_early", score)
    if timestamp > candidate.expires_at:
        return ProactiveDecision(False, "expired", score)
    if score < 0.82:
        return ProactiveDecision(False, "score_below_threshold", score)

    sent = [datetime.fromtimestamp(float(item), tz=UTC8) for item in sent_at]
    if any(item.date() == local_now.date() for item in sent):
        return ProactiveDecision(False, "daily_limit", score)
    beginning = _week_start(local_now)
    weekly_count = sum(beginning <= item <= local_now for item in sent)
    weekly_limit = 1 if profile.proactive_mode is ProactiveMode.REDUCED else 3
    if weekly_count >= weekly_limit:
        return ProactiveDecision(False, "weekly_limit", score)
    return ProactiveDecision(True, "allowed", score)
