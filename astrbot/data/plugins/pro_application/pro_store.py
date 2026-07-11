"""Minimal, privacy-safe state store for human-reviewed Pro applications."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


APPLICATION_TTL_SECONDS = 72 * 60 * 60
CODE_TTL_SECONDS = 10 * 60
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


class ProStore:
    def __init__(self, path: Path, *, reviewer_id: str):
        self.path = Path(path)
        self.reviewer_id = str(reviewer_id or "").strip()
        if not self.reviewer_id.isdigit():
            raise ValueError("reviewer_id_invalid")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

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
                    pro_expires_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_applications_qq_state
                    ON applications(qq_id, state);
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    application_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_at REAL NOT NULL
                );
                """
            )

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
        )

    def _event(self, connection: sqlite3.Connection, application_id: str, event_type: str, now: float) -> None:
        connection.execute(
            "INSERT INTO events(application_id, event_type, event_at) VALUES (?, ?, ?)",
            (application_id, event_type, float(now)),
        )

    def _cleanup(self, connection: sqlite3.Connection, now: float) -> None:
        connection.execute(
            """
            UPDATE applications
            SET state = 'expired', verification_code_hash = NULL
            WHERE state IN ('pending_email', 'awaiting_review')
              AND application_expires_at < ?
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
            SET state = 'pro_expired'
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
                WHERE qq_id = ? AND state IN ('pending_email', 'awaiting_review', 'awaiting_verify')
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

    def approve(self, application_id: str, reviewer_id: str, days: int = DEFAULT_PRO_DAYS, *, now: float) -> str:
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
            code = self._verification_code()
            connection.execute(
                """
                UPDATE applications
                SET state = 'awaiting_verify', reviewer_id = ?, verification_code_hash = ?,
                    verification_expires_at = ?, verification_attempts = 0, approved_days = ?
                WHERE application_id = ?
                """,
                (
                    self.reviewer_id,
                    self._code_hash(code),
                    float(now) + CODE_TTL_SECONDS,
                    duration,
                    key,
                ),
            )
            self._event(connection, key, "approved_pending_verification", now)
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
            if row["verification_code_hash"] != candidate_hash:
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
                connection.execute(
                    """
                    UPDATE applications
                    SET state = 'active', pro_expires_at = ?, verification_expires_at = NULL
                    WHERE application_id = ?
                    """,
                    (expires_at, row["application_id"]),
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
                SELECT 1 FROM applications
                WHERE qq_id = ? AND state = 'active' AND pro_expires_at >= ?
                LIMIT 1
                """,
                (identity, float(now)),
            ).fetchone()
            return row is not None

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
                "UPDATE applications SET state = 'revoked', pro_expires_at = ? WHERE application_id = ?",
                (float(now), row["application_id"]),
            )
            self._event(connection, row["application_id"], "revoked", now)
            return True
