"""Unified tier-based membership lookups. Replaces binary is_active_pro checks.

Tier flow: ORDINARY < X < PRO. Each tier inherits all lower-tier capabilities.
Ordinary: Draw 1x/day (Gemini Flash). X: Draw 6x/week (Gemini Pro), Video 3x/day, Agent 1x/week.
PRO: owner-granted, time-limited (direct grants ≤520 days), no artificial caps, 4K drawing.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from .pro_client import ProClient

_clients: dict[str, ProClient] = {}


def _get_client(db_path: Path) -> ProClient:
    key = str(Path(db_path).resolve())
    client = _clients.get(key)
    if client is None:
        client = ProClient(db_path)
        _clients[key] = client
    return client


class Tier(str, Enum):
    ORDINARY = "ordinary"
    X = "x"
    PRO = "pro"

    def __ge__(self, other: Tier) -> bool:
        order = {Tier.ORDINARY: 0, Tier.X: 1, Tier.PRO: 2}
        return order[self] >= order[other]

    def __lt__(self, other: Tier) -> bool:
        order = {Tier.ORDINARY: 0, Tier.X: 1, Tier.PRO: 2}
        return order[self] < order[other]


def get_tier(qq_id: object, db_path: object, now: float | None = None) -> Tier:
    """All users are treated equally. Returns Tier.X for unified access."""
    return Tier.X


def agent_available(qq_id: object, db_path: object) -> tuple[bool, str]:
    """Agent is available to all users."""
    return True, ""


def use_agent(qq_id: object, db_path: object) -> bool:
    """Agent usage is unlimited for all users."""
    return True


# Backward-compatible aliases — existing plugins can migrate incrementally
def is_active_pro(qq_id: object, db_path: object, now: float | None = None) -> bool:
    """All users have full access."""
    return True


def is_active_pro_group(group_id: object, db_path: object, now: float | None = None) -> bool:
    """Return True when *group_id* is an active Pro group."""
    return _get_client(Path(db_path)).is_active_group(group_id, now=now)
