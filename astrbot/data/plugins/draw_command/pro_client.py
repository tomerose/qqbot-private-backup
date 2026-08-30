"""Shared read-only Pro membership check with cached signing key.

Single source of truth for signing-key loading and HMAC-verified membership
lookups, used by draw_command (per-message check), pro_access (library shim),
pro_application.ProStore (delegates key loading), and claude_code_agent (via
pro_access.is_active_pro).
"""

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
KEY_CACHE_TTL_SECONDS = 1800  # 30 min


def load_signing_key(database_path: object) -> bytes | None:
    """Read or generate the per-database HMAC signing key.

    Callable as a module-level function so ProStore can delegate without
    constructing a ProClient (which is read-only by design).
    """
    path = Path(database_path)
    configured = os.environ.get(SIGNING_KEY_ENV)
    if configured is not None:
        key = configured.encode("utf-8")
        return key if len(key) >= MIN_SIGNING_KEY_BYTES else None

    key_path = path.with_suffix(".key")
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
    key: bytes,
    application_id: str,
    qq_id: str,
    state: str,
    pro_expires_at: float,
    tier: str,
) -> str:
    payload = (
        f"{application_id}|{qq_id}|{state}|{float(pro_expires_at):.6f}|{tier}"
    ).encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


class ProClient:
    """Cached-key read-only Pro membership lookups.

    Key is cached (30 min TTL); connection is opened per-call so non-existent
    DB files fail closed and callers never hold a stale handle.
    """

    def __init__(self, db_path: object) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._key: bytes | None = None
        self._key_loaded_at: float = 0.0
        self._key_mtime: float = 0.0

    @property
    def signing_key(self) -> bytes | None:
        now = time.monotonic()
        key_path = self._db_path.with_suffix(".key")
        try:
            disk_mtime = key_path.stat().st_mtime
        except FileNotFoundError:
            disk_mtime = 0.0
        stale_by_mtime = disk_mtime > self._key_mtime
        stale_by_ttl = (now - self._key_loaded_at) >= KEY_CACHE_TTL_SECONDS
        if self._key is not None and not stale_by_mtime and not stale_by_ttl:
            return self._key
        self._key = load_signing_key(self._db_path)
        self._key_loaded_at = now
        try:
            self._key_mtime = key_path.stat().st_mtime
        except FileNotFoundError:
            self._key_mtime = disk_mtime
        return self._key

    def is_active(self, qq_id: object, now: float | None = None) -> bool:
        """Return True when *qq_id* holds a valid, unexpired, signed Pro membership."""
        return self.active_tier(qq_id, now) is not None

    def active_tier(self, qq_id: object, now: float | None = None) -> str | None:
        """Return the tier from the exact HMAC-verified active row."""
        identity = str(qq_id or "").strip()
        if not identity.isdigit() or not (5 <= len(identity) <= 12):
            return None

        key = self.signing_key
        if key is None:
            return None

        checked_at = time.time() if now is None else float(now)
        try:
            conn = sqlite3.connect(
                f"{self._db_path.resolve(strict=True).as_uri()}?mode=ro", uri=True,
            )
            conn.row_factory = sqlite3.Row
        except OSError:
            return None

        try:
            row = conn.execute(
                """SELECT application_id, qq_id, state, pro_expires_at, membership_signature, tier
                   FROM applications
                   WHERE qq_id = ? AND state = 'active' AND pro_expires_at >= ?
                   LIMIT 1""",
                (identity, checked_at),
            ).fetchone()
        except sqlite3.Error:
            conn.close()
            return None

        if row is None:
            conn.close()
            return None

        try:
            expected = _membership_signature(
                key,
                row["application_id"],
                row["qq_id"],
                row["state"],
                row["pro_expires_at"],
                row["tier"],
            )
            if not hmac.compare_digest(str(row["membership_signature"] or ""), expected):
                return None
            tier = str(row["tier"] or "").strip().lower()
            return tier if tier in {"x", "pro"} else None
        except (KeyError, TypeError, ValueError):
            return None
        finally:
            conn.close()

    def is_active_group(self, group_id: object, now: float | None = None) -> bool:
        """Return True when *group_id* is an active Pro group."""
        gid = str(group_id or "").strip()
        if not gid.isdigit():
            return False
        key = self.signing_key
        if key is None:
            return False
        checked_at = time.time() if now is None else float(now)
        try:
            conn = sqlite3.connect(
                f"{self._db_path.resolve(strict=True).as_uri()}?mode=ro", uri=True,
            )
            conn.row_factory = sqlite3.Row
        except OSError:
            return False
        try:
            row = conn.execute(
                """SELECT group_id, activated_by, activated_at, signature
                   FROM pro_groups
                   WHERE group_id = ? AND deactivated_at IS NULL""",
                (gid,),
            ).fetchone()
        except sqlite3.Error:
            conn.close()
            return False
        if row is None:
            conn.close()
            return False
        try:
            payload = f"group:{row['group_id']}|{row['activated_by']}|{float(row['activated_at']):.6f}".encode("utf-8")
            expected = hmac.new(key, payload, hashlib.sha256).hexdigest()
            return hmac.compare_digest(str(row["signature"] or ""), expected)
        except (KeyError, TypeError, ValueError):
            return False
        finally:
            conn.close()
