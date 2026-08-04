"""Shared lightweight relationship state for Xiaoning friend-layer plugins."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

RETURN_GAP_SECONDS = 20 * 3600
QUIET_MODE = "quiet"
NORMAL_MODE = "normal"

_QUIET_RE = re.compile(r"(?:安静一点|别主动关心|不要主动关心|少主动|别提旧事)")
_NORMAL_RE = re.compile(r"(?:恢复正常|恢复主动|正常一点|可以主动)")


def load_state(path: Path, legacy_paths: list[Path] | None = None) -> dict:
    """Load the shared state, lazily seeding from older plugin state files."""
    state = _read_json(path)
    for legacy in legacy_paths or []:
        for uid, entry in _read_json(legacy).items():
            if uid not in state and isinstance(entry, dict):
                state[uid] = {
                    "first_seen_ts": entry.get("first_seen_ts", 0),
                    "last_message_ts": entry.get("last_message_ts", 0),
                    "message_count": entry.get("message_count", 0),
                    "friend_mode": entry.get("friend_mode", NORMAL_MODE),
                }
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def record_interaction(
    state: dict, sender: str, *, now: float | None = None
) -> dict:
    """Record one direct interaction and return the updated user entry."""
    now_ts = time.time() if now is None else float(now)
    entry = state.setdefault(sender, {})
    prev_ts = float(entry.get("last_message_ts", 0) or 0)
    gap = now_ts - prev_ts if prev_ts else 0
    entry.setdefault("friend_mode", NORMAL_MODE)
    if not entry.get("first_seen_ts"):
        entry["first_seen_ts"] = now_ts
    entry["last_message_ts"] = now_ts
    entry["message_count"] = int(entry.get("message_count", 0) or 0) + 1
    if gap >= RETURN_GAP_SECONDS:
        entry["last_return_gap_hours"] = round(gap / 3600, 1)
    else:
        entry.pop("last_return_gap_hours", None)
    return entry


def get_snapshot(state: dict, sender: str, *, now: float | None = None) -> dict:
    """Return a bounded copy for prompt injection or /认识."""
    entry = dict(state.get(sender, {}) or {})
    current = time.time() if now is None else float(now)
    first_seen = float(entry.get("first_seen_ts", 0) or 0)
    entry["days_known"] = max(0, round((current - first_seen) / 86400)) if first_seen else 0
    entry["message_count"] = int(entry.get("message_count", 0) or 0)
    entry["friend_mode"] = entry.get("friend_mode") or NORMAL_MODE
    return entry


def set_friend_mode(state: dict, sender: str, mode: str) -> dict:
    """Set the user's friend-layer mode without touching permissions."""
    if mode not in {QUIET_MODE, NORMAL_MODE}:
        raise ValueError("invalid friend mode")
    entry = state.setdefault(sender, {})
    entry["friend_mode"] = mode
    entry["mode_updated_at"] = time.time()
    return entry


def can_send_proactive(
    state: dict, sender: str, cooldown_seconds: float, *, now: float | None = None
) -> bool:
    """Return whether one private proactive message may be sent now."""
    entry = get_snapshot(state, sender, now=now)
    if entry.get("friend_mode") == QUIET_MODE:
        return False
    current = time.time() if now is None else float(now)
    last_sent = float(entry.get("last_proactive_at", 0) or 0)
    return last_sent <= 0 or current - last_sent >= max(0, float(cooldown_seconds))


def record_proactive_send(state: dict, sender: str, *, now: float | None = None) -> None:
    """Record a confirmed private proactive send for the shared cooldown."""
    entry = state.setdefault(sender, {})
    entry["last_proactive_at"] = time.time() if now is None else float(now)


def parse_friend_mode(text: object) -> str | None:
    value = str(text or "").strip()
    if _QUIET_RE.search(value):
        return QUIET_MODE
    if _NORMAL_RE.search(value):
        return NORMAL_MODE
    return None


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
