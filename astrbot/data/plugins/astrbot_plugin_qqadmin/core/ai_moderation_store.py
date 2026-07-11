"""Anonymous, privacy-minimal state for AI group moderation."""

from __future__ import annotations

import csv
import hashlib
import hmac
import os
import secrets
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path

from .ai_moderation_policy import ALLOWED_REASONS

OFFENSE_TTL_SECONDS = 24 * 60 * 60
ALLOWED_ACTIONS = {"recall", "mute"}


def _run_hidden(command: list[str]) -> None:
    subprocess.run(
        command,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _windows_user_sid() -> str:
    result = subprocess.run(
        ["whoami.exe", "/user", "/fo", "csv", "/nh"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    row = next(csv.reader([result.stdout.strip()]))
    sid = row[-1].strip()
    if not sid.startswith("S-1-"):
        raise RuntimeError("无法确认当前用户安全标识")
    return sid


def harden_private_path(directory: Path, files: list[Path]) -> None:
    """Fail closed if Windows ACLs cannot be restricted."""
    if os.name != "nt":
        return
    sid = _windows_user_sid()
    _run_hidden(
        [
            "icacls.exe",
            str(directory),
            "/inheritance:r",
            "/grant:r",
            f"*{sid}:(OI)(CI)F",
            "*S-1-5-18:(OI)(CI)F",
            "*S-1-5-32-544:(OI)(CI)F",
        ]
    )
    for path in files:
        if not path.exists():
            continue
        _run_hidden(
            [
                "icacls.exe",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"*{sid}:F",
                "*S-1-5-18:F",
                "*S-1-5-32-544:F",
            ]
        )


class AIModerationStore:
    """Persist only HMAC identifiers and fixed moderation metadata."""

    def __init__(
        self, db_path: Path, salt_path: Path, default_enabled: bool = False
    ):
        self.db_path = Path(db_path)
        self.salt_path = Path(salt_path)
        self.default_enabled = bool(default_enabled)
        if self.db_path.parent.resolve() != self.salt_path.parent.resolve():
            raise ValueError("审计数据库和盐文件必须位于同一私有目录")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        harden_private_path(self.db_path.parent, [])
        if not self.salt_path.exists():
            self.salt_path.write_bytes(secrets.token_bytes(32))
        self._salt = self.salt_path.read_bytes()
        if len(self._salt) != 32:
            raise RuntimeError("审计匿名化盐无效")
        with closing(self._connect()) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    group_hash TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1))
                );
                CREATE TABLE IF NOT EXISTS offenses (
                    group_hash TEXT NOT NULL,
                    user_hash TEXT NOT NULL,
                    occurred_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS offenses_lookup
                    ON offenses(group_hash, user_hash, occurred_at);
                CREATE TABLE IF NOT EXISTS audit (
                    occurred_at REAL NOT NULL,
                    group_hash TEXT NOT NULL,
                    user_hash TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    confidence_bucket TEXT NOT NULL,
                    success INTEGER NOT NULL CHECK(success IN (0, 1))
                );
                """
            )
        harden_private_path(self.db_path.parent, [self.db_path, self.salt_path])

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=5)

    def _fingerprint(self, raw: str) -> str:
        return hmac.new(
            self._salt,
            str(raw).encode("utf-8", errors="replace"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _confidence_bucket(confidence: float) -> str:
        value = max(0.0, min(float(confidence), 1.0))
        if value >= 0.98:
            return "98-100"
        if value >= 0.95:
            return "95-97"
        return "90-94"

    def set_enabled(self, group_id: str, enabled: bool) -> None:
        group_hash = self._fingerprint(group_id)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """INSERT INTO settings(group_hash, enabled) VALUES(?, ?)
                ON CONFLICT(group_hash) DO UPDATE SET enabled=excluded.enabled""",
                (group_hash, int(bool(enabled))),
            )

    def is_enabled(self, group_id: str) -> bool:
        group_hash = self._fingerprint(group_id)
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT enabled FROM settings WHERE group_hash = ?", (group_hash,)
            ).fetchone()
        return bool(row[0]) if row is not None else self.default_enabled

    def offense_count(self, group_id: str, user_id: str, now: float | None = None) -> int:
        checked_at = time.time() if now is None else float(now)
        cutoff = checked_at - OFFENSE_TTL_SECONDS
        group_hash = self._fingerprint(group_id)
        user_hash = self._fingerprint(user_id)
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM offenses WHERE occurred_at < ?", (cutoff,))
            row = conn.execute(
                """SELECT COUNT(*) FROM offenses
                WHERE group_hash = ? AND user_hash = ? AND occurred_at >= ?""",
                (group_hash, user_hash, cutoff),
            ).fetchone()
        return int(row[0] if row else 0)

    def record_action(
        self,
        group_id: str,
        user_id: str,
        action: str,
        reason_code: str,
        confidence: float,
        success: bool,
        now: float | None = None,
    ) -> None:
        normalized_action = str(action)
        normalized_reason = str(reason_code)
        if normalized_action not in ALLOWED_ACTIONS:
            raise ValueError("审计动作不在允许列表")
        if normalized_reason not in ALLOWED_REASONS:
            raise ValueError("审计原因不在允许列表")
        timestamp = time.time() if now is None else float(now)
        group_hash = self._fingerprint(group_id)
        user_hash = self._fingerprint(user_id)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """INSERT INTO audit(
                    occurred_at, group_hash, user_hash, action,
                    reason_code, confidence_bucket, success
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    timestamp,
                    group_hash,
                    user_hash,
                    normalized_action,
                    normalized_reason,
                    self._confidence_bucket(confidence),
                    int(bool(success)),
                ),
            )
            if success:
                conn.execute(
                    "INSERT INTO offenses(group_hash, user_hash, occurred_at) VALUES (?, ?, ?)",
                    (group_hash, user_hash, timestamp),
                )

    def prune(self, now: float | None = None) -> int:
        checked_at = time.time() if now is None else float(now)
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "DELETE FROM offenses WHERE occurred_at < ?",
                (checked_at - OFFENSE_TTL_SECONDS,),
            )
            return int(cursor.rowcount)
