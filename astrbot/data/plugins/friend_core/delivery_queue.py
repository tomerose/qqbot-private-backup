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
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from astrbot.api import logger

try:
    from google.cloud import firestore
    from google.cloud.firestore import Client as FirestoreClient
except ImportError:
    firestore = None
    FirestoreClient = None

FIRESTORE_PROJECT = "solar-modem-496213-f5"
FIRESTORE_DATABASE = "qqbot"
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
    created_at: float = 0.0


class DeliveryQueue:
    """Firestore-backed persistent queue for file delivery to QQ."""

    def __init__(self):
        self._db: FirestoreClient | None = None
        self._send_fn = None  # callback(qq_id, message) set by friend_core
        self._deliver_fn = None  # callback(event_params) → bool set by runtime

    @property
    def db(self) -> FirestoreClient | None:
        if self._db is not None:
            return self._db
        if firestore is None:
            return None
        try:
            self._db = firestore.Client(
                project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE
            )
        except Exception as e:
            logger.warning(f"[DeliveryQueue] Firestore连接失败: {e}")
            return None
        return self._db

    # ── Enqueue ───────────────────────────────────────────────────

    def enqueue(self, *, local_path: str, file_name: str, kind: str = "file",
                sender_id: str = "", group_id: str = "",
                job_id: str = "", task_desc: str = "") -> str | None:
        """Store a failed delivery in Firestore for retry. Returns doc_id or None."""
        if not self.db or not sender_id.isdigit():
            return None
        if not Path(local_path).is_file():
            logger.warning("[DeliveryQueue] enqueue skipped: file missing %s", local_path)
            return None

        now = time.time()
        try:
            doc_ref = self.db.collection("users").document(sender_id)\
                .collection(DELIVERY_COLLECTION).document()
            doc_ref.set({
                "local_path": str(local_path),
                "file_name": file_name,
                "kind": kind,
                "sender_id": sender_id,
                "group_id": group_id,
                "retry_count": 0,
                "next_retry_at": now + BASE_DELAY_SECONDS,
                "status": "pending",
                "job_id": job_id,
                "task_desc": task_desc[:200],
                "created_at": now,
            })
            logger.info("[DeliveryQueue] ENQUEUED %s → QQ %s (retry in %ds)",
                         file_name, sender_id, BASE_DELAY_SECONDS)
            return doc_ref.id
        except Exception as e:
            logger.warning("[DeliveryQueue] enqueue fail: %s", e)
            return None

    # ── Poll & retry ──────────────────────────────────────────────

    def poll_and_retry(self) -> int:
        """Query Firestore for deliveries due for retry. Execute them. Returns count processed."""
        if not self.db:
            return 0
        now = time.time()
        processed = 0
        try:
            users_ref = self.db.collection("users").limit(500).stream()
            for user_doc in users_ref:
                qq_id = user_doc.id
                if not qq_id.isdigit():
                    continue
                deliveries = user_doc.reference.collection(DELIVERY_COLLECTION)\
                    .where("status", "==", "pending")\
                    .where("next_retry_at", "<=", now)\
                    .limit(5).stream()
                for doc in deliveries:
                    entry = self._doc_to_entry(doc.id, doc.to_dict())
                    if not entry:
                        continue
                    success = self._try_deliver(entry)
                    if success:
                        doc.reference.update({
                            "status": "delivered",
                            "delivered_at": now,
                        })
                        asyncio.create_task(self._notify_delivered(entry))
                        logger.info("[DeliveryQueue] DELIVERED %s → QQ %s",
                                     entry.file_name, entry.sender_id)
                    else:
                        new_count = entry.retry_count + 1
                        if new_count >= MAX_RETRIES:
                            doc.reference.update({
                                "status": "failed_permanent",
                                "retry_count": new_count,
                                "failed_at": now,
                            })
                            asyncio.create_task(self._notify_failed(entry))
                            logger.warning("[DeliveryQueue] FAILED PERMANENT %s → QQ %s",
                                        entry.file_name, entry.sender_id)
                        else:
                            next_at = now + BASE_DELAY_SECONDS * (2 ** new_count)
                            doc.reference.update({
                                "retry_count": new_count,
                                "next_retry_at": next_at,
                            })
                            logger.info("[DeliveryQueue] RETRY #%d %s (next in %ds)",
                                         new_count, entry.file_name,
                                         BASE_DELAY_SECONDS * (2 ** new_count))
                    processed += 1
        except Exception as e:
            logger.debug("[DeliveryQueue] poll fail: %s", e)
        return processed

    def _try_deliver(self, entry: DeliveryEntry) -> bool:
        """Attempt one delivery. Uses NapCat call_action directly."""
        if not Path(entry.local_path).is_file():
            return False  # file gone, will be marked failed

        try:
            # We need a NapCat client. Access via the stored bot reference.
            # The deliver_fn callback is set by friend_core at init time.
            if self._deliver_fn:
                return self._deliver_fn(
                    local_path=entry.local_path,
                    file_name=entry.file_name,
                    kind=entry.kind,
                    sender_id=entry.sender_id,
                    group_id=entry.group_id,
                )
            return False
        except Exception as e:
            logger.debug("[DeliveryQueue] _try_deliver error: %s", e)
            return False

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
        """Number of pending deliveries for a user."""
        if not self.db or not qq_id.isdigit():
            return 0
        try:
            docs = self.db.collection("users").document(qq_id)\
                .collection(DELIVERY_COLLECTION)\
                .where("status", "==", "pending").limit(50).stream()
            return len(list(docs))
        except Exception:
            return 0

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
