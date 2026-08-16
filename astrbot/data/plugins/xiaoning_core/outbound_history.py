"""Safe history write-back for QQ messages sent outside AstrBot's send pipeline.

Normal proactive messages must continue to use ``conversation_manager``.  This
module exists for delivery tools that have already obtained recipient-visible
OneBot confirmation and would otherwise leave the next LLM turn unaware of what
Xiaoning just said.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


_UMO_RE = re.compile(
    r"^[A-Za-z0-9_.-]+:(?:FriendMessage|PrivateMessage|GroupMessage):[A-Za-z0-9_.-]+$"
)
_HISTORY_MARKER = "[小柠主动消息记录]"


@dataclass(frozen=True)
class OutboundHistoryResult:
    status: str
    conversation_id: str = ""
    delivery_key: str = ""


def _selected_conversation_id(connection: sqlite3.Connection, umo: str) -> str:
    row = connection.execute(
        """SELECT value FROM preferences
           WHERE scope='umo' AND scope_id=? AND "key"='sel_conv_id'""",
        (umo,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no selected conversation for exact session: {umo}")
    try:
        decoded = json.loads(row["value"])
        conversation_id = str(decoded.get("val", "")).strip()
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("selected conversation preference is malformed") from exc
    if not conversation_id:
        raise ValueError("selected conversation preference is empty")
    return conversation_id


def _message_text(message: object) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        str(part.get("text", ""))
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    )


def record_confirmed_outbound(
    db_path: str | Path,
    *,
    umo: str,
    text: str,
    delivery_id: str,
    confirmed: bool,
) -> OutboundHistoryResult:
    """Append one confirmed outbound text to exactly one selected conversation.

    The delivery receipt and history append share one ``BEGIN IMMEDIATE``
    transaction, making retries idempotent.  An exact conversation/session
    ownership check fails closed instead of falling back to another user's row.
    """

    if not confirmed:
        return OutboundHistoryResult(status="unconfirmed")
    normalized_umo = str(umo or "").strip()
    normalized_text = str(text or "").strip()
    normalized_delivery_id = str(delivery_id or "").strip()
    if not _UMO_RE.fullmatch(normalized_umo):
        raise ValueError("unsupported or malformed unified message origin")
    if not normalized_text or len(normalized_text) > 12000:
        raise ValueError("outbound text is empty or too long")
    if not normalized_delivery_id or len(normalized_delivery_id) > 160:
        raise ValueError("delivery id is empty or too long")

    delivery_key = hashlib.sha256(
        f"{normalized_umo}\0{normalized_delivery_id}".encode(
            "utf-8", errors="replace"
        )
    ).hexdigest()
    content_digest = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
    database = Path(db_path)
    if not database.is_file():
        raise FileNotFoundError(database)

    with closing(sqlite3.connect(database, timeout=8, isolation_level=None)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=8000")
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS xiaoning_outbound_history_receipts (
                       delivery_key TEXT PRIMARY KEY,
                       conversation_id TEXT NOT NULL,
                       content_digest TEXT NOT NULL,
                       created_at REAL NOT NULL
                   )"""
            )
            existing = connection.execute(
                """SELECT conversation_id, content_digest
                   FROM xiaoning_outbound_history_receipts
                   WHERE delivery_key=?""",
                (delivery_key,),
            ).fetchone()
            if existing is not None:
                if str(existing["content_digest"]) != content_digest:
                    raise ValueError("delivery id was reused with different content")
                connection.execute("COMMIT")
                return OutboundHistoryResult(
                    status="duplicate",
                    conversation_id=str(existing["conversation_id"]),
                    delivery_key=delivery_key,
                )

            conversation_id = _selected_conversation_id(connection, normalized_umo)
            row = connection.execute(
                """SELECT user_id, content FROM conversations
                   WHERE conversation_id=?""",
                (conversation_id,),
            ).fetchone()
            if row is None:
                raise LookupError("selected conversation does not exist")
            if str(row["user_id"]) != normalized_umo:
                raise ValueError("selected conversation belongs to another session")
            try:
                history = json.loads(row["content"] or "[]")
            except (TypeError, ValueError) as exc:
                raise ValueError("conversation history is malformed") from exc
            if not isinstance(history, list):
                raise ValueError("conversation history is not a list")

            # Older repair tools may have written the content before receipt
            # tracking existed.  Adopt that exact assistant message instead of
            # duplicating it at the end of the conversation.
            if any(
                isinstance(message, dict)
                and message.get("role") == "assistant"
                and hashlib.sha256(_message_text(message).encode("utf-8")).hexdigest()
                == content_digest
                for message in history
            ):
                connection.execute(
                    """INSERT INTO xiaoning_outbound_history_receipts(
                           delivery_key, conversation_id, content_digest, created_at
                       ) VALUES (?, ?, ?, ?)""",
                    (delivery_key, conversation_id, content_digest, time.time()),
                )
                connection.execute("COMMIT")
                return OutboundHistoryResult(
                    status="duplicate_content",
                    conversation_id=conversation_id,
                    delivery_key=delivery_key,
                )

            history.extend(
                (
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": _HISTORY_MARKER}],
                    },
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": normalized_text}],
                    },
                )
            )
            connection.execute(
                """UPDATE conversations
                   SET content=?, updated_at=CURRENT_TIMESTAMP
                   WHERE conversation_id=? AND user_id=?""",
                (
                    json.dumps(history, ensure_ascii=False, separators=(",", ":")),
                    conversation_id,
                    normalized_umo,
                ),
            )
            connection.execute(
                """INSERT INTO xiaoning_outbound_history_receipts(
                       delivery_key, conversation_id, content_digest, created_at
                   ) VALUES (?, ?, ?, ?)""",
                (delivery_key, conversation_id, content_digest, time.time()),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    return OutboundHistoryResult(
        status="recorded",
        conversation_id=conversation_id,
        delivery_key=delivery_key,
    )
