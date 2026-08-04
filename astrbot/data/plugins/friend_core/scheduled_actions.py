"""Google Firestore-backed scheduled actions — actual execution, not just text injection.

Problem: commitment_tracker stores promises as text but never executes them.
"30分钟后提醒你" → stored in Firestore → injected as text next conversation → useless.

Solution: parse time expressions → calculate fire_at → Firestore document →
APScheduler polls every 60s → send QQ reminder → mark done.

This is the Google ecosystem solution: Firestore as persistent schedule store,
survives restarts, zero additional infra.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone

from astrbot.api import logger

try:
    from google.cloud import firestore
    from google.cloud.firestore import Client as FirestoreClient
except ImportError:
    firestore = None
    FirestoreClient = None

FIRESTORE_PROJECT = "solar-modem-496213-f5"
FIRESTORE_DATABASE = "qqbot"
POLL_INTERVAL_SECONDS = 60

# ── Time expression parsing ──────────────────────────────────────────
# Format: (regex, extract_fn) → minutes from now
# ponytail: regex covers ~90% of common Chinese time expressions.
# Remaining 10% (complex relative dates like "下周三下午") need LLM parsing.

_MINUTE_RE = re.compile(r"(\d+)\s*分(?:钟)?\s*(?:后|以后|之后)?")
_HOUR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:个)?(?:小时|钟头)\s*(?:后|以后|之后)?")
_HALF_HOUR_RE = re.compile(r"半(?:个)?(?:小时|钟头)\s*(?:后|以后|之后)?")
_TOMORROW_RE = re.compile(r"明天\s*(?:早上|上午)?\s*(\d{1,2})\s*[点时:]?\s*(?:钟|分)?")
_TODAY_TIME_RE = re.compile(r"今天\s*(?:下午|晚上|中午)?\s*(\d{1,2})\s*[点时:]?\s*(?:钟|分)?")
_AFTERNOON_RE = re.compile(r"下午\s*(\d{1,2})\s*[点时:]?")
_EVENING_RE = re.compile(r"晚上\s*(\d{1,2})\s*[点时:]?")
_MORNING_RE = re.compile(r"(?:早上|上午)\s*(\d{1,2})\s*[点时:]?")
_NOON_RE = re.compile(r"中午\s*(\d{0,2})\s*[点时:]?")
_SECONDS_RE = re.compile(r"(\d+)\s*秒\s*(?:后|以后|之后)?")


def parse_time_expression(text: str, now: datetime | None = None) -> datetime | None:
    """Extract a future timestamp from Chinese time expressions. Returns None if unparseable."""
    if now is None:
        now = datetime.now()
    text = str(text or "").strip()
    if not text:
        return None

    # 30分钟后, 5分钟后
    m = _MINUTE_RE.search(text)
    if m:
        return now + timedelta(minutes=int(m.group(1)))

    # 2小时后, 1.5小时后, 2个小时后
    m = _HOUR_RE.search(text)
    if m:
        return now + timedelta(hours=float(m.group(1)))

    # 半小时后
    if _HALF_HOUR_RE.search(text):
        return now + timedelta(minutes=30)

    # 30秒后
    m = _SECONDS_RE.search(text)
    if m:
        return now + timedelta(seconds=int(m.group(1)))

    # 明天早上8点 / 明天8点
    m = _TOMORROW_RE.search(text)
    if m:
        target = now + timedelta(days=1)
        return target.replace(hour=int(m.group(1)), minute=0, second=0, microsecond=0)

    # 今天下午3点
    m = _TODAY_TIME_RE.search(text)
    if m:
        hour = int(m.group(1))
        if "下午" in text:
            hour += 12
        return now.replace(hour=hour, minute=0, second=0, microsecond=0)

    # 下午3点
    m = _AFTERNOON_RE.search(text)
    if m:
        hour = int(m.group(1)) + 12
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)  # tomorrow if already past
        return target

    # 晚上8点
    m = _EVENING_RE.search(text)
    if m:
        hour = int(m.group(1)) + 12
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    # 早上9点, 上午10点
    m = _MORNING_RE.search(text)
    if m:
        hour = int(m.group(1))
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    # 中午12点
    if _NOON_RE.search(text):
        hour = 12
        m = _NOON_RE.search(text)
        if m and m.group(1):
            hour = int(m.group(1))
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    return None


# ── Promise patterns that indicate the bot committed to a future action ──
_PROMISE_RE = re.compile(
    r"(?:会提醒你|到时候提醒|记得提醒|准时提醒|帮你记着|帮你盯着|会叫你|"
    r"时间到了|到点|我叫你|到时候找你|提醒你|通知你|喊你|叫你)",
    re.I,
)


def extract_scheduled_action(user_msg: str, bot_reply: str) -> tuple[datetime, str] | None:
    """If bot made a time-based promise, return (fire_at, action_text) or None."""
    if not _PROMISE_RE.search(bot_reply):
        return None

    fire_at = parse_time_expression(user_msg) or parse_time_expression(bot_reply)
    if fire_at is None:
        return None

    # Build a natural action message
    action_text = bot_reply[:150].strip()
    if not action_text:
        action_text = user_msg[:100].strip()
    return fire_at, action_text


# ── Firestore scheduled actions store ─────────────────────────────────

class ScheduledActionStore:
    """Persist and poll scheduled actions backed by Firestore."""

    def __init__(self):
        self._db: FirestoreClient | None = None
        self._poll_task: asyncio.Task | None = None
        self._send_fn = None  # set by friend_core after init

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
            logger.warning(f"[ScheduledAction] Firestore连接失败: {e}")
            return None
        return self._db

    def schedule(self, qq_id: str, fire_at: datetime, action_text: str,
                 context_msg: str = "") -> bool:
        """Store a scheduled action in Firestore. Returns True on success."""
        if not self.db or not qq_id.isdigit():
            return False
        try:
            doc_ref = self.db.collection("users").document(qq_id)\
                .collection("scheduled_actions").document()
            doc_ref.set({
                "fire_at": fire_at,
                "action_text": action_text[:200],
                "context_msg": context_msg[:200],
                "status": "pending",
                "created_at": datetime.now(timezone.utc),
            })
            logger.info("[ScheduledAction] scheduled @ %s", fire_at.strftime("%m-%d %H:%M"))
            return True
        except Exception as e:
            logger.debug("[ScheduledAction] store fail: %s", type(e).__name__)
            return False

    def poll_and_execute(self) -> int:
        """Query Firestore for due actions, execute them, mark done. Returns count executed."""
        if not self.db:
            return 0
        now = datetime.now(timezone.utc)
        executed = 0
        try:
            # Query across all users — Firestore collection group query
            # ponytail: iterate over known users' scheduled_actions subcollections.
            # Collection group query would be cleaner but requires composite index.
            users_ref = self.db.collection("users").limit(500).stream()
            for user_doc in users_ref:
                qq_id = user_doc.id
                if not qq_id.isdigit():
                    continue
                actions = user_doc.reference.collection("scheduled_actions")\
                    .where("status", "==", "pending")\
                    .where("fire_at", "<=", now)\
                    .limit(5).stream()
                for doc in actions:
                    data = doc.to_dict()
                    action_text = data.get("action_text", "")
                    try:
                        if self._send_fn:
                            asyncio.create_task(
                                self._send_fn(qq_id, f"⏰ 提醒：{action_text}")
                            )
                        doc.reference.update({"status": "done", "executed_at": now})
                        executed += 1
                        logger.info("[ScheduledAction] FIRED")
                    except Exception as e:
                        logger.debug("[ScheduledAction] execute fail: %s", type(e).__name__)
        except Exception as e:
            logger.debug("[ScheduledAction] poll fail: %s", type(e).__name__)
        return executed

    def cleanup_old(self, max_age_days: int = 7) -> int:
        """Delete done/expired actions older than max_age_days. Returns count deleted."""
        if not self.db:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        deleted = 0
        try:
            users_ref = self.db.collection("users").limit(200).stream()
            for user_doc in users_ref:
                actions = user_doc.reference.collection("scheduled_actions")\
                    .where("created_at", "<=", cutoff).limit(20).stream()
                for doc in actions:
                    doc.reference.delete()
                    deleted += 1
        except Exception:
            pass
        return deleted


# ── Singleton ─────────────────────────────────────────────────────────
_store: ScheduledActionStore | None = None


def get_store() -> ScheduledActionStore:
    global _store
    if _store is None:
        _store = ScheduledActionStore()
    return _store
