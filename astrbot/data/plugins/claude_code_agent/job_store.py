"""Privacy-preserving SQLite ledger for local Agent job metadata."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

TERMINAL_STATES = {"completed", "failed", "cancelled", "timeout", "recovery_blocked"}
ACTIVE_STATES = {
    "accepted", "planned", "queued", "awaiting_approval", "executing", "running",
    "recovering", "verifying", "delivering", "delivery_pending",
}
ALLOWED_STATES = TERMINAL_STATES | ACTIVE_STATES | {"interrupted"}
_DELIVERY_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_TRANSITIONS = {
    "accepted": {"planned", "cancelled", "failed", "recovery_blocked"},
    "planned": {"awaiting_approval", "queued", "executing", "cancelled", "recovery_blocked"},
    "queued": {"awaiting_approval", "executing", "running", "recovering", "cancelled", "recovery_blocked"},
    "awaiting_approval": {"queued", "executing", "running", "cancelled", "recovery_blocked"},
    "executing": {"awaiting_approval", "verifying", "failed", "cancelled", "timeout", "interrupted"},
    "running": {"verifying", "failed", "cancelled", "timeout", "interrupted"},
    "recovering": {"running", "verifying", "failed", "cancelled", "timeout", "interrupted"},
    "verifying": {"delivering", "failed", "cancelled", "timeout", "interrupted"},
    "delivering": {"delivery_pending", "completed", "failed", "interrupted"},
    "delivery_pending": {"completed", "failed", "interrupted"},
    "interrupted": {"recovering", "recovery_blocked"},
}


def _digest(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8", errors="replace")).hexdigest()


class JobStore:
    """Store only bounded metadata; never persist task text, paths, output, or tokens."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    owner_fingerprint TEXT NOT NULL,
                    scope_fingerprint TEXT NOT NULL,
                    task_digest TEXT NOT NULL,
                    backend TEXT NOT NULL,
                    state TEXT NOT NULL,
                    risk TEXT NOT NULL DEFAULT '',
                    exit_code INTEGER,
                    deliverable_count INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT NOT NULL DEFAULT '',
                    stage TEXT NOT NULL DEFAULT '',
                    recovery TEXT NOT NULL DEFAULT 'blocked',
                    delivery_digest TEXT NOT NULL DEFAULT '',
                    step_index INTEGER NOT NULL DEFAULT 0,
                    step_count INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    evidence TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    replayed INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_events_replayed ON task_events(replayed, created_at)"
            )
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        additions = {
            "stage": "TEXT NOT NULL DEFAULT ''",
            "recovery": "TEXT NOT NULL DEFAULT 'blocked'",
            "delivery_digest": "TEXT NOT NULL DEFAULT ''",
            "step_index": "INTEGER NOT NULL DEFAULT 0",
            "step_count": "INTEGER NOT NULL DEFAULT 1",
        }
        for name, declaration in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
        # task_events migration
        try:
            te_columns = {row[1] for row in conn.execute("PRAGMA table_info(task_events)")}
        except Exception:
            te_columns = set()
        if "sender_id" not in te_columns:
            try:
                conn.execute("ALTER TABLE task_events ADD COLUMN sender_id TEXT NOT NULL DEFAULT ''")
            except Exception:
                pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def start(
        self,
        job_id: str,
        owner_id: str,
        scope: str,
        task: str,
        backend: str,
        risk: str,
        state: str = "running",
        recovery: str = "blocked",
        stage: str = "",
        now: float | None = None,
        *,
        step_index: int = 0,
        step_count: int = 1,
    ) -> None:
        normalized_state = str(state).lower()
        if normalized_state not in ALLOWED_STATES:
            raise ValueError("任务状态无效")
        normalized_recovery = str(recovery).lower()
        if normalized_recovery not in {"blocked", "replay_safe"}:
            raise ValueError("恢复策略无效")
        timestamp = time.time() if now is None else float(now)
        normalized_step_count = int(step_count)
        normalized_step_index = int(step_index)
        if not 1 <= normalized_step_count <= 8:
            raise ValueError("任务步骤数量无效")
        if not 0 <= normalized_step_index < normalized_step_count:
            raise ValueError("任务步骤索引无效")
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """INSERT INTO jobs (
                    job_id, owner_fingerprint, scope_fingerprint, task_digest,
                    backend, state, risk, stage, recovery, step_index, step_count,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(job_id), _digest(owner_id)[:16], _digest(scope)[:16],
                    _digest(task), str(backend), normalized_state, str(risk)[:120],
                    str(stage)[:40], normalized_recovery, normalized_step_index,
                    normalized_step_count, timestamp, timestamp,
                ),
            )

    def transition(
        self,
        job_id: str,
        state: str,
        stage: str,
        error_code: str = "",
        now: float | None = None,
        *,
        step_index: int | None = None,
    ) -> None:
        normalized = str(state).lower()
        if normalized not in ALLOWED_STATES:
            raise ValueError("任务状态无效")
        timestamp = time.time() if now is None else float(now)
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT state, step_count FROM jobs WHERE job_id = ?", (str(job_id),)
            ).fetchone()
            if row is None:
                raise ValueError("任务不存在")
            current = str(row["state"])
            if normalized not in _TRANSITIONS.get(current, set()):
                raise ValueError(f"任务状态迁移无效: {current} -> {normalized}")
            normalized_step_index = None if step_index is None else int(step_index)
            if normalized_step_index is not None and not 0 <= normalized_step_index < int(row["step_count"]):
                raise ValueError("任务步骤索引无效")
            conn.execute(
                """UPDATE jobs SET state = ?, stage = ?, error_code = ?,
                    step_index = COALESCE(?, step_index), updated_at = ? WHERE job_id = ?""",
                (
                    normalized, str(stage)[:40], str(error_code)[:80],
                    normalized_step_index, timestamp, str(job_id),
                ),
            )

    def record_step(
        self,
        job_id: str,
        *,
        step_index: int,
        step_count: int,
        now: float | None = None,
    ) -> None:
        index = int(step_index)
        count = int(step_count)
        if not 1 <= count <= 8 or not 0 <= index < count:
            raise ValueError("任务步骤无效")
        timestamp = time.time() if now is None else float(now)
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """UPDATE jobs SET step_index = ?, step_count = ?, updated_at = ?
                    WHERE job_id = ?""",
                (index, count, timestamp, str(job_id)),
            )
            if cursor.rowcount != 1:
                raise ValueError("任务不存在")

    def finish(
        self,
        job_id: str,
        state: str,
        exit_code: int | None = None,
        deliverable_count: int = 0,
        error_code: str = "",
        now: float | None = None,
    ) -> None:
        normalized = str(state).lower()
        if normalized not in TERMINAL_STATES:
            raise ValueError("任务终态无效")
        timestamp = time.time() if now is None else float(now)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """UPDATE jobs SET state = ?, stage = ?, exit_code = ?, deliverable_count = ?,
                    error_code = ?, updated_at = ? WHERE job_id = ?""",
                (
                    normalized, normalized, exit_code, max(0, int(deliverable_count)),
                    str(error_code)[:80], timestamp, str(job_id),
                ),
            )

    def recover_interrupted(self, now: float | None = None) -> int:
        timestamp = time.time() if now is None else float(now)
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """UPDATE jobs SET state = 'interrupted', stage = 'interrupted',
                    error_code = 'process_restart', updated_at = ?
                    WHERE state IN ('executing', 'running', 'recovering', 'verifying', 'delivering')""",
                (timestamp,),
            )
            return int(cursor.rowcount)

    def list_interrupted(self) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT job_id, task_digest, backend, state, stage, recovery,
                    delivery_digest, step_index, step_count, updated_at FROM jobs
                    WHERE state = 'interrupted' ORDER BY created_at, job_id"""
            ).fetchall()
        return [dict(row) for row in rows]

    def list_recoverable(self) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT job_id, task_digest, backend, state, stage, recovery,
                    delivery_digest, step_index, step_count, updated_at FROM jobs
                    WHERE state IN ('interrupted', 'queued') ORDER BY created_at, job_id"""
            ).fetchall()
        return [dict(row) for row in rows]

    def list_active_for(
        self,
        owner_id: str,
        scope: str = "",
        *,
        limit: int = 5,
    ) -> list[dict]:
        """Return privacy-safe active metadata visible to one user/session."""
        clauses = [
            "owner_fingerprint = ?",
            "state IN ({})".format(",".join("?" for _ in ACTIVE_STATES | {"interrupted"})),
        ]
        params: list[object] = [_digest(owner_id)[:16], *(ACTIVE_STATES | {"interrupted"})]
        normalized_scope = str(scope or "").strip()
        if normalized_scope:
            clauses.append("scope_fingerprint = ?")
            params.append(_digest(normalized_scope)[:16])
        params.append(max(1, min(int(limit), 20)))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""SELECT job_id, backend, state, stage, recovery,
                    delivery_digest, step_index, step_count, updated_at
                    FROM jobs WHERE {' AND '.join(clauses)}
                    ORDER BY updated_at DESC LIMIT ?""",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def record_delivery(
        self,
        job_id: str,
        digest: str,
        now: float | None = None,
    ) -> None:
        normalized = str(digest or "").strip().lower()
        if not _DELIVERY_DIGEST.fullmatch(normalized):
            raise ValueError("交付摘要无效")
        timestamp = time.time() if now is None else float(now)
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "UPDATE jobs SET delivery_digest = ?, updated_at = ? WHERE job_id = ?",
                (normalized, timestamp, str(job_id)),
            )
            if cursor.rowcount != 1:
                raise ValueError("任务不存在")

    def get(self, job_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (str(job_id),)
            ).fetchone()
        return dict(row) if row is not None else None

    def record_task_event(
        self,
        job_id: str,
        status: str,
        evidence: str = "",
        sender_id: str = "",
        now: float | None = None,
    ) -> None:
        """Local fallback queue for cross-dialog task events when Firestore is unreachable."""
        timestamp = time.time() if now is None else float(now)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """INSERT INTO task_events (job_id, sender_id, status, evidence, created_at)
                    VALUES (?, ?, ?, ?, ?)""",
                (str(job_id), str(sender_id)[:20], str(status)[:40], str(evidence)[:200], timestamp),
            )

    def pending_task_events(self, limit: int = 50) -> list[dict]:
        """Return unreplayed local task events so the memory plugin can catch up."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT id, job_id, sender_id, status, evidence, created_at FROM task_events
                    WHERE replayed = 0 ORDER BY created_at LIMIT ?""",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_task_events_replayed(self, event_ids: list[int]) -> None:
        if not event_ids:
            return
        with closing(self._connect()) as conn, conn:
            conn.executemany(
                "UPDATE task_events SET replayed = 1 WHERE id = ?",
                [(int(eid),) for eid in event_ids],
            )

    def cleanup_task_events(self, before_days: int = 7) -> int:
        """Delete replayed events older than N days. Returns count deleted."""
        cutoff = time.time() - float(before_days) * 86400
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "DELETE FROM task_events WHERE replayed = 1 AND created_at < ?",
                (cutoff,),
            )
            return int(cursor.rowcount)
