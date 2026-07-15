"""Unified tier-based membership lookups. Replaces binary is_active_pro checks.

Tier flow: ORDINARY < X < PRO. Each tier inherits all lower-tier capabilities.
Ordinary: Draw 6x/week (Gemini). X: Draw 6x/week (Imagen 4), Video 3x/day, Agent 1x/week.
PRO: owner-granted, time-limited (direct grants ≤520 days), no artificial caps.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
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
    """Return the membership tier for *qq_id*. Falls back to ORDINARY.
    Uses ProClient.is_active() directly (not is_active_pro) to avoid recursion."""
    tier_raw = _get_client(Path(db_path)).active_tier(qq_id, now)
    return {"x": Tier.X, "pro": Tier.PRO}.get(tier_raw, Tier.ORDINARY)


def agent_available(qq_id: object, db_path: object) -> tuple[bool, str]:
    """Returns (available, reason). X: once per 7 days (1x/week). PRO: unlimited."""
    tier = get_tier(qq_id, db_path)
    if tier == Tier.PRO:
        return True, ""
    if tier != Tier.X:
        return False, "Agent 功能需要 X 或 PRO 权限。添加小柠为 QQ 好友即可获得 X 资格。"

    db = Path(db_path)
    try:
        with closing(sqlite3.connect(str(db.resolve(strict=True)))) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """SELECT agent_used_at FROM applications
                   WHERE qq_id = ? AND tier = 'x' AND state = 'active'
                     AND pro_expires_at >= ? LIMIT 1""",
                (str(qq_id or "").strip(), time.time()),
            ).fetchone()
    except (OSError, sqlite3.Error):
        return False, "权限查询失败，稍后再试"

    if row and row["agent_used_at"]:
        elapsed = time.time() - float(row["agent_used_at"])
        if elapsed < 7 * 86400:
            remaining = 7 * 86400 - elapsed
            days = int(remaining // 86400)
            hours = int((remaining % 86400) // 3600)
            return False, f"X 用户 Agent 每周可用 1 次。{days}天{hours}小时后刷新"
    return True, ""


def use_agent(qq_id: object, db_path: object) -> bool:
    """Atomically record one X Agent use. Return False if already consumed."""
    tier = get_tier(qq_id, db_path)
    if tier != Tier.X:
        return tier == Tier.PRO
    db = Path(db_path)
    now = time.time()
    try:
        with closing(sqlite3.connect(str(db.resolve(strict=True)))) as conn, conn:
            cursor = conn.execute(
                """UPDATE applications SET agent_used_at = ?
                   WHERE qq_id = ? AND tier = 'x' AND state = 'active'
                     AND pro_expires_at >= ?
                      AND (agent_used_at IS NULL OR agent_used_at <= ?)""",
                (now, str(qq_id or "").strip(), now, now - 7 * 86400),
            )
            return cursor.rowcount == 1
    except (OSError, sqlite3.Error):
        return False


# Backward-compatible aliases — existing plugins can migrate incrementally
def is_active_pro(qq_id: object, db_path: object, now: float | None = None) -> bool:
    """DEPRECATED: use get_tier() >= Tier.X instead."""
    return get_tier(qq_id, db_path, now) >= Tier.X


def is_active_pro_group(group_id: object, db_path: object, now: float | None = None) -> bool:
    """Return True when *group_id* is an active Pro group."""
    return _get_client(Path(db_path)).is_active_group(group_id, now=now)
