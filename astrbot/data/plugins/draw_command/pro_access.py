"""Fail-closed read-only access to approved Pro memberships."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from pathlib import Path


SIGNING_KEY_ENV = "XIAONING_PRO_SIGNING_KEY"
MIN_SIGNING_KEY_BYTES = 32


def _load_signing_key(database_path: Path) -> bytes | None:
    configured = os.environ.get(SIGNING_KEY_ENV)
    if configured is not None:
        key = configured.encode("utf-8")
        return key if len(key) >= MIN_SIGNING_KEY_BYTES else None
    key_path = Path(database_path).with_suffix(".key")
    try:
        key = key_path.read_bytes()
    except FileNotFoundError:
        generated = secrets.token_bytes(MIN_SIGNING_KEY_BYTES)
        try:
            descriptor = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                key = key_path.read_bytes()
            except OSError:
                return None
        except OSError:
            return None
        else:
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(generated)
                key = generated
            except OSError:
                return None
    except OSError:
        return None
    return key if len(key) >= MIN_SIGNING_KEY_BYTES else None


def _membership_signature(
    key: bytes, application_id: str, qq_id: str, state: str, pro_expires_at: float
) -> str:
    payload = f"{application_id}|{qq_id}|{state}|{float(pro_expires_at):.6f}".encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def is_active_pro(qq_id: object, db_path: Path, now: float | None = None) -> bool:
    identity = str(qq_id or "").strip()
    if not identity.isdigit() or not (5 <= len(identity) <= 12):
        return False
    try:
        path = Path(db_path).resolve(strict=True)
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
    except (OSError, sqlite3.Error, ValueError):
        return False
    try:
        checked_at = time.time() if now is None else float(now)
        key = _load_signing_key(path)
        if key is None:
            return False
        row = connection.execute(
            """
            SELECT application_id, qq_id, state, pro_expires_at, membership_signature FROM applications
            WHERE qq_id = ? AND state = 'active' AND pro_expires_at >= ?
            LIMIT 1
            """,
            (identity, checked_at),
        ).fetchone()
        if row is None:
            return False
        try:
            expected = _membership_signature(
                key, row["application_id"], row["qq_id"], row["state"], row["pro_expires_at"]
            )
        except (TypeError, ValueError):
            return False
        return hmac.compare_digest(str(row["membership_signature"] or ""), expected)
    except sqlite3.Error:
        return False
    finally:
        connection.close()
