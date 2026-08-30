"""Immutable ordinary/Pro capability policy for the QQ local Agent."""

from __future__ import annotations

import re
from enum import Enum
from typing import Iterable


_QQ_ID = re.compile(r"^[1-9]\d{4,11}$")


class AccessTier(str, Enum):
    ORDINARY = "ordinary"
    PRO = "pro"


class Capability(str, Enum):
    CHAT = "chat"
    VOICE = "voice"
    LOCAL_AGENT = "local_agent"
    LOCAL_FILE = "local_file"
    TASK_CONTROL = "task_control"


_ORDINARY_CAPABILITIES = frozenset({Capability.CHAT, Capability.VOICE})


def parse_pro_user_ids(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        candidates = re.split(r"[\s,;]+", value)
    elif isinstance(value, Iterable):
        candidates = [str(item) for item in value]
    else:
        candidates = []
    return tuple(
        dict.fromkeys(
            candidate.strip()
            for candidate in candidates
            if _QQ_ID.fullmatch(candidate.strip())
        )
    )


class AccessPolicy:
    def __init__(self, pro_user_ids: object):
        self.pro_user_ids = frozenset(parse_pro_user_ids(pro_user_ids))

    def resolve_tier(self, sender_id: object) -> AccessTier:
        normalized = str(sender_id or "").strip()
        return AccessTier.PRO if normalized in self.pro_user_ids else AccessTier.ORDINARY

    def authorize(self, sender_id: object, capability: Capability) -> bool:
        tier = self.resolve_tier(sender_id)
        return tier is AccessTier.PRO or capability in _ORDINARY_CAPABILITIES
