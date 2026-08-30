"""Firestore-backed persistent delivery queue — guarantees files reach QQ.

Problem: deliver_local_artifact tries 4 channels once and gives up.
If QQ risk-control blocks all channels, the file is stuck on disk forever.
User sees "发送失败" and has to retry manually.

Solution: Google ecosystem persistent retry queue.
1. On delivery failure → enqueue in Firestore with file path, target user, retry count
2. Background worker polls every 60s, retries pending deliveries
3. Exponential backoff: 60s → 120s → 240s → ... → max 10 retries (~30 min)
4. On success → notify user on QQ, clean up Firestore entry
5. On permanent failure → notify user with manual recovery instructions

This is the Google ecosystem approach: Firestore as reliable persistent queue,
zero additional infrastructure (no Cloud Tasks, no Pub/Sub).
"""

from __future__ import annotations

import asyncio
import inspect
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger

try:
    from google.cloud import firestore
    from google.cloud.firestore import Client as FirestoreClient
except ImportError:
    firestore = None
    FirestoreClient = None

FIRESTORE_PROJECT = os.getenv("FIRESTORE_PROJECT", "").strip()
FIRESTORE_DATABASE = os.getenv("FIRESTORE_DATABASE", "qqbot").strip()
DELIVERY_COLLECTION = "pending_deliveries"
MAX_RETRIES = 10
BASE_DELAY_SECONDS = 60  # first retry after 60s, then 120s, 240s...
POLL_INTERVAL = 60
CLEANUP_AGE_DAYS = 3


@dataclass
class DeliveryEntry:
    """A file queued for delivery to QQ."""
    doc_id: str = ""
    local_path: str = ""
    file_name: str = ""
    kind: str = "file"  # "file" or "image"
    sender_id: str = ""
    group_id: str = ""
    retry_count: int = 0
    next_retry_at: float = 0.0
    status: str = "pending"
    job_id: str = ""
    task_desc: str = ""
    task_owner: str = ""
    created_at: float = 0.0


