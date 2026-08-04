"""Bot self-memory: track what 小柠 promised so she can follow through.

Extraction: scan last assistant message in conversation history for commitment
patterns ("帮你查", "下次提醒你", etc.) → store in Firestore.

Injection: when the user follows up on an old promise, inject pending commitments
into system prompt so 小柠 knows what she promised.

Fulfillment: when user references previous commitments ("你上次说..."), auto-resolve.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
try:
    from xiaoning_runtime import is_weixin_private, private_user_key
except ImportError:
    from data.plugins.xiaoning_runtime import is_weixin_private, private_user_key

try:
    from google.cloud import firestore
    from google.cloud.firestore import Client as FirestoreClient
except ImportError:
    firestore = None
    FirestoreClient = None

FIRESTORE_PROJECT = "solar-modem-496213-f5"
FIRESTORE_DATABASE = "qqbot"
COMMITMENT_EXPIRY_DAYS = 7
MAX_ACTIVE_COMMITMENTS = 3
INJECTION_MARKER = "【未兑现的约定】"

# ── Regex: common Chinese commitment phrases ──────────────────────
# ponytail: regex covers ~80% of commitment patterns; the remaining
# 20% (complex/indirect promises) aren't worth LLM extraction cost.
_COMMITMENT_PATTERNS: list[tuple[str, str]] = [
    (r"帮你查[一下看]|帮你看[一下看]|帮你问[一下看]|帮你找[一下看]", "check"),
    (r"下次[再给帮跟和找][的你]|回头[再给帮跟和找][的你]", "next_time"),
    (r"我记[住了下着]|帮你记[着下]|记下来", "remembered"),
    (r"会提醒你|到时候提醒|记得提醒|准时提醒", "remind"),
    (r"明天[帮给跟和找].{0,10}你|明天再|明天给|明天发", "tomorrow"),
    (r"改天|有空再|找个时间|晚点再|抽空", "someday"),
    (r"(?:到时候|回头|晚点)(?:再|跟[你我]|和[你我])说", "tell_later"),
]

_COMMITMENT_RE = re.compile(
    "|".join(f"(?P<{name}>{pattern})" for pattern, name in _COMMITMENT_PATTERNS),
    re.I,
)

# User messages that signal they're following up on a prior commitment
_FULFILLED_TRIGGER = re.compile(
    r"(?:你(?:上次|之前)说|你答应|你不是说|你说了要|上次你|之前不是|"
    r"上回说|上次不是说|你说[过的]要|还记得.*你说)",
    re.I,
)
_COMMITMENT_FOLLOWUP_TRIGGER = re.compile(
    r"(?:你(?:上次|之前)说|你答应|你不是说|你说了要|上次你|之前不是|"
    r"上回说|上次不是说|你说[过的]要|还记得.*你说|"
    r"(?:刚才|刚刚|上次|之前|前面|那个|这事|这件事).{0,16}"
    r"(?:怎么样|咋样|进度|好了|完成|查到|找到|提醒|结果|发了)|"
    r"(?:进度|结果).{0,8}(?:怎么样|咋样|呢|如何))",
    re.I,
)


def _extract_sentences(text: str) -> list[str]:
    """Extract sentences containing commitment patterns from bot response text."""
    if not text or len(text) < 10:
        return []
    matches = list(_COMMITMENT_RE.finditer(text))
    if not matches:
        return []
    commitments: list[str] = []
    for m in matches:
        start = max(0, m.start() - 25)
        end = min(len(text), m.end() + 35)
        while start > 0 and text[start] not in "。！？\n！？!":
            start -= 1
        if text[start] in "。！？\n！？!":
            start += 1
        while end < len(text) and text[end - 1] not in "。！？\n！？!":
            end += 1
        sentence = text[start:end].strip("。！？\n\t ,，!?～~….")
        if 5 <= len(sentence) <= 200:
            commitments.append(sentence)
    # deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for c in commitments:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique[:3]


class CommitmentTracker:
    """Track 小柠's commitments across conversations."""

    def __init__(self):
        self._db: FirestoreClient | None = None

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
            logger.warning(f"[CommitmentTracker] Firestore 连接失败: {e}")
            return None
        return self._db

    def _ref(self, qq_id: str):
        if not self.db or len(qq_id) < 5:
            return None
        return self.db.collection("users").document(qq_id).collection("commitments")

    # ── Store ──────────────────────────────────────────────────────

    def store(self, qq_id: str, promises: list[str], context_msg: str = "") -> int:
        """Persist extracted commitments. Returns count stored."""
        if not promises or not (ref := self._ref(qq_id)):
            return 0
        now = datetime.now(timezone.utc)
        batch = self.db.batch()
        for promise in promises:
            identity = hashlib.sha256(promise.strip().casefold().encode("utf-8")).hexdigest()[:32]
            batch.set(ref.document(identity), {
                "promise": promise[:200],
                "created_at": now,
                "status": "pending",
                "context_msg": context_msg[:80],
            })
        try:
            batch.commit()
        except Exception as e:
            logger.debug("[CommitmentTracker] store fail: %s", type(e).__name__)
            return 0
        logger.info("[CommitmentTracker] stored %d commitments", len(promises))
        return len(promises)

    # ── Retrieve & auto-expire ─────────────────────────────────────

    def get_pending(self, qq_id: str) -> list[dict]:
        """Return active pending commitments (newest first, max N). Auto-expire old."""
        ref = self._ref(qq_id)
        if ref is None:
            return []
        try:
            docs = ref.where("status", "==", "pending").order_by(
                "created_at", direction="DESCENDING"
            ).stream()
        except Exception as e:
            logger.debug("[CommitmentTracker] query fail: %s", type(e).__name__)
            return []

        now = datetime.now(timezone.utc)
        active: list[dict] = []
        stale_refs: list = []

        for doc in docs:
            data = doc.to_dict()
            created = data.get("created_at")
            if created is not None:
                created_utc = created.replace(tzinfo=timezone.utc)
                if (now - created_utc).days >= COMMITMENT_EXPIRY_DAYS:
                    stale_refs.append(doc.reference)
                    continue
            active.append({**data, "doc_id": doc.id})

        # Batch-expire
        if stale_refs:
            batch = self.db.batch()
            for r in stale_refs:
                batch.update(r, {"status": "expired"})
            try:
                batch.commit()
            except Exception:
                pass

        return active[:MAX_ACTIVE_COMMITMENTS]

    # ── Resolve ────────────────────────────────────────────────────

    def mark_fulfilled(
        self, qq_id: str, doc_ids: tuple[str, ...] = (), evidence: str = ""
    ) -> int:
        """Resolve only explicitly selected promises with execution evidence."""
        selected = {str(item) for item in doc_ids if str(item)}
        if not selected or not str(evidence or "").strip():
            return 0
        ref = self._ref(qq_id)
        if ref is None:
            return 0
        pending = self.get_pending(qq_id)
        if not pending:
            return 0

        now = datetime.now(timezone.utc)
        batch = self.db.batch()
        matched = [c for c in pending if c.get("doc_id") in selected]
        if not matched:
            return 0
        for c in matched:
            batch.update(ref.document(c["doc_id"]), {
                "status": "fulfilled",
                "fulfilled_at": now,
                "evidence": str(evidence)[:200],
            })
        try:
            batch.commit()
        except Exception:
            return 0
        logger.info("[CommitmentTracker] fulfilled %d commitments", len(matched))
        return len(matched)

    # ── Injection ──────────────────────────────────────────────────

    def build_injection(self, qq_id: str) -> str:
        """Build system-prompt block for pending commitments, or empty string."""
        pending = self.get_pending(qq_id)
        if not pending:
            return ""

        lines: list[str] = []
        now = datetime.now(timezone.utc)
        for c in pending:
            created = c.get("created_at")
            time_str = ""
            if created:
                created_utc = created.replace(tzinfo=timezone.utc)
                days = (now - created_utc).days
                time_str = "刚才" if days == 0 else "昨天" if days == 1 else f"{days}天前"
            lines.append(f"- {c['promise']}（{time_str}）")

        return (
            f"\n\n{INJECTION_MARKER}\n"
            "你之前答应过此用户：\n"
            + "\n".join(lines)
            + "\n这些都仍是待办，不是完成记录。用户追问、说“你答应过”或催进度不等于兑现；"
            "只有真实执行和交付证据才能标记完成。相关时如实跟进，不能声称正在后台处理。"
        )


