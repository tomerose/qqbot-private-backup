"""Pure, fail-closed policy helpers for privacy-preserving AI moderation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

CONFIDENCE_THRESHOLD = 0.90
MUTE_STEPS = (0, 60, 300, 1800)
ALLOWED_DECISIONS = {"none", "recall", "recall_and_mute"}
ALLOWED_CATEGORIES = {"spam", "advertising", "fraud", "harassment", "severe_abuse"}
ALLOWED_REASONS = {
    "repeated_spam",
    "bulk_ad",
    "fraud_lure",
    "targeted_harassment",
    "severe_personal_attack",
}

WINDOWS_PATH_RE = re.compile(
    r"(?i)(?<![a-z0-9])(?:[a-z]:[\\/]|\\\\)[^\s`\"<>|，。；！？）】},;!]+"
)
NUMERIC_ID_RE = re.compile(r"(?<!\d)\d{5,12}(?!\d)")
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|password|secret|token)"
    r"(\s*[:=]\s*)[\"']?[^\s,;\"']+[\"']?"
)
URL_RE = re.compile(r"https?://[^\s<>()，。；！？]+", re.IGNORECASE)
SUSPICIOUS_RE = re.compile(
    r"返现|扫码|点击链接|免费领取|加群|私聊|兼职|代理|博彩|稳赚|刷单|"
    r"广告|诈骗|转账|傻逼|死妈|滚开|操你|领取红包|http://|https://",
    re.IGNORECASE,
)
DEFAULT_AI_IDENTITY_TERMS = ("人工智障", "人机", "机器人", "AI")


@dataclass(frozen=True)
class ModerationDecision:
    decision: Literal["none", "recall", "recall_and_mute"]
    category: str
    confidence: float
    reason_code: str


@dataclass(frozen=True)
class ModerationAction:
    recall: bool
    mute_seconds: int
    reason_code: str


NONE_DECISION = ModerationDecision("none", "", 0.0, "none")


def parse_decision(raw: str) -> ModerationDecision:
    """Accept only the bounded JSON contract; every ambiguity becomes no action."""
    try:
        payload = json.loads(str(raw or ""))
    except (TypeError, json.JSONDecodeError):
        return NONE_DECISION
    if not isinstance(payload, dict):
        return NONE_DECISION
    decision = payload.get("decision")
    if decision == "none":
        return NONE_DECISION
    category = payload.get("category")
    reason = payload.get("reason_code")
    confidence = payload.get("confidence")
    if (
        decision not in ALLOWED_DECISIONS
        or category not in ALLOWED_CATEGORIES
        or reason not in ALLOWED_REASONS
        or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
    ):
        return NONE_DECISION
    confidence_value = float(confidence)
    if not 0 <= confidence_value <= 1 or confidence_value < CONFIDENCE_THRESHOLD:
        return NONE_DECISION
    return ModerationDecision(decision, category, confidence_value, reason)


def resolve_action(
    decision: ModerationDecision, offense_count: int
) -> ModerationAction:
    if decision.decision == "none" or decision.confidence < CONFIDENCE_THRESHOLD:
        return ModerationAction(False, 0, "none")
    index = min(max(int(offense_count), 0), len(MUTE_STEPS) - 1)
    return ModerationAction(True, MUTE_STEPS[index], decision.reason_code)


def _strip_url_metadata(match: re.Match[str]) -> str:
    try:
        split = urlsplit(match.group(0))
        return urlunsplit((split.scheme, split.netloc, split.path, "", ""))
    except ValueError:
        return "[链接]"


def sanitize_message(text: str, max_chars: int = 500) -> str:
    """Minimize one message before it can cross the model trust boundary."""
    cleaned = WINDOWS_PATH_RE.sub("[本机路径]", str(text or ""))
    cleaned = SECRET_ASSIGNMENT_RE.sub(r"\1\2[已隐藏]", cleaned)
    cleaned = NUMERIC_ID_RE.sub("[成员标识]", cleaned)
    cleaned = URL_RE.sub(_strip_url_metadata, cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()[: max(0, int(max_chars))]


def _speaker_label(index: int) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return alphabet[index] if index < len(alphabet) else f"{index + 1}"


def build_anonymous_context(
    messages: list[tuple[str, str]],
    max_messages: int = 8,
    max_chars: int = 3000,
) -> str:
    speakers: dict[str, str] = {}
    lines: list[str] = []
    remaining = max(0, int(max_chars))
    for speaker_id, text in messages[-max(0, int(max_messages)) :]:
        key = str(speaker_id)
        if key not in speakers:
            speakers[key] = _speaker_label(len(speakers))
        line = f"成员{speakers[key]}：{sanitize_message(text)}"
        if len(line) > remaining:
            line = line[:remaining]
        if not line:
            break
        lines.append(line)
        remaining -= len(line) + 1
        if remaining <= 0:
            break
    return "\n".join(lines)


def is_candidate(text: str, recent_same: int = 0) -> bool:
    """Broad local gate; this never punishes and only decides whether AI is consulted."""
    value = str(text or "").strip()
    if not value:
        return False
    if int(recent_same) >= 2:
        return True
    if SUSPICIOUS_RE.search(value):
        return True
    return bool(re.search(r"(.{2,12})\1{2,}", value))


def matches_ai_identity_attack(
    text: str, terms: tuple[str, ...] = DEFAULT_AI_IDENTITY_TERMS
) -> bool:
    """Match every literal configured term, regardless of intent or context."""
    value = str(text or "").casefold()
    return any(str(term).casefold() in value for term in terms if str(term))