class DeliveryQueue:
    """Persistent queue for file delivery to QQ — Firestore + local SQLite fallback."""

    def __init__(self):
        self._db: FirestoreClient | None = None
        self._send_fn = None  # callback(qq_id, message) set by friend_core
        self._deliver_fn = None  # callback(event_params) → bool set by runtime
        self._task_tracker = None  # optional sync callback used by tests/integrations
        self._outcome_handlers: dict[str, Any] = {}
        self._init_local_db()

    # ── Local SQLite fallback ──────────────────────────────────────

    def _local_db_path(self) -> Path:
        return (
            Path(__file__).resolve().parents[2]
            / "plugin_data" / "friend_core" / "delivery_queue.db"
        )

    def _init_local_db(self) -> None:
        import sqlite3
        path = self._local_db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    local_path TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'file',
                    sender_id TEXT NOT NULL,
                    group_id TEXT NOT NULL DEFAULT '',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    next_retry_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    job_id TEXT NOT NULL DEFAULT '',
                    task_desc TEXT NOT NULL DEFAULT '',
                    task_owner TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pending_next ON pending_deliveries(status, next_retry_at)"
            )

    def _local_enqueue(self, *, local_path: str, file_name: str, kind: str,
                       sender_id: str, group_id: str, job_id: str,
                       task_desc: str, task_owner: str) -> int | None:
        """Always-on local enqueue — never blocked by Firestore."""
        import sqlite3
        now = time.time()
        try:
            with sqlite3.connect(str(self._local_db_path())) as conn:
                cursor = conn.execute(
                    """INSERT INTO pending_deliveries
                       (local_path, file_name, kind, sender_id, group_id,
                        retry_count, next_retry_at, status, job_id, task_desc,
                        task_owner, created_at)
                       VALUES (?,?,?,?,?, 0, ?, 'pending', ?,?,?, ?)""",
                    (str(local_path), file_name, kind, sender_id, group_id,
                     now + BASE_DELAY_SECONDS, job_id, task_desc[:200],
                     task_owner[:24], now),
                )
                return cursor.lastrowid
        except Exception as e:
            logger.warning("[DeliveryQueue] local enqueue failed: %s", type(e).__name__)
            return None

    def register_outcome_handler(self, owner: str, callback: Any) -> None:
        """Register one live owner callback for local task-ledger convergence."""
        key = str(owner or "").strip().lower()
        if key and callable(callback):
            self._outcome_handlers[key] = callback

    async def _emit_outcome(
        self, entry: DeliveryEntry, status: str, evidence: str
    ) -> None:
        callback = self._outcome_handlers.get(entry.task_owner.lower())
        if callback is None:
            return
        try:
            result = callback(entry, status, evidence)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.debug(
                "[DeliveryQueue] owner outcome callback failed",
            )

    @property
    def db(self) -> FirestoreClient | None:
        if self._db is not None:
            return self._db
        if firestore is None or not FIRESTORE_PROJECT:
            return None
        try:
            self._db = firestore.Client(
                project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE
            )
        except Exception as e:
            logger.warning("[DeliveryQueue] Firestore连接失败: %s", type(e).__name__)
            return None
        return self._db

    # ── Enqueue ───────────────────────────────────────────────────

    def enqueue(self, *, local_path: str, file_name: str, kind: str = "file",
                sender_id: str = "", group_id: str = "",
                job_id: str = "", task_desc: str = "",
                task_owner: str = "") -> str | None:
        """Store a failed delivery for retry. Local SQLite always first; Firestore best-effort."""
        if not sender_id.isdigit():
            return None
        if not Path(local_path).is_file():
            logger.warning("[DeliveryQueue] enqueue skipped: file missing")
            return None

        # ── Local SQLite: always works, no network dependency ──
        row_id = self._local_enqueue(
            local_path=local_path, file_name=file_name, kind=kind,
            sender_id=sender_id, group_id=group_id, job_id=job_id,
            task_desc=task_desc, task_owner=task_owner,
        )
        if row_id is None:
            return None
        local_doc_id = f"local-{row_id}"
        logger.info("[DeliveryQueue] ENQUEUED(local)")

        # ── Firestore: best-effort mirror ──
        if self.db and sender_id.isdigit():
            try:
                now = time.time()
                user_ref = self.db.collection("users").document(sender_id)
                user_ref.set({"delivery_queue_registered_at": now}, merge=True)
                doc_ref = user_ref.collection(DELIVERY_COLLECTION).document()
                doc_ref.set({
                    "local_path": str(local_path), "file_name": file_name,
                    "kind": kind, "sender_id": sender_id, "group_id": group_id,
                    "retry_count": 0, "next_retry_at": now + BASE_DELAY_SECONDS,
                    "status": "pending", "job_id": job_id,
                    "task_desc": task_desc[:200], "task_owner": task_owner[:24],
                    "created_at": now,
                })
            except Exception as e:
                logger.debug("[DeliveryQueue] Firestore mirror skipped: %s", type(e).__name__)

        return local_doc_id

    # ── Poll & retry ──────────────────────────────────────────────

    def _local_poll_entries(self, now: float) -> list[dict]:
        """Read due entries from local SQLite."""
        import sqlite3
        try:
            with sqlite3.connect(str(self._local_db_path())) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """SELECT * FROM pending_deliveries
                       WHERE status = 'pending' AND next_retry_at <= ?
                       ORDER BY created_at LIMIT 100""",
                    (now,),
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception:
            return []

    def _local_update_entry(self, row_id: int, status: str,
                            retry_count: int = 0, next_at: float = 0.0) -> None:
        import sqlite3
        try:
            with sqlite3.connect(str(self._local_db_path())) as conn:
                conn.execute(
                    """UPDATE pending_deliveries
                       SET status=?, retry_count=?, next_retry_at=?
                       WHERE id=?""",
                    (status, retry_count, next_at, row_id),
                )
        except Exception as e:
            logger.debug("[DeliveryQueue] local update fail: %s", type(e).__name__)

    def _local_cleanup(self, before_days: int = 7) -> None:
        import sqlite3
        cutoff = time.time() - float(before_days) * 86400
        try:
            with sqlite3.connect(str(self._local_db_path())) as conn:
                conn.execute(
                    "DELETE FROM pending_deliveries WHERE status != 'pending' AND created_at < ?",
                    (cutoff,),
                )
        except Exception:
            pass

    async def poll_and_retry(self) -> int:
        """Query local SQLite + Firestore for due deliveries. Execute them."""
        now = time.time()
        processed = 0

        # ── Local SQLite entries (always works) ──
        local_entries = await asyncio.to_thread(self._local_poll_entries, now)
        for row in local_entries:
            entry = DeliveryEntry(
                doc_id=f"local-{row['id']}",
                local_path=row["local_path"], file_name=row["file_name"],
                kind=row["kind"], sender_id=row["sender_id"],
                group_id=row["group_id"], retry_count=row["retry_count"],
                next_retry_at=row["next_retry_at"], status=row["status"],
                job_id=row["job_id"], task_desc=row["task_desc"],
                task_owner=row["task_owner"], created_at=row["created_at"],
            )
            success = await self._try_deliver(entry)
            if success:
                self._local_update_entry(row["id"], "delivered")
                await self._track_delivery_outcome(entry, "done", "qq:retry_queue")
                logger.info("[DeliveryQueue] DELIVERED(local)")
            else:
                new_count = entry.retry_count + 1
                if new_count >= MAX_RETRIES:
                    self._local_update_entry(row["id"], "failed_permanent", new_count)
                    await self._track_delivery_outcome(entry, "failed", "delivery_retries_exhausted")
                    logger.warning("[DeliveryQueue] FAILED(local)")
                else:
                    next_at = now + BASE_DELAY_SECONDS * (2 ** new_count)
                    self._local_update_entry(row["id"], "pending", new_count, next_at)
                    logger.info("[DeliveryQueue] RETRY(local) #%d", new_count)
            processed += 1

        # ── Firestore entries (best-effort mirror) ──
        if self.db:
            try:
                docs = await asyncio.to_thread(self._query_due_documents, now)
                for doc in docs:
                    entry = self._doc_to_entry(doc.id, doc.to_dict())
                    if not entry:
                        continue
                    success = await self._try_deliver(entry)
                    if success:
                        await asyncio.to_thread(doc.reference.update, {
                            "status": "delivered", "delivered_at": now,
                        })
                        await self._track_delivery_outcome(entry, "done", "qq:retry_queue")
                        await self._notify_delivered(entry)
                        logger.info("[DeliveryQueue] DELIVERED")
                    else:
                        new_count = entry.retry_count + 1
                        if new_count >= MAX_RETRIES:
                            await asyncio.to_thread(doc.reference.update, {
                                "status": "failed_permanent",
                                "retry_count": new_count, "failed_at": now,
                            })
                            await self._track_delivery_outcome(entry, "failed", "delivery_retries_exhausted")
                            await self._notify_failed(entry)
                            logger.warning("[DeliveryQueue] FAILED PERMANENT")
                        else:
                            next_at = now + BASE_DELAY_SECONDS * (2 ** new_count)
                            await asyncio.to_thread(doc.reference.update, {
                                "retry_count": new_count, "next_retry_at": next_at,
                            })
                            logger.info("[DeliveryQueue] RETRY #%d", new_count)
                    processed += 1
            except Exception as e:
                logger.debug("[DeliveryQueue] Firestore poll skipped: %s", type(e).__name__)

        # Periodic cleanup
        if processed > 0:
            self._local_cleanup()
        return processed

    def _query_due_documents(self, now: float) -> list[Any]:
        """Find due queue entries, including entries below missing parent docs."""
        collection_group = getattr(self.db, "collection_group", None)
        if callable(collection_group):
            try:
                docs = collection_group(DELIVERY_COLLECTION)\
                    .where("status", "==", "pending").limit(500).stream()
                return [
                    doc for doc in docs
                    if float((doc.to_dict() or {}).get("next_retry_at", 0)) <= now
                ][:100]
            except Exception as exc:
                logger.debug("[DeliveryQueue] collection-group query unavailable: %s", type(exc).__name__)

        due: list[Any] = []
        for user_doc in self.db.collection("users").limit(500).stream():
            if not str(user_doc.id).isdigit():
                continue
            docs = user_doc.reference.collection(DELIVERY_COLLECTION)\
                .where("status", "==", "pending").limit(20).stream()
            for doc in docs:
                if float((doc.to_dict() or {}).get("next_retry_at", 0)) <= now:
                    due.append(doc)
                    if len(due) >= 100:
                        return due
        return due

    async def _try_deliver(self, entry: DeliveryEntry) -> bool:
        """Attempt one delivery. Uses NapCat call_action directly."""
        if not Path(entry.local_path).is_file():
            return False  # file gone, will be marked failed

        try:
            # We need a NapCat client. Access via the stored bot reference.
            # The deliver_fn callback is set by friend_core at init time.
            if self._deliver_fn:
                result = self._deliver_fn(
                    local_path=entry.local_path,
                    file_name=entry.file_name,
                    kind=entry.kind,
                    sender_id=entry.sender_id,
                    group_id=entry.group_id,
                )
                if inspect.isawaitable(result):
                    result = await result
                return bool(result)
            return False
        except Exception as e:
            logger.debug("[DeliveryQueue] _try_deliver error: %s", type(e).__name__)
            return False

    async def _track_delivery_outcome(
        self, entry: DeliveryEntry, status: str, evidence: str
    ) -> None:
        """Advance the real task ledger only when the whole queued task settles."""
        if not entry.job_id or not entry.task_owner or not entry.task_desc:
            return
        if status == "done":
            try:
                docs = await asyncio.to_thread(
                    lambda: list(
                        self.db.collection("users").document(entry.sender_id)
                        .collection(DELIVERY_COLLECTION)
                        .where("status", "==", "pending").limit(100).stream()
                    )
                )
                if any(
                    str((doc.to_dict() or {}).get("job_id", "")) == entry.job_id
                    and str((doc.to_dict() or {}).get("task_owner", "")) == entry.task_owner
                    for doc in docs
                ):
                    await self._emit_outcome(
                        entry, "artifact_delivered", evidence
                    )
                    return
            except Exception:
                await self._emit_outcome(entry, "artifact_delivered", evidence)
                return
        await self._emit_outcome(entry, status, evidence)
        try:
            track_runtime_task_status = self._task_tracker
            if track_runtime_task_status is None:
                try:
                    from astrbot_plugin_xiaoning_memory.main import track_runtime_task_status
                except ImportError:
                    from data.plugins.astrbot_plugin_xiaoning_memory.main import track_runtime_task_status
            await asyncio.to_thread(
                track_runtime_task_status,
                entry.sender_id,
                entry.job_id,
                entry.task_desc,
                status,
                evidence,
                entry.task_owner,
            )
        except Exception:
            logger.debug(
                "[DeliveryQueue] task mirror unavailable: %s/%s",
                entry.task_owner,
                entry.job_id,
            )
    # ── Notifications ─────────────────────────────────────────────

    async def _notify_delivered(self, entry: DeliveryEntry):
        """Send QQ message: file was delivered."""
        if self._send_fn:
            msg = f"📦 之前未发出的文件「{entry.file_name}」已成功发送给你～"
            if entry.task_desc:
                msg += f"\n任务：{entry.task_desc[:80]}"
            await self._send_fn(entry.sender_id, msg)

    async def _notify_failed(self, entry: DeliveryEntry):
        """Send QQ message: delivery permanently failed."""
        if self._send_fn:
            job_hint = f"\n可尝试 /agent recover {entry.job_id}" if entry.job_id else ""
            msg = (f"⚠ 文件「{entry.file_name}」发送多次仍未成功。"
                   f"请稍后重试或联系小江。{job_hint}")
            await self._send_fn(entry.sender_id, msg)

    # ── Cleanup ───────────────────────────────────────────────────

    def cleanup(self) -> int:
        """Delete delivered/failed entries older than CLEANUP_AGE_DAYS."""
        if not self.db:
            return 0
        cutoff = time.time() - CLEANUP_AGE_DAYS * 86400
        deleted = 0
        try:
            users_ref = self.db.collection("users").limit(200).stream()
            for user_doc in users_ref:
                docs = user_doc.reference.collection(DELIVERY_COLLECTION)\
                    .where("created_at", "<=", cutoff).limit(20).stream()
                for doc in docs:
                    data = doc.to_dict()
                    if data.get("status") in ("delivered", "failed_permanent"):
                        doc.reference.delete()
                        deleted += 1
        except Exception:
            pass
        return deleted

    # ── Status query ──────────────────────────────────────────────

    def pending_count(self, qq_id: str) -> int:
        """Number of pending deliveries for a user (local + Firestore)."""
        if not qq_id.isdigit():
            return 0
        count = 0
        # Local SQLite (fast, always works)
        import sqlite3
        try:
            with sqlite3.connect(str(self._local_db_path())) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM pending_deliveries WHERE sender_id=? AND status='pending'",
                    (qq_id,),
                ).fetchone()
                if row:
                    count += int(row[0])
        except Exception:
            pass
        # Firestore (best-effort)
        if self.db:
            try:
                docs = self.db.collection("users").document(qq_id)\
                    .collection(DELIVERY_COLLECTION)\
                    .where("status", "==", "pending").limit(50).stream()
                count += len(list(docs))
            except Exception:
                pass
        return count

    # ── Helpers ──────────────────────────────────────────────────

    @staticmethod
    def _doc_to_entry(doc_id: str, data: dict) -> DeliveryEntry | None:
        try:
            return DeliveryEntry(
                doc_id=doc_id,
                local_path=str(data.get("local_path", "")),
                file_name=str(data.get("file_name", "")),
                kind=str(data.get("kind", "file")),
                sender_id=str(data.get("sender_id", "")),
                group_id=str(data.get("group_id", "")),
                retry_count=int(data.get("retry_count", 0)),
                next_retry_at=float(data.get("next_retry_at", 0)),
                status=str(data.get("status", "pending")),
                job_id=str(data.get("job_id", "")),
                task_desc=str(data.get("task_desc", "")),
                task_owner=str(data.get("task_owner", "")),
                created_at=float(data.get("created_at", 0)),
            )
        except (ValueError, TypeError):
            return None


# ── Singleton ─────────────────────────────────────────────────────
_queue: DeliveryQueue | None = None


def get_queue() -> DeliveryQueue:
    global _queue
    if _queue is None:
        _queue = DeliveryQueue()
    return _queue