# ── Singleton ─────────────────────────────────────────────────────
_tracker: CommitmentTracker | None = None


def get_tracker() -> CommitmentTracker:
    global _tracker
    if _tracker is None:
        _tracker = CommitmentTracker()
    return _tracker


# ── AstrBot hooks (called from friend_core/main.py) ────────────────

async def on_llm_request_extract(event: AstrMessageEvent, req) -> None:
    """Extract commitments from last assistant message in conversation history.

    Runs at priority -3 (after persona injection at -5, before memory at -10).
    Scans the request's message history for the last bot response,
    extracts commitment sentences, and stores them.
    Also checks for time-based promises → schedules real Firestore-backed reminders.
    """
    tracker = get_tracker()
    sender = private_user_key(event)
    if len(sender) < 5:
        return

    # A follow-up is evidence that a promise is still pending, never that it
    # was fulfilled.  Keep the trigger only for intent recognition/injection.
    user_text = str(getattr(event, "get_message_str", lambda: "")() or "")
    following_up = bool(user_text and _COMMITMENT_FOLLOWUP_TRIGGER.search(user_text))

    # Scan last assistant message in history for new commitments
    messages = getattr(req, "messages", None)
    if not messages:
        return

    last_assistant_text = ""
    context_msg = ""
    for msg in reversed(messages):
        role = msg.get("role", "") if isinstance(msg, dict) else getattr(msg, "role", "")
        content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
        if role == "assistant":
            last_assistant_text = content if isinstance(content, str) else str(content)
            break
        elif role == "user" and not context_msg:
            context_msg = content if isinstance(content, str) else str(content or "")

    if not last_assistant_text:
        return

    promises = _extract_sentences(last_assistant_text)
    if promises:
        tracker.store(sender, promises, context_msg[:80])
    elif following_up:
        logger.info("[CommitmentTracker] checking overdue commitments")

    # ── Time-based promise → scheduled action (Google ecosystem) ──
    # Personal WeChat delivery is tied to a live context token.  Do not create
    # a durable reminder until that transport has recipient-visible proof.
    if is_weixin_private(event):
        return
    try:
        from .scheduled_actions import extract_scheduled_action, get_store
        result = extract_scheduled_action(context_msg, last_assistant_text)
        if result:
            fire_at, action_text = result
            store = get_store()
            store.schedule(sender, fire_at, action_text, context_msg[:200])
    except Exception:
        pass  # don't break commitment extraction for scheduling failures


async def on_llm_request_inject(event: AstrMessageEvent, req) -> None:
    """Inject pending commitments into system prompt.

    Runs at priority -6 (after persona -5, before memory -10).
    """
    tracker = get_tracker()
    sender = private_user_key(event)
    if len(sender) < 5:
        return
    user_text = str(getattr(event, "get_message_str", lambda: "")() or "")
    if not (user_text and _COMMITMENT_FOLLOWUP_TRIGGER.search(user_text)):
        return

    sp = str(getattr(req, "system_prompt", "") or "")
    # Dedup: remove old injection if present
    if INJECTION_MARKER in sp:
        idx = sp.find(INJECTION_MARKER)
        sp = sp[:idx].strip()
    block = tracker.build_injection(sender)
    if block:
        req.system_prompt = (sp + block).strip()
