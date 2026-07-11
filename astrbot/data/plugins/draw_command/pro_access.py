"""Fail-closed read-only access to approved Pro memberships."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path


def is_active_pro(qq_id: object, db_path: Path, now: float | None = None) -> bool:
    identity = str(qq_id or "").strip()
    if not identity.isdigit() or not (5 <= len(identity) <= 12):
        return False
    try:
        path = Path(db_path).resolve(strict=True)
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    except (OSError, sqlite3.Error, ValueError):
        return False
    try:
        checked_at = time.time() if now is None else float(now)
        row = connection.execute(
            """
            SELECT 1 FROM applications
            WHERE qq_id = ? AND state = 'active' AND pro_expires_at >= ?
            LIMIT 1
            """,
            (identity, checked_at),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False
    finally:
        connection.close()
