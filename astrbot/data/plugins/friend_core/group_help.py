"""High-confidence, low-noise group-help opportunities."""

from __future__ import annotations

import re
from dataclasses import dataclass
import json

try:
    from xiaoning_capabilities import Capability, match_capability
except ImportError:
    from data.plugins.xiaoning_capabilities import Capability, match_capability


_HELP_SIGNAL = re.compile(
    r"(?:求助|救命|谁(?:会|知道|懂|能)|有没有人|能不能(?:帮|看)|帮(?:我|忙)|"
    r"需要(?:一份|一个|有人)|想要(?:一份|一个)|怎么(?:办|做|弄|解决|处理)|"
    r"如何(?:做|弄|解决|处理))",
    re.I,
)
_STRONG_HELP_SIGNAL = re.compile(
    r"求助|救命|谁(?:会|知道|懂|能)|有没有人|帮(?:我|忙)|能不能帮", re.I
)
_SENSITIVE = re.compile(
    r"密码|验证码|身份证|银行卡|住址|地址|手机号|聊天记录|通讯录|隐私", re.I
)


@dataclass(frozen=True)
class GroupHelpDecision:
    capability: Capability
    confidence: float

    @property
    def offer(self) -> str:
        return self.capability.offer


def screen_group_help(text: object) -> GroupHelpDecision | None:
    """Cheap first pass; ambiguous candidates require model confirmation."""
    message = str(text or "").strip()
    if not message or _SENSITIVE.search(message) or not _HELP_SIGNAL.search(message):
        return None
    capability = match_capability(message, proactive_only=True)
    if capability is None:
        return None
    confidence = 0.96 if _STRONG_HELP_SIGNAL.search(message) else 0.72
    return GroupHelpDecision(capability, confidence)


def group_help_offer(text: object) -> str | None:
    """Return one useful offer only for an unambiguous public request."""
    decision = screen_group_help(text)
    return decision.offer if decision and decision.confidence >= 0.92 else None


def parse_group_help_confirmation(
    raw: object, expected_capability: str
) -> GroupHelpDecision | None:
    """Accept only the exact bounded classifier contract."""
    try:
        data = json.loads(str(raw or ""))
        capability_id = str(data.get("capability_id") or "")
        confidence = float(data.get("confidence", 0))
        requested = data.get("help_requested") is True
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not requested or capability_id != expected_capability or not 0 <= confidence <= 1:
        return None
    try:
        from xiaoning_capabilities import CAPABILITY_BY_ID
    except ImportError:
        from data.plugins.xiaoning_capabilities import CAPABILITY_BY_ID
    capability = CAPABILITY_BY_ID.get(capability_id)
    if capability is None or confidence < 0.92:
        return None
    return GroupHelpDecision(capability, confidence)
