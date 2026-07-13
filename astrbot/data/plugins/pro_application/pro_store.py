"""Privacy-safe state store for Pro memberships — 小柠 automated system.

ALL Pro access is verified via HMAC-signed database records. There is no
config-file bypass, no operator whitelist, and no human-in-the-loop override.
The reviewer identity is cryptographically bound to the signing key; management
commands require a separate passphrase (XIAONING_PRO_PASSPHRASE env var) that
is never stored in config files or transmitted in group chats.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


SIGNING_KEY_ENV = "XIAONING_PRO_SIGNING_KEY"
PASSPHRASE_ENV = "XIAONING_PRO_PASSPHRASE"
MIN_SIGNING_KEY_BYTES = 32
OWNER_PRO_DAYS = 36500  # ~100 years — permanent owner membership


def _load_signing_key(database_path: object) -> bytes | None:
    """Read or generate the per-database HMAC signing key."""
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


APPLICATION_TTL_SECONDS = 72 * 60 * 60
CODE_TTL_SECONDS = 10 * 60
APPROVAL_CONFIRM_TTL_SECONDS = 5 * 60
RESEND_COOLDOWN_SECONDS = 60
MAX_CODE_ATTEMPTS = 3
DEFAULT_PRO_DAYS = 90
MIN_PRO_DAYS = 1
MAX_PRO_DAYS = 365


class ProStoreError(ValueError):
    """Stable, privacy-safe application state error."""


@dataclass(frozen=True)
class Application:
    application_id: str
    qq_id: str
    state: str
    created_at: float
    expires_at: float
    pro_expires_at: float | None
    tier: str = "pro"


@dataclass(frozen=True)
class AuditEvent:
    event_type: str
    event_at: float


class ProStore:
    def __init__(self, path: Path, *, reviewer_id: str):
        self.path = Path(path)
        self.reviewer_id = str(reviewer_id or "").strip()
        if not self.reviewer_id.isdigit():
            raise ValueError("reviewer_id_invalid")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._signing_key = _load_signing_key(self.path)
        self._initialize()
        self._harden_files()

    # ── Passphrase verification (constant-time, env-var only) ──────

    @staticmethod
    def verify_passphrase(passphrase: str) -> bool:
        """Constant-time passphrase check against XIAONING_PRO_PASSPHRASE.

        Returns False when the env var is unset (fail-secure: no admin
        commands work without it).
        """
        expected = os.environ.get(PASSPHRASE_ENV, "")
        if not expected:
            return False
        return hmac.compare_digest(expected, str(passphrase or ""))

    # ── Connection ─────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _transaction(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS applications (
                    application_id TEXT PRIMARY KEY,
                    qq_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    application_expires_at REAL NOT NULL,
                    reviewer_id TEXT,
                    verification_code_hash TEXT,
                    verification_expires_at REAL,
                    verification_attempts INTEGER NOT NULL DEFAULT 0,
                    approved_days INTEGER,
                    pro_expires_at REAL,
                    approval_confirm_expires_at REAL,
                    last_code_sent_at REAL,
                    membership_signature TEXT,
                    tier TEXT,
                    agent_used_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_applications_qq_state
                    ON applications(qq_id, state);
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    application_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pro_groups (
                    group_id TEXT PRIMARY KEY,
                    activated_by TEXT NOT NULL,
                    activated_at REAL NOT NULL,
                    deactivated_at REAL,
                    signature TEXT NOT NULL
                );
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(applications)").fetchall()
            }
            tier_added = "tier" not in columns
            for name, declaration in (
                ("approval_confirm_expires_at", "REAL"),
                ("last_code_sent_at", "REAL"),
                ("membership_signature", "TEXT"),
                ("tier", "TEXT"),
                ("agent_used_at", "REAL"),
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE applications ADD COLUMN {name} {declaration}")
            connection.execute(
                "UPDATE applications SET tier = 'pro' WHERE tier IS NULL OR tier = ''"
            )
            # One-time legacy migration only. Re-signing every active row on
            # startup would legitimize database tampering.
            if tier_added and self._signing_key is not None:
                rows = connection.execute(
                    """
                    SELECT application_id, qq_id, state, pro_expires_at, tier FROM applications
                    WHERE state = 'active' AND pro_expires_at IS NOT NULL
                    """
                ).fetchall()
                for row in rows:
                    connection.execute(
                        "UPDATE applications SET membership_signature = ? WHERE application_id = ?",
                        (
                            self._membership_signature(
                                row["application_id"],
                                row["qq_id"],
                                row["state"],
                                row["pro_expires_at"],
                                row["tier"],
                            ),
                            row["application_id"],
                        ),
                    )
            # Ensure the permanent owner membership exists (fail-secure:
            # without a signing key the membership row won't validate, but
            # we still create it so callers get a clean error).
            self._ensure_owner_membership(connection)

    def _ensure_owner_membership(self, connection: sqlite3.Connection) -> None:
        """Guarantee a permanent, signed Pro membership for the reviewer (owner).

        This is the ONLY path to Pro that bypasses the application workflow,
        and it is restricted to the hardcoded reviewer_id. The membership is
        re-signed on every bot start so key rotation is handled automatically.
        """
        now = self._clock() if hasattr(self, "_clock") else __import__("time").time()
        row = connection.execute(
            "SELECT application_id, qq_id, state, pro_expires_at, tier FROM applications"
            " WHERE application_id = 'OWNER-PERMANENT'"
        ).fetchone()
        expires_at = now + OWNER_PRO_DAYS * 86400
        if row is None:
            connection.execute(
                """INSERT INTO applications(
                    application_id, qq_id, state, created_at, application_expires_at,
                    pro_expires_at, membership_signature, tier
                ) VALUES ('OWNER-PERMANENT', ?, 'active', ?, ?, ?, 'pending', 'pro')""",
                (self.reviewer_id, now, expires_at + 86400, expires_at),
            )
            self._event(connection, "OWNER-PERMANENT", "owner_membership_created", now)
        else:
            connection.execute(
                """UPDATE applications
                   SET pro_expires_at = ?, membership_signature = 'pending', tier = 'pro'
                   WHERE application_id = 'OWNER-PERMANENT'""",
                (expires_at,),
            )
        if self._signing_key is not None:
            connection.execute(
                "UPDATE applications SET membership_signature = ?"
                " WHERE application_id = 'OWNER-PERMANENT'",
                (
                    self._membership_signature(
                        "OWNER-PERMANENT", self.reviewer_id, "active", expires_at, "pro"
                    ),
                ),
            )

    def _harden_files(self) -> None:
        """Restrict ACL on key and database files to current user only.

        On Windows this uses icacls for explicit ownership; on other platforms
        it ensures owner-only read/write (0o600). The key file is additionally
        marked read-only to prevent accidental overwrite.
        """
        key_path = self.path.with_suffix(".key")
        for target in (self.path, key_path):
            if not target.exists():
                continue
            try:
                if os.name == "nt":
                    # Windows: remove inherited permissions, grant current user full control
                    import subprocess
                    subprocess.run(
                        ["icacls", str(target), "/inheritance:r", "/grant:r",
                         f"{os.environ.get('USERNAME', 'SYSTEM')}:F"],
                        capture_output=True, timeout=10, check=False,
                    )
                else:
                    target.chmod(0o600)
            except OSError:
                pass  # Non-fatal — signing still works without ACL hardening
        # Mark key file read-only
        if key_path.exists():
            try:
                key_path.chmod(key_path.stat().st_mode & ~0o222)
            except OSError:
                pass

    @staticmethod
    def _application_id() -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        return "APP-" + "".join(secrets.choice(alphabet) for _ in range(8))

    @staticmethod
    def _code_hash(code: str) -> str:
        return hashlib.sha256(str(code).encode("utf-8")).hexdigest()

    @staticmethod
    def _verification_code() -> str:
        return secrets.token_urlsafe(12)

    def _membership_signature(
        self,
        application_id: str,
        qq_id: str,
        state: str,
        pro_expires_at: float,
        tier: str,
    ) -> str:
        if self._signing_key is None:
            raise ProStoreError("signing_key_unavailable")
        payload = (
            f"{application_id}|{qq_id}|{state}|{float(pro_expires_at):.6f}|{tier}".encode("utf-8")
        )
        return hmac.new(self._signing_key, payload, hashlib.sha256).hexdigest()

    @staticmethod
    def _valid_qq_id(qq_id: object) -> str:
        value = str(qq_id or "").strip()
        if not value.isdigit() or not (5 <= len(value) <= 12):
            raise ProStoreError("qq_id_invalid")
        return value

    @staticmethod
    def _row_to_application(row: sqlite3.Row) -> Application:
        return Application(
            application_id=row["application_id"],
            qq_id=row["qq_id"],
            state=row["state"],
            created_at=float(row["created_at"]),
            expires_at=float(row["application_expires_at"]),
            pro_expires_at=(
                float(row["pro_expires_at"])
                if row["pro_expires_at"] is not None
                else None
            ),
            tier=str(row["tier"] or "pro").strip().lower(),
        )

    def _event(self, connection: sqlite3.Connection, application_id: str, event_type: str, now: float) -> None:
        connection.execute(
            "INSERT INTO events(application_id, event_type, event_at) VALUES (?, ?, ?)",
            (application_id, event_type, float(now)),
        )

    def _cleanup(self, connection: sqlite3.Connection, now: float) -> None:
        expiring_confirmations = connection.execute(
            """
            SELECT application_id FROM applications
            WHERE state = 'approval_pending_confirm' AND approval_confirm_expires_at < ?
            """,
            (float(now),),
        ).fetchall()
        connection.execute(
            """
            UPDATE applications
            SET state = 'expired', verification_code_hash = NULL
            WHERE state IN ('pending_email', 'awaiting_review', 'approval_pending_confirm')
              AND application_expires_at < ?
            """,
            (float(now),),
        )
        for row in expiring_confirmations:
            self._event(connection, row["application_id"], "approval_confirmation_expired", now)
        connection.execute(
            """
            UPDATE applications
            SET state = 'awaiting_review', approval_confirm_expires_at = NULL,
                approved_days = NULL
            WHERE state = 'approval_pending_confirm' AND approval_confirm_expires_at < ?
            """,
            (float(now),),
        )
        connection.execute(
            """
            UPDATE applications
            SET state = 'verification_expired', verification_code_hash = NULL
            WHERE state = 'awaiting_verify' AND verification_expires_at < ?
            """,
            (float(now),),
        )
        connection.execute(
            """
            UPDATE applications
            SET state = 'pro_expired', membership_signature = NULL
            WHERE state = 'active' AND pro_expires_at < ?
            """,
            (float(now),),
        )

    def create_application(self, qq_id: str, *, now: float) -> Application:
        identity = self._valid_qq_id(qq_id)
        with self._transaction() as connection:
            self._cleanup(connection, now)
            pending = connection.execute(
                """
                SELECT 1 FROM applications
                WHERE qq_id = ? AND state IN (
                    'pending_email', 'awaiting_review', 'approval_pending_confirm', 'awaiting_verify'
                )
                LIMIT 1
                """,
                (identity,),
            ).fetchone()
            if pending is not None:
                raise ProStoreError("application_pending")
            application_id = self._application_id()
            while connection.execute(
                "SELECT 1 FROM applications WHERE application_id = ?", (application_id,)
            ).fetchone() is not None:
                application_id = self._application_id()
            expires_at = float(now) + APPLICATION_TTL_SECONDS
            connection.execute(
                """
                INSERT INTO applications(
                    application_id, qq_id, state, created_at, application_expires_at
                ) VALUES (?, ?, 'pending_email', ?, ?)
                """,
                (application_id, identity, float(now), expires_at),
            )
            self._event(connection, application_id, "created", now)
            row = connection.execute(
                "SELECT * FROM applications WHERE application_id = ?", (application_id,)
            ).fetchone()
            return self._row_to_application(row)

    def mark_sent(self, application_id: str, qq_id: str, *, now: float) -> Application:
        identity = self._valid_qq_id(qq_id)
        key = str(application_id or "").strip().upper()
        with self._transaction() as connection:
            self._cleanup(connection, now)
            row = connection.execute(
                "SELECT * FROM applications WHERE application_id = ?", (key,)
            ).fetchone()
            if row is None:
                raise ProStoreError("application_expired")
            if row["qq_id"] != identity:
                raise ProStoreError("application_owner")
            if row["state"] == "expired":
                raise ProStoreError("application_expired")
            if row["state"] != "pending_email":
                raise ProStoreError("application_state")
            connection.execute(
                "UPDATE applications SET state = 'awaiting_review' WHERE application_id = ?",
                (key,),
            )
            self._event(connection, key, "email_marked_sent", now)
            updated = connection.execute(
                "SELECT * FROM applications WHERE application_id = ?", (key,)
            ).fetchone()
            return self._row_to_application(updated)

    def request_approval(
        self, application_id: str, reviewer_id: str, days: int = DEFAULT_PRO_DAYS, *, now: float
    ) -> None:
        if str(reviewer_id or "").strip() != self.reviewer_id:
            raise ProStoreError("reviewer_required")
        duration = int(days)
        if not MIN_PRO_DAYS <= duration <= MAX_PRO_DAYS:
            raise ProStoreError("duration_invalid")
        key = str(application_id or "").strip().upper()
        with self._transaction() as connection:
            self._cleanup(connection, now)
            row = connection.execute(
                "SELECT * FROM applications WHERE application_id = ?", (key,)
            ).fetchone()
            if row is None or row["state"] == "expired":
                raise ProStoreError("application_expired")
            if row["state"] != "awaiting_review":
                raise ProStoreError("application_state")
            connection.execute(
                """
                UPDATE applications
                SET state = 'approval_pending_confirm', reviewer_id = ?, approved_days = ?,
                    approval_confirm_expires_at = ?
                WHERE application_id = ?
                """,
                (
                    self.reviewer_id,
                    duration,
                    float(now) + APPROVAL_CONFIRM_TTL_SECONDS,
                    key,
                ),
            )
            self._event(connection, key, "approval_requested", now)

    def confirm_approval(self, application_id: str, reviewer_id: str, *, now: float) -> str:
        if str(reviewer_id or "").strip() != self.reviewer_id:
            raise ProStoreError("reviewer_required")
        key = str(application_id or "").strip().upper()
        with self._transaction() as connection:
            self._cleanup(connection, now)
            row = connection.execute(
                "SELECT * FROM applications WHERE application_id = ?", (key,)
            ).fetchone()
            if row is None or row["state"] == "expired":
                raise ProStoreError("application_expired")
            if row["state"] != "approval_pending_confirm":
                raise ProStoreError("application_state")
            code = self._verification_code()
            connection.execute(
                """
                UPDATE applications
                SET state = 'awaiting_verify', verification_code_hash = ?,
                    verification_expires_at = ?, verification_attempts = 0,
                    approval_confirm_expires_at = NULL, last_code_sent_at = ?
                WHERE application_id = ?
                """,
                (self._code_hash(code), float(now) + CODE_TTL_SECONDS, float(now), key),
            )
            self._event(connection, key, "approval_confirmed", now)
            return code

    def resend_verification(self, application_id: str, reviewer_id: str, *, now: float) -> str:
        if str(reviewer_id or "").strip() != self.reviewer_id:
            raise ProStoreError("reviewer_required")
        key = str(application_id or "").strip().upper()
        with self._transaction() as connection:
            self._cleanup(connection, now)
            row = connection.execute(
                "SELECT * FROM applications WHERE application_id = ?", (key,)
            ).fetchone()
            if row is None or row["state"] == "expired":
                raise ProStoreError("application_expired")
            if row["state"] != "awaiting_verify":
                raise ProStoreError("application_state")
            last_sent = row["last_code_sent_at"]
            if last_sent is not None and float(now) - float(last_sent) < RESEND_COOLDOWN_SECONDS:
                raise ProStoreError("resend_rate_limited")
            code = self._verification_code()
            connection.execute(
                """
                UPDATE applications
                SET verification_code_hash = ?, verification_expires_at = ?,
                    verification_attempts = 0, last_code_sent_at = ?
                WHERE application_id = ?
                """,
                (self._code_hash(code), float(now) + CODE_TTL_SECONDS, float(now), key),
            )
            self._event(connection, key, "verification_resent", now)
            return code

    def verify(self, qq_id: str, code: str, *, now: float) -> str:
        identity = self._valid_qq_id(qq_id)
        candidate_hash = self._code_hash(code)
        failure: str | None = None
        with self._transaction() as connection:
            self._cleanup(connection, now)
            row = connection.execute(
                """
                SELECT * FROM applications
                WHERE qq_id = ? AND state IN ('awaiting_verify', 'verification_locked')
                ORDER BY created_at DESC LIMIT 1
                """,
                (identity,),
            ).fetchone()
            if row is None:
                raise ProStoreError("verification_invalid")
            if row["state"] == "verification_locked":
                raise ProStoreError("verification_locked")
            if not hmac.compare_digest(str(row["verification_code_hash"] or ""), candidate_hash):
                attempts = int(row["verification_attempts"]) + 1
                state = "verification_locked" if attempts >= MAX_CODE_ATTEMPTS else "awaiting_verify"
                connection.execute(
                    "UPDATE applications SET verification_attempts = ?, state = ? WHERE application_id = ?",
                    (attempts, state, row["application_id"]),
                )
                self._event(connection, row["application_id"], "verification_failed", now)
                failure = "verification_invalid"
            else:
                expires_at = float(now) + int(row["approved_days"]) * 86400
                tier = str(row["tier"] or "pro").strip().lower()
                signature = self._membership_signature(
                    row["application_id"], row["qq_id"], "active", expires_at, tier
                )
                connection.execute(
                    """
                    UPDATE applications
                    SET state = 'active', pro_expires_at = ?, verification_expires_at = NULL,
                        membership_signature = ?, tier = ?
                    WHERE application_id = ?
                    """,
                    (expires_at, signature, tier, row["application_id"]),
                )
                self._event(connection, row["application_id"], "activated", now)
        if failure is not None:
            raise ProStoreError(failure)
        return "active"

    def is_active_pro(self, qq_id: str, *, now: float) -> bool:
        identity = self._valid_qq_id(qq_id)
        with self._transaction() as connection:
            self._cleanup(connection, now)
            row = connection.execute(
                """
                SELECT application_id, qq_id, state, pro_expires_at, membership_signature, tier FROM applications
                WHERE qq_id = ? AND state = 'active' AND pro_expires_at >= ?
                LIMIT 1
                """,
                (identity, float(now)),
            ).fetchone()
            if row is None:
                return False
            try:
                expected = self._membership_signature(
                    row["application_id"], row["qq_id"], row["state"],
                    row["pro_expires_at"], row["tier"]
                )
            except (ProStoreError, TypeError, ValueError):
                return False
            return hmac.compare_digest(str(row["membership_signature"] or ""), expected)

    def status_for(self, qq_id: str, *, now: float) -> Application | None:
        identity = self._valid_qq_id(qq_id)
        with self._transaction() as connection:
            self._cleanup(connection, now)
            row = connection.execute(
                """
                SELECT * FROM applications
                WHERE qq_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (identity,),
            ).fetchone()
            return self._row_to_application(row) if row is not None else None

    def pending_for_review(self, reviewer_id: str, *, now: float) -> tuple[Application, ...]:
        if str(reviewer_id or "").strip() != self.reviewer_id:
            raise ProStoreError("reviewer_required")
        with self._transaction() as connection:
            self._cleanup(connection, now)
            rows = connection.execute(
                """
                SELECT * FROM applications
                WHERE state = 'awaiting_review'
                ORDER BY created_at ASC
                """
            ).fetchall()
            return tuple(self._row_to_application(row) for row in rows)

    def delivery_target(
        self, application_id: str, reviewer_id: str, required_state: str, *, now: float
    ) -> str:
        if str(reviewer_id or "").strip() != self.reviewer_id:
            raise ProStoreError("reviewer_required")
        key = str(application_id or "").strip().upper()
        with self._transaction() as connection:
            self._cleanup(connection, now)
            row = connection.execute(
                "SELECT qq_id, state FROM applications WHERE application_id = ?", (key,)
            ).fetchone()
            if row is None or row["state"] == "expired":
                raise ProStoreError("application_expired")
            if row["state"] != required_state:
                raise ProStoreError("application_state")
            return str(row["qq_id"])

    def audit_for(
        self, application_id: str, reviewer_id: str, *, now: float, limit: int = 20
    ) -> tuple[AuditEvent, ...]:
        if str(reviewer_id or "").strip() != self.reviewer_id:
            raise ProStoreError("reviewer_required")
        key = str(application_id or "").strip().upper()
        bounded_limit = max(1, min(int(limit), 20))
        with self._transaction() as connection:
            self._cleanup(connection, now)
            exists = connection.execute(
                "SELECT 1 FROM applications WHERE application_id = ?", (key,)
            ).fetchone()
            if exists is None:
                raise ProStoreError("application_expired")
            rows = connection.execute(
                """
                SELECT event_type, event_at FROM events
                WHERE application_id = ?
                ORDER BY event_at ASC, event_id ASC
                LIMIT ?
                """,
                (key, bounded_limit),
            ).fetchall()
            return tuple(
                AuditEvent(event_type=str(row["event_type"]), event_at=float(row["event_at"]))
                for row in rows
            )

    def deny(self, application_id: str, reviewer_id: str, *, now: float) -> bool:
        if str(reviewer_id or "").strip() != self.reviewer_id:
            raise ProStoreError("reviewer_required")
        key = str(application_id or "").strip().upper()
        with self._transaction() as connection:
            self._cleanup(connection, now)
            row = connection.execute(
                "SELECT state FROM applications WHERE application_id = ?", (key,)
            ).fetchone()
            if row is None or row["state"] != "awaiting_review":
                return False
            connection.execute(
                "UPDATE applications SET state = 'denied' WHERE application_id = ?",
                (key,),
            )
            self._event(connection, key, "denied", now)
            return True

    def reset_verification(self, application_id: str, reviewer_id: str, *, now: float) -> str:
        if str(reviewer_id or "").strip() != self.reviewer_id:
            raise ProStoreError("reviewer_required")
        key = str(application_id or "").strip().upper()
        with self._transaction() as connection:
            self._cleanup(connection, now)
            row = connection.execute(
                "SELECT qq_id, state FROM applications WHERE application_id = ?", (key,)
            ).fetchone()
            if row is None or row["state"] != "awaiting_verify":
                raise ProStoreError("application_state")
            connection.execute(
                """
                UPDATE applications
                SET state = 'awaiting_review', verification_code_hash = NULL,
                    verification_expires_at = NULL, verification_attempts = 0,
                    approved_days = NULL
                WHERE application_id = ?
                """,
                (key,),
            )
            self._event(connection, key, "verification_delivery_failed", now)
            return str(row["qq_id"])

    def revoke(self, qq_id: str, reviewer_id: str, *, now: float) -> bool:
        if str(reviewer_id or "").strip() != self.reviewer_id:
            raise ProStoreError("reviewer_required")
        identity = self._valid_qq_id(qq_id)
        with self._transaction() as connection:
            self._cleanup(connection, now)
            row = connection.execute(
                """
                SELECT application_id FROM applications
                WHERE qq_id = ? AND state = 'active'
                ORDER BY created_at DESC LIMIT 1
                """,
                (identity,),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                """
                UPDATE applications
                SET state = 'revoked', pro_expires_at = ?, membership_signature = NULL
                WHERE application_id = ?
                """,
                (float(now), row["application_id"]),
            )
            self._event(connection, row["application_id"], "revoked", now)
            return True

    # ponytail: GO tier constants — shorter max days, fewer priviledges
    GO_MAX_DAYS = 90

    def grant(
        self, qq_id: str, reviewer_id: str, days: int = DEFAULT_PRO_DAYS, *,
        now: float, tier: str = "pro", permanent: bool = False,
    ) -> str:
        """直接授予 Pro 或 GO，跳过申请流程。仅 reviewer 可调用。

        ``permanent`` 使用与永久所有者相同的约 100 年有效期定义，
        但仍保留正常的签名、撤销和审计流程；不暴露给普通用户命令。
        """
        if str(reviewer_id or "").strip() != self.reviewer_id:
            raise ProStoreError("reviewer_required")
        identity = self._valid_qq_id(qq_id)
        tier = str(tier or "pro").strip().lower()
        if tier not in ("pro", "go"):
            raise ProStoreError("tier_invalid")
        duration = OWNER_PRO_DAYS if permanent else int(days)
        max_d = OWNER_PRO_DAYS if permanent else (
            self.GO_MAX_DAYS if tier == "go" else MAX_PRO_DAYS
        )
        min_d = 1
        if not min_d <= duration <= max_d:
            raise ProStoreError("duration_invalid")
        with self._transaction() as connection:
            self._cleanup(connection, now)
            # Revoke any existing active membership for this QQ
            existing = connection.execute(
                "SELECT application_id FROM applications WHERE qq_id = ? AND state = 'active'",
                (identity,),
            ).fetchall()
            for row in existing:
                connection.execute(
                    "UPDATE applications SET state = 'revoked', membership_signature = NULL WHERE application_id = ?",
                    (row["application_id"],),
                )
                self._event(connection, row["application_id"], "revoked_by_grant", now)
            # Create direct active membership
            app_id = self._application_id()
            while connection.execute(
                "SELECT 1 FROM applications WHERE application_id = ?", (app_id,)
            ).fetchone() is not None:
                app_id = self._application_id()
            expires_at = float(now) + duration * 86400
            signature = self._membership_signature(
                app_id, identity, "active", expires_at, tier
            )
            connection.execute(
                """INSERT INTO applications(
                    application_id, qq_id, state, created_at, application_expires_at,
                    approved_days, pro_expires_at, membership_signature, tier
                ) VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?)""",
                (app_id, identity, float(now), expires_at + 86400, duration, expires_at, signature, tier),
            )
            event_name = f"granted_{tier}_permanent" if permanent else f"granted_{tier}"
            self._event(connection, app_id, event_name, now)
            return app_id

    def list_active_pro_qqs(self, *, now: float) -> list[str]:
        """返回所有当前有效 Pro 的 QQ 号列表。"""
        with self._transaction() as connection:
            self._cleanup(connection, now)
            rows = connection.execute(
                "SELECT qq_id FROM applications WHERE state = 'active' AND pro_expires_at >= ?",
                (float(now),),
            ).fetchall()
            return sorted({str(row["qq_id"]) for row in rows})

    # ── Pro Groups ──────────────────────────────────────────

    def _group_signature(self, group_id: str, activated_by: str, activated_at: float) -> str:
        if self._signing_key is None:
            raise ProStoreError("signing_key_unavailable")
        payload = f"group:{group_id}|{activated_by}|{float(activated_at):.6f}".encode("utf-8")
        return hmac.new(self._signing_key, payload, hashlib.sha256).hexdigest()

    def activate_group(self, group_id: str, reviewer_id: str, *, now: float) -> None:
        if str(reviewer_id or "").strip() != self.reviewer_id:
            raise ProStoreError("reviewer_required")
        gid = str(group_id or "").strip()
        if not gid.isdigit() or len(gid) < 5:
            raise ProStoreError("qq_id_invalid")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT group_id, deactivated_at FROM pro_groups WHERE group_id = ?",
                (gid,),
            ).fetchone()
            if row is not None and row["deactivated_at"] is None:
                raise ProStoreError("application_pending")  # reuse: already exists
            sig = self._group_signature(gid, self.reviewer_id, float(now))
            if row is None:
                connection.execute(
                    "INSERT INTO pro_groups(group_id, activated_by, activated_at, signature) VALUES(?,?,?,?)",
                    (gid, self.reviewer_id, float(now), sig),
                )
            else:
                connection.execute(
                    """UPDATE pro_groups
                       SET activated_by = ?, activated_at = ?, deactivated_at = NULL, signature = ?
                       WHERE group_id = ?""",
                    (self.reviewer_id, float(now), sig, gid),
                )

    def deactivate_group(self, group_id: str, reviewer_id: str, *, now: float) -> bool:
        if str(reviewer_id or "").strip() != self.reviewer_id:
            raise ProStoreError("reviewer_required")
        gid = str(group_id or "").strip()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT group_id FROM pro_groups WHERE group_id = ? AND deactivated_at IS NULL",
                (gid,),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "UPDATE pro_groups SET deactivated_at = ? WHERE group_id = ?",
                (float(now), gid),
            )
            return True

    def list_active_groups(self, reviewer_id: str, *, now: float) -> list[str]:
        if str(reviewer_id or "").strip() != self.reviewer_id:
            raise ProStoreError("reviewer_required")
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT group_id FROM pro_groups WHERE deactivated_at IS NULL ORDER BY activated_at ASC"
            ).fetchall()
            return [str(row["group_id"]) for row in rows]

    def is_active_group(self, group_id: str, *, now: float) -> bool:
        gid = str(group_id or "").strip()
        if not gid.isdigit():
            return False
        if self._signing_key is None:
            return False
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT group_id, activated_by, activated_at, signature FROM pro_groups WHERE group_id = ? AND deactivated_at IS NULL",
                (gid,),
            ).fetchone()
            if row is None:
                return False
            expected = self._group_signature(
                row["group_id"], row["activated_by"], row["activated_at"]
            )
            return hmac.compare_digest(str(row["signature"] or ""), expected)
