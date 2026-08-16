"""Consent-gated, local-first personal memory with encrypted SQLite payloads."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .models import (
    CandidateStatus,
    OpenLoop,
    OpenLoopStatus,
    PersonaEvent,
    ProactiveCandidate,
    ProactiveMode,
    RelationshipProfile,
)

try:
    from claude_code_agent.encrypted_payload_store import _dpapi, _harden_private_path
except ImportError:
    from data.plugins.claude_code_agent.encrypted_payload_store import (
        _dpapi,
        _harden_private_path,
    )


_MAGIC = b"XNM1"
_KEY_MAGIC = b"XNK1"
_ALLOWED_KINDS = {"preference", "relationship", "commitment", "task_result"}
_ALLOWED_SOURCES = {"user_quote", "verified_task"}
_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]{2,}|[\u3400-\u9fff]")
_TIME_RE = re.compile(r"(?:20\d{2}|\d{1,2}[月日点]|今天|明天|后天|周[一二三四五六日天])")
_ENTITY_RE = re.compile(r"(?:[A-Z][A-Za-z0-9_-]{1,}|\d{2,})")


class MemoryCipher(Protocol):
    @property
    def search_key(self) -> bytes: ...

    def encrypt(self, value: bytes, *, context: str) -> bytes: ...

    def decrypt(self, value: bytes, *, context: str) -> bytes: ...


class DpapiMemoryCipher:
    """Current-Windows-user encryption; no plaintext fallback is allowed."""

    def __init__(self, root: Path):
        if os.name != "nt":
            raise RuntimeError("本地长期记忆需要 Windows DPAPI")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        _harden_private_path(self.root)
        self._key_path = self.root / "search-key.bin"
        self._search_key = self._load_or_create_search_key()

    @staticmethod
    def _entropy(context: str) -> bytes:
        return hashlib.sha256(f"xiaoning-memory-v1:{context}".encode("utf-8")).digest()

    def _load_or_create_search_key(self) -> bytes:
        if self._key_path.exists():
            raw = self._key_path.read_bytes()
            if not raw.startswith(_KEY_MAGIC):
                raise RuntimeError("本地记忆检索密钥格式无效")
            key = _dpapi(
                raw[len(_KEY_MAGIC) :], self._entropy("search-key"), protect=False
            )
            if len(key) != 32:
                raise RuntimeError("本地记忆检索密钥长度无效")
            return key
        key = secrets.token_bytes(32)
        encrypted = _dpapi(key, self._entropy("search-key"), protect=True)
        temporary = self.root / f".search-key.{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_bytes(_KEY_MAGIC + encrypted)
            _harden_private_path(self.root, (temporary,))
            os.replace(temporary, self._key_path)
            _harden_private_path(self.root, (self._key_path,))
        finally:
            temporary.unlink(missing_ok=True)
        return key

    @property
    def search_key(self) -> bytes:
        return self._search_key

    def encrypt(self, value: bytes, *, context: str) -> bytes:
        return _MAGIC + _dpapi(value, self._entropy(context), protect=True)

    def decrypt(self, value: bytes, *, context: str) -> bytes:
        if not bytes(value).startswith(_MAGIC):
            raise ValueError("本地记忆密文格式无效")
        return _dpapi(bytes(value)[len(_MAGIC) :], self._entropy(context), protect=False)


@dataclass(frozen=True)
class ConsentSnapshot:
    memory: bool = False
    proactive: bool = False


@dataclass(frozen=True)
class MemoryRecord:
    memory_id: str
    kind: str
    value: str
    source_type: str
    source_quote: str
    source_ref: str
    confidence: float
    created_at: float
    valid_from: float
    supersedes_id: str = ""
    score: float = 0.0


@dataclass(frozen=True)
class SyncEvent:
    event_id: str
    operation: str
    aggregate_id: str
    payload: dict
    idempotency_key: str
    attempts: int


def _plain_tokens(value: str) -> set[str]:
    normalized = str(value or "").casefold()
    base = _TOKEN_RE.findall(normalized)
    cjk = "".join(ch for ch in normalized if "\u3400" <= ch <= "\u9fff")
    bigrams = [cjk[index : index + 2] for index in range(max(0, len(cjk) - 1))]
    return {item for item in (*base, *bigrams) if item}


class MemoryGateway:
    def __init__(self, path: Path, *, cipher: MemoryCipher | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cipher = cipher or DpapiMemoryCipher(self.path.parent)
        self._initialize()
        try:
            _harden_private_path(self.path.parent, (self.path,))
        except Exception:
            # The payload remains DPAPI encrypted even if an ACL hardening retry fails.
            pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=8)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS consents (
                    scope_key TEXT PRIMARY KEY,
                    memory_enabled INTEGER NOT NULL DEFAULT 0,
                    proactive_enabled INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_items (
                    memory_id TEXT PRIMARY KEY,
                    scope_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload BLOB NOT NULL,
                    source_digest TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    valid_from REAL NOT NULL,
                    valid_to REAL,
                    supersedes_id TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_scope_valid
                    ON memory_items(scope_key, valid_to, created_at DESC);
                CREATE TABLE IF NOT EXISTS deletion_confirmations (
                    scope_key TEXT PRIMARY KEY,
                    token_digest TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sync_outbox (
                    event_id TEXT PRIMARY KEY,
                    scope_key TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    aggregate_id TEXT NOT NULL,
                    payload BLOB NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sync_outbox_pending
                    ON sync_outbox(status, created_at);
                CREATE TABLE IF NOT EXISTS relationship_profiles (
                    scope_key TEXT PRIMARY KEY,
                    payload BLOB NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS open_loops (
                    loop_id TEXT PRIMARY KEY,
                    scope_key TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    not_before REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    payload BLOB NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(scope_key, dedupe_key)
                );
                CREATE INDEX IF NOT EXISTS idx_open_loops_due
                    ON open_loops(scope_key, status, not_before, expires_at);
                CREATE TABLE IF NOT EXISTS proactive_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    scope_key TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    score REAL NOT NULL,
                    not_before REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    payload BLOB NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_proactive_candidates_due
                    ON proactive_candidates(scope_key, status, not_before, expires_at);
                CREATE TABLE IF NOT EXISTS proactive_sends (
                    send_id TEXT PRIMARY KEY,
                    scope_key TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    sent_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_proactive_sends_scope_time
                    ON proactive_sends(scope_key, sent_at DESC);
                CREATE TABLE IF NOT EXISTS persona_events (
                    event_day TEXT PRIMARY KEY,
                    payload BLOB NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS engagement_events (
                    event_id TEXT PRIMARY KEY,
                    scope_key TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_day TEXT NOT NULL,
                    attributes TEXT NOT NULL,
                    occurred_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_engagement_event_type_day
                    ON engagement_events(event_type, event_day);
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_search USING fts5(
                    memory_id UNINDEXED,
                    scope_key UNINDEXED,
                    terms,
                    tokenize='unicode61'
                );
                """
            )

    def _scope_key(self, scope: str) -> str:
        return hmac.new(
            self.cipher.search_key,
            f"scope:{str(scope)}".encode("utf-8", errors="replace"),
            hashlib.sha256,
        ).hexdigest()

    def _term(self, token: str) -> str:
        return hmac.new(
            self.cipher.search_key,
            f"term:{token}".encode("utf-8", errors="replace"),
            hashlib.sha256,
        ).hexdigest()[:32]

    def _search_terms(self, value: str) -> str:
        return " ".join(sorted(self._term(token) for token in _plain_tokens(value)))

    def get_consent(self, scope: str) -> ConsentSnapshot:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT memory_enabled, proactive_enabled FROM consents WHERE scope_key = ?",
                (self._scope_key(scope),),
            ).fetchone()
        if row is None:
            return ConsentSnapshot()
        return ConsentSnapshot(bool(row["memory_enabled"]), bool(row["proactive_enabled"]))

    def set_consent(
        self,
        scope: str,
        *,
        memory: bool | None = None,
        proactive: bool | None = None,
        now: float | None = None,
    ) -> ConsentSnapshot:
        timestamp = time.time() if now is None else float(now)
        previous = self.get_consent(scope)
        updated = ConsentSnapshot(
            previous.memory if memory is None else bool(memory),
            previous.proactive if proactive is None else bool(proactive),
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT INTO consents(scope_key, memory_enabled, proactive_enabled, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(scope_key) DO UPDATE SET
                     memory_enabled=excluded.memory_enabled,
                     proactive_enabled=excluded.proactive_enabled,
                     updated_at=excluded.updated_at""",
                (
                    self._scope_key(scope), int(updated.memory),
                    int(updated.proactive), timestamp,
                ),
            )
        return updated

    def add_memory(
        self,
        scope: str,
        *,
        kind: str,
        value: str,
        source_type: str,
        source_quote: str,
        source_ref: str = "",
        confidence: float = 1.0,
        supersedes_id: str = "",
        now: float | None = None,
    ) -> MemoryRecord:
        if not self.get_consent(scope).memory:
            raise PermissionError("长期记忆尚未获得用户授权")
        normalized_kind = str(kind).strip().casefold()
        if normalized_kind not in _ALLOWED_KINDS:
            raise ValueError("不允许的记忆类型")
        normalized_source = str(source_type).strip().casefold()
        if normalized_source not in _ALLOWED_SOURCES:
            raise ValueError("长期记忆必须来自用户原话或已验证任务结果")
        clean_value = str(value or "").strip()
        clean_quote = str(source_quote or "").strip()
        if not clean_value or len(clean_value) > 600:
            raise ValueError("记忆内容为空或过长")
        if not clean_quote or len(clean_quote) > 1200:
            raise ValueError("记忆来源为空或过长")
        if normalized_source == "user_quote" and clean_value not in clean_quote:
            raise ValueError("用户记忆必须可在用户原话中逐字找到")
        timestamp = time.time() if now is None else float(now)
        memory_id = uuid.uuid4().hex
        scope_key = self._scope_key(scope)
        payload = {
            "value": clean_value,
            "source_type": normalized_source,
            "source_quote": clean_quote,
            "source_ref": str(source_ref or "")[:240],
        }
        ciphertext = self.cipher.encrypt(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            context=f"memory:{memory_id}",
        )
        source_digest = hashlib.sha256(clean_quote.encode("utf-8")).hexdigest()
        with closing(self._connect()) as connection, connection:
            if supersedes_id:
                old = connection.execute(
                    "SELECT memory_id FROM memory_items WHERE memory_id=? AND scope_key=? AND valid_to IS NULL",
                    (supersedes_id, scope_key),
                ).fetchone()
                if old is None:
                    raise ValueError("要更正的记忆不存在或不属于当前用户")
                connection.execute(
                    "UPDATE memory_items SET valid_to=? WHERE memory_id=?",
                    (timestamp, supersedes_id),
                )
            connection.execute(
                """INSERT INTO memory_items(
                    memory_id, scope_key, kind, payload, source_digest, confidence,
                    valid_from, valid_to, supersedes_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
                (
                    memory_id, scope_key, normalized_kind, ciphertext, source_digest,
                    max(0.0, min(1.0, float(confidence))), timestamp,
                    str(supersedes_id or ""), timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO memory_search(memory_id, scope_key, terms) VALUES (?, ?, ?)",
                (memory_id, scope_key, self._search_terms(clean_value)),
            )
            self._enqueue(
                connection,
                scope_key=scope_key,
                operation="upsert",
                aggregate_id=memory_id,
                payload={
                    "user_scope": str(scope),
                    "value": clean_value,
                    "kind": normalized_kind,
                    "source_type": normalized_source,
                    "source_digest": source_digest,
                    "source_ref": str(source_ref or "")[:240],
                    "supersedes_id": str(supersedes_id or ""),
                },
                idempotency_key=f"memory-upsert:{memory_id}",
                now=timestamp,
            )
        return MemoryRecord(
            memory_id=memory_id,
            kind=normalized_kind,
            value=clean_value,
            source_type=normalized_source,
            source_quote=clean_quote,
            source_ref=str(source_ref or "")[:240],
            confidence=max(0.0, min(1.0, float(confidence))),
            created_at=timestamp,
            valid_from=timestamp,
            supersedes_id=str(supersedes_id or ""),
        )

    def _decode(self, row: sqlite3.Row, *, score: float = 0.0) -> MemoryRecord:
        payload = json.loads(
            self.cipher.decrypt(
                bytes(row["payload"]), context=f"memory:{row['memory_id']}"
            ).decode("utf-8")
        )
        return MemoryRecord(
            memory_id=str(row["memory_id"]),
            kind=str(row["kind"]),
            value=str(payload["value"]),
            source_type=str(payload["source_type"]),
            source_quote=str(payload["source_quote"]),
            source_ref=str(payload.get("source_ref", "")),
            confidence=float(row["confidence"]),
            created_at=float(row["created_at"]),
            valid_from=float(row["valid_from"]),
            supersedes_id=str(row["supersedes_id"] or ""),
            score=float(score),
        )

    def list_memories(self, scope: str, *, limit: int = 100) -> list[MemoryRecord]:
        if not self.get_consent(scope).memory:
            return []
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT * FROM memory_items
                   WHERE scope_key=? AND valid_to IS NULL
                   ORDER BY created_at DESC LIMIT ?""",
                (self._scope_key(scope), max(1, min(100, int(limit)))),
            ).fetchall()
        return [self._decode(row) for row in rows]

    def resolve_memory_id(self, scope: str, prefix: str) -> str:
        normalized = str(prefix or "").strip().casefold()
        if len(normalized) < 6 or not re.fullmatch(r"[0-9a-f]+", normalized):
            raise ValueError("记忆编号至少需要前 6 位")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT memory_id FROM memory_items
                   WHERE scope_key=? AND valid_to IS NULL AND memory_id LIKE ?
                   LIMIT 2""",
                (self._scope_key(scope), f"{normalized}%"),
            ).fetchall()
        if len(rows) != 1:
            raise ValueError("记忆编号不存在或不唯一")
        return str(rows[0][0])

    def delete_memory(
        self, scope: str, prefix: str, *, now: float | None = None
    ) -> str:
        memory_id = self.resolve_memory_id(scope, prefix)
        timestamp = time.time() if now is None else float(now)
        scope_key = self._scope_key(scope)
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM memory_search WHERE memory_id=?", (memory_id,))
            connection.execute(
                "DELETE FROM memory_items WHERE memory_id=? AND scope_key=?",
                (memory_id, scope_key),
            )
            self._enqueue(
                connection,
                scope_key=scope_key,
                operation="delete",
                aggregate_id=memory_id,
                payload={"user_scope": str(scope), "memory_id": memory_id},
                idempotency_key=f"memory-delete:{memory_id}",
                now=timestamp,
            )
        return memory_id

    def recall(
        self,
        scope: str,
        query: str,
        *,
        limit: int = 8,
        now: float | None = None,
        semantic_scorer: Callable[[str, str], float] | None = None,
    ) -> list[MemoryRecord]:
        if not self.get_consent(scope).memory:
            return []
        scope_key = self._scope_key(scope)
        query_tokens = _plain_tokens(query)
        hashed = [self._term(token) for token in sorted(query_tokens)]
        with closing(self._connect()) as connection:
            if hashed:
                fts_query = " OR ".join(f'"{token}"' for token in hashed)
                rows = connection.execute(
                    """SELECT m.* FROM memory_search s
                       JOIN memory_items m ON m.memory_id=s.memory_id
                       WHERE s.memory_search MATCH ? AND s.scope_key=? AND m.valid_to IS NULL
                       ORDER BY bm25(memory_search) LIMIT 40""",
                    (fts_query, scope_key),
                ).fetchall()
            else:
                rows = []
            if not rows:
                rows = connection.execute(
                    """SELECT * FROM memory_items WHERE scope_key=? AND valid_to IS NULL
                       ORDER BY created_at DESC LIMIT 40""",
                    (scope_key,),
                ).fetchall()
        timestamp = time.time() if now is None else float(now)
        query_entities = set(_ENTITY_RE.findall(str(query or "")))
        query_times = set(_TIME_RE.findall(str(query or "")))
        ranked: list[MemoryRecord] = []
        for row in rows:
            record = self._decode(row)
            memory_tokens = _plain_tokens(record.value)
            union = query_tokens | memory_tokens
            keyword = len(query_tokens & memory_tokens) / max(1, len(union))
            entity = 1.0 if query_entities & set(_ENTITY_RE.findall(record.value)) else 0.0
            temporal = 1.0 if query_times & set(_TIME_RE.findall(record.value)) else 0.0
            age_days = max(0.0, timestamp - record.created_at) / 86400
            recency = math.exp(-age_days / 180)
            semantic = 0.0
            if semantic_scorer is not None:
                try:
                    semantic = max(0.0, min(1.0, float(semantic_scorer(query, record.value))))
                except Exception:
                    semantic = 0.0
            score = (
                0.35 * keyword + 0.20 * entity + 0.15 * temporal
                + 0.10 * recency + 0.10 * record.confidence + 0.10 * semantic
            )
            ranked.append(MemoryRecord(**{**record.__dict__, "score": score}))
        ranked.sort(key=lambda item: (-item.score, -item.created_at, item.memory_id))
        return ranked[: max(1, min(8, int(limit)))]

    def request_delete_all(
        self, scope: str, *, now: float | None = None, ttl_seconds: int = 300
    ) -> str:
        timestamp = time.time() if now is None else float(now)
        token = f"{secrets.randbelow(1_000_000):06d}"
        digest = hmac.new(
            self.cipher.search_key,
            f"delete:{self._scope_key(scope)}:{token}".encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT INTO deletion_confirmations(scope_key, token_digest, expires_at)
                   VALUES (?, ?, ?) ON CONFLICT(scope_key) DO UPDATE SET
                   token_digest=excluded.token_digest, expires_at=excluded.expires_at""",
                (self._scope_key(scope), digest, timestamp + max(60, int(ttl_seconds))),
            )
        return token

    def confirm_delete_all(
        self, scope: str, token: str, *, now: float | None = None
    ) -> bool:
        timestamp = time.time() if now is None else float(now)
        scope_key = self._scope_key(scope)
        candidate = hmac.new(
            self.cipher.search_key,
            f"delete:{scope_key}:{str(token).strip()}".encode("ascii", errors="ignore"),
            hashlib.sha256,
        ).hexdigest()
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT token_digest, expires_at FROM deletion_confirmations WHERE scope_key=?",
                (scope_key,),
            ).fetchone()
            if (
                row is None
                or float(row["expires_at"]) < timestamp
                or not hmac.compare_digest(str(row["token_digest"]), candidate)
            ):
                return False
            ids = [
                str(item[0])
                for item in connection.execute(
                    "SELECT memory_id FROM memory_items WHERE scope_key=?", (scope_key,)
                ).fetchall()
            ]
            connection.execute("DELETE FROM memory_search WHERE scope_key=?", (scope_key,))
            connection.execute("DELETE FROM memory_items WHERE scope_key=?", (scope_key,))
            connection.execute("DELETE FROM deletion_confirmations WHERE scope_key=?", (scope_key,))
            self._enqueue(
                connection,
                scope_key=scope_key,
                operation="delete_all",
                aggregate_id="all",
                payload={"user_scope": str(scope), "memory_ids": ids},
                idempotency_key=f"memory-delete-all:{scope_key}:{int(timestamp)}",
                now=timestamp,
            )
        return True

    def _enqueue(
        self,
        connection: sqlite3.Connection,
        *,
        scope_key: str,
        operation: str,
        aggregate_id: str,
        payload: dict,
        idempotency_key: str,
        now: float,
    ) -> None:
        event_id = uuid.uuid4().hex
        ciphertext = self.cipher.encrypt(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            context=f"outbox:{event_id}",
        )
        connection.execute(
            """INSERT OR IGNORE INTO sync_outbox(
                event_id, scope_key, operation, aggregate_id, payload,
                idempotency_key, status, attempts, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)""",
            (
                event_id, scope_key, operation, aggregate_id, ciphertext,
                idempotency_key, now, now,
            ),
        )

    def pending_sync_count(self) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM sync_outbox WHERE status='pending'"
            ).fetchone()
        return int(row[0])

    def pending_sync(self, *, limit: int = 25) -> list[SyncEvent]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT event_id, operation, aggregate_id, payload,
                          idempotency_key, attempts
                   FROM sync_outbox WHERE status IN ('pending', 'retry')
                   ORDER BY created_at, rowid LIMIT ?""",
                (max(1, min(100, int(limit))),),
            ).fetchall()
        events = []
        for row in rows:
            payload = json.loads(
                self.cipher.decrypt(
                    bytes(row["payload"]), context=f"outbox:{row['event_id']}"
                ).decode("utf-8")
            )
            events.append(
                SyncEvent(
                    event_id=str(row["event_id"]),
                    operation=str(row["operation"]),
                    aggregate_id=str(row["aggregate_id"]),
                    payload=payload,
                    idempotency_key=str(row["idempotency_key"]),
                    attempts=int(row["attempts"]),
                )
            )
        return events

    def _encode_model(self, model, *, context: str) -> bytes:
        return self.cipher.encrypt(
            model.model_dump_json().encode("utf-8"), context=context
        )

    def _decode_model(self, payload: bytes, model_type, *, context: str):
        raw = self.cipher.decrypt(bytes(payload), context=context).decode("utf-8")
        return model_type.model_validate_json(raw)

    def get_relationship_profile(self, scope: str) -> RelationshipProfile:
        scope_key = self._scope_key(scope)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload FROM relationship_profiles WHERE scope_key=?",
                (scope_key,),
            ).fetchone()
        if row is None:
            return RelationshipProfile()
        return self._decode_model(
            row["payload"], RelationshipProfile, context=f"relationship:{scope_key}"
        )

    def _save_relationship_profile(
        self, scope: str, profile: RelationshipProfile, *, now: float
    ) -> RelationshipProfile:
        scope_key = self._scope_key(scope)
        payload = self._encode_model(profile, context=f"relationship:{scope_key}")
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT INTO relationship_profiles(scope_key, payload, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(scope_key) DO UPDATE SET
                     payload=excluded.payload, updated_at=excluded.updated_at""",
                (scope_key, payload, float(now)),
            )
        return profile

    def record_private_turn(
        self, scope: str, text: object, *, now: float | None = None
    ) -> RelationshipProfile:
        from datetime import datetime, timedelta, timezone

        from .relationship import is_meaningful_private_turn

        timestamp = time.time() if now is None else float(now)
        profile = self.get_relationship_profile(scope)
        meaningful = is_meaningful_private_turn(text)
        turns = profile.meaningful_turns + int(meaningful)
        active_dates = list(profile.active_dates)
        local_day = datetime.fromtimestamp(
            timestamp, tz=timezone(timedelta(hours=8))
        ).date().isoformat()
        if meaningful and local_day not in active_dates:
            active_dates.append(local_day)
        activated_now = not profile.activated and turns >= 3
        updated = profile.model_copy(
            update={
                "activated": profile.activated or activated_now,
                "meaningful_turns": turns,
                "active_dates": tuple(active_dates[-400:]),
                "relationship_temperature": min(1.0, turns / 30.0),
                "last_user_at": timestamp,
                "unanswered_proactive": 0 if profile.activated else profile.unanswered_proactive,
                "activation_notice_pending": profile.activation_notice_pending or activated_now,
            }
        )
        self._save_relationship_profile(scope, updated, now=timestamp)
        if activated_now:
            self.set_consent(
                scope,
                memory=True,
                proactive=updated.proactive_mode is not ProactiveMode.PAUSED,
                now=timestamp,
            )
            self.record_engagement(scope, "activated", now=timestamp)
        elif meaningful:
            self.record_engagement(scope, "active_day", now=timestamp)
        return updated

    def consume_activation_notice(self, scope: str, *, now: float | None = None) -> bool:
        timestamp = time.time() if now is None else float(now)
        profile = self.get_relationship_profile(scope)
        if not profile.activation_notice_pending:
            return False
        self._save_relationship_profile(
            scope,
            profile.model_copy(update={"activation_notice_pending": False}),
            now=timestamp,
        )
        return True

    def set_proactive_mode(
        self, scope: str, mode: ProactiveMode, *, now: float | None = None
    ) -> RelationshipProfile:
        timestamp = time.time() if now is None else float(now)
        profile = self.get_relationship_profile(scope).model_copy(
            update={"proactive_mode": mode}
        )
        self._save_relationship_profile(scope, profile, now=timestamp)
        self.set_consent(scope, proactive=mode is not ProactiveMode.PAUSED, now=timestamp)
        if mode is ProactiveMode.PAUSED:
            scope_key = self._scope_key(scope)
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    "UPDATE proactive_candidates SET status='cancelled', updated_at=? WHERE scope_key=? AND status='pending'",
                    (timestamp, scope_key),
                )
        self.record_engagement(scope, f"proactive_{mode.value}", now=timestamp)
        return profile

    def upsert_open_loop(
        self, scope: str, loop: OpenLoop, *, now: float | None = None
    ) -> OpenLoop:
        timestamp = time.time() if now is None else float(now)
        scope_key = self._scope_key(scope)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                "SELECT loop_id FROM open_loops WHERE scope_key=? AND dedupe_key=?",
                (scope_key, loop.dedupe_key),
            ).fetchone()
            stored = loop if existing is None else loop.model_copy(
                update={"loop_id": str(existing["loop_id"])}
            )
            payload = self._encode_model(stored, context=f"open-loop:{stored.loop_id}")
            connection.execute(
                """INSERT INTO open_loops(
                       loop_id, scope_key, dedupe_key, status, not_before, expires_at,
                       payload, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(scope_key, dedupe_key) DO UPDATE SET
                     status=excluded.status, not_before=excluded.not_before,
                     expires_at=excluded.expires_at, payload=excluded.payload,
                     updated_at=excluded.updated_at""",
                (
                    stored.loop_id, scope_key, stored.dedupe_key, stored.status.value,
                    stored.not_before, stored.expires_at, payload, stored.created_at,
                    timestamp,
                ),
            )
        self.record_engagement(scope, "open_loop_created", now=timestamp)
        return stored

    def list_open_loops(
        self, scope: str, *, now: float | None = None, limit: int = 20
    ) -> list[OpenLoop]:
        timestamp = time.time() if now is None else float(now)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT loop_id, payload FROM open_loops
                   WHERE scope_key=? AND status=? AND expires_at>=?
                   ORDER BY created_at DESC LIMIT ?""",
                (
                    self._scope_key(scope), OpenLoopStatus.OPEN.value,
                    timestamp, max(1, min(100, int(limit))),
                ),
            ).fetchall()
        return [
            self._decode_model(
                row["payload"], OpenLoop, context=f"open-loop:{row['loop_id']}"
            )
            for row in rows
        ]

    def delete_open_loops(self, scope: str, *, loop_id: str | None = None) -> int:
        scope_key = self._scope_key(scope)
        with closing(self._connect()) as connection, connection:
            if loop_id:
                cursor = connection.execute(
                    "DELETE FROM open_loops WHERE scope_key=? AND loop_id=?",
                    (scope_key, str(loop_id)),
                )
                candidate_rows = connection.execute(
                    """SELECT candidate_id, payload FROM proactive_candidates
                       WHERE scope_key=? AND status IN ('pending', 'claimed')""",
                    (scope_key,),
                ).fetchall()
                matching_candidate_ids = []
                for row in candidate_rows:
                    try:
                        candidate = self._decode_model(
                            row["payload"],
                            ProactiveCandidate,
                            context=f"candidate:{row['candidate_id']}",
                        )
                    except Exception:
                        continue
                    if candidate.open_loop_id == str(loop_id):
                        matching_candidate_ids.append(str(row["candidate_id"]))
                if matching_candidate_ids:
                    placeholders = ",".join("?" for _ in matching_candidate_ids)
                    connection.execute(
                        f"""UPDATE proactive_candidates
                            SET status='cancelled', updated_at=?
                            WHERE scope_key=? AND candidate_id IN ({placeholders})
                              AND status IN ('pending', 'claimed')""",
                        (time.time(), scope_key, *matching_candidate_ids),
                    )
            else:
                cursor = connection.execute(
                    "DELETE FROM open_loops WHERE scope_key=?", (scope_key,)
                )
            if cursor.rowcount and not loop_id:
                connection.execute(
                    "UPDATE proactive_candidates SET status='cancelled', updated_at=? WHERE scope_key=? AND status IN ('pending', 'claimed')",
                    (time.time(), scope_key),
                )
        return int(cursor.rowcount)

    def get_persona_event(self, day: str) -> PersonaEvent | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload FROM persona_events WHERE event_day=?", (str(day),)
            ).fetchone()
        if row is None:
            return None
        return self._decode_model(
            row["payload"], PersonaEvent, context=f"persona-event:{day}"
        )

    def put_persona_event(
        self, event: PersonaEvent, *, now: float | None = None
    ) -> PersonaEvent:
        timestamp = time.time() if now is None else float(now)
        payload = self._encode_model(event, context=f"persona-event:{event.day}")
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT OR IGNORE INTO persona_events(event_day, payload, created_at) VALUES (?, ?, ?)",
                (event.day, payload, timestamp),
            )
        return self.get_persona_event(event.day) or event

    def record_engagement(
        self,
        scope: str,
        event_type: str,
        *,
        now: float | None = None,
        attributes: dict | None = None,
    ) -> None:
        from datetime import datetime, timedelta, timezone

        timestamp = time.time() if now is None else float(now)
        safe_attributes = {
            str(key)[:80]: value
            for key, value in (attributes or {}).items()
            if isinstance(value, (str, int, float, bool)) and len(str(value)) <= 120
        }
        event_day = datetime.fromtimestamp(
            timestamp, tz=timezone(timedelta(hours=8))
        ).date().isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT INTO engagement_events(
                       event_id, scope_key, event_type, event_day, attributes, occurred_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    uuid.uuid4().hex, self._scope_key(scope), str(event_type)[:80],
                    event_day, json.dumps(safe_attributes, separators=(",", ":")),
                    timestamp,
                ),
            )

    def enqueue_open_loop_candidate(
        self, scope: str, loop: OpenLoop, *, now: float | None = None
    ) -> ProactiveCandidate:
        timestamp = time.time() if now is None else float(now)
        scope_key = self._scope_key(scope)
        idempotency_key = hmac.new(
            self.cipher.search_key,
            f"candidate:{scope_key}:{loop.dedupe_key}".encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT candidate_id, payload FROM proactive_candidates WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        if row is not None:
            return self._decode_model(
                row["payload"],
                ProactiveCandidate,
                context=f"candidate:{row['candidate_id']}",
            )
        candidate = ProactiveCandidate(
            candidate_id=f"candidate-{uuid.uuid4().hex}",
            open_loop_id=loop.loop_id,
            why_now=f"围绕用户原话自然接续：{loop.evidence_summary}",
            source_type="open_loop",
            relevance=0.94,
            timing=0.90,
            novelty=0.90,
            evidence_confidence=0.96,
            not_before=loop.not_before,
            expires_at=loop.expires_at,
            idempotency_key=idempotency_key,
        )
        payload = self._encode_model(
            candidate, context=f"candidate:{candidate.candidate_id}"
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT OR IGNORE INTO proactive_candidates(
                       candidate_id, scope_key, idempotency_key, status, score,
                       not_before, expires_at, payload, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    candidate.candidate_id, scope_key, idempotency_key,
                    CandidateStatus.PENDING.value, candidate.score,
                    candidate.not_before, candidate.expires_at, payload,
                    timestamp, timestamp,
                ),
            )
        return candidate

    def claim_due_candidate(
        self, scope: str, *, now: float | None = None
    ) -> ProactiveCandidate | None:
        from datetime import datetime, timedelta, timezone

        from .relationship import evaluate_proactive_send

        timestamp = time.time() if now is None else float(now)
        scope_key = self._scope_key(scope)
        profile = self.get_relationship_profile(scope)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE proactive_candidates SET status='expired', updated_at=? WHERE scope_key=? AND status='pending' AND expires_at<?",
                (timestamp, scope_key, timestamp),
            )
            rows = connection.execute(
                """SELECT candidate_id, payload FROM proactive_candidates
                   WHERE scope_key=? AND status='pending' AND not_before<=? AND expires_at>=?
                   ORDER BY score DESC, created_at ASC LIMIT 8""",
                (scope_key, timestamp, timestamp),
            ).fetchall()
            sent_rows = connection.execute(
                "SELECT sent_at FROM proactive_sends WHERE scope_key=? AND sent_at>=?",
                (scope_key, timestamp - 8 * 86400),
            ).fetchall()
            sent_at = tuple(float(row["sent_at"]) for row in sent_rows)
            moment = datetime.fromtimestamp(
                timestamp, tz=timezone(timedelta(hours=8))
            )
            for row in rows:
                candidate = self._decode_model(
                    row["payload"],
                    ProactiveCandidate,
                    context=f"candidate:{row['candidate_id']}",
                )
                decision = evaluate_proactive_send(profile, candidate, moment, sent_at)
                if not decision.allowed:
                    continue
                cursor = connection.execute(
                    """UPDATE proactive_candidates SET status='claimed', updated_at=?
                       WHERE candidate_id=? AND scope_key=? AND status='pending'""",
                    (timestamp, candidate.candidate_id, scope_key),
                )
                if cursor.rowcount == 1:
                    return candidate
        return None

    def mark_candidate_sent(
        self, scope: str, candidate_id: str, *, now: float | None = None
    ) -> bool:
        timestamp = time.time() if now is None else float(now)
        scope_key = self._scope_key(scope)
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """UPDATE proactive_candidates SET status='sent', updated_at=?
                   WHERE candidate_id=? AND scope_key=? AND status='claimed'""",
                (timestamp, str(candidate_id), scope_key),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                "INSERT INTO proactive_sends(send_id, scope_key, candidate_id, sent_at) VALUES (?, ?, ?, ?)",
                (uuid.uuid4().hex, scope_key, str(candidate_id), timestamp),
            )
        profile = self.get_relationship_profile(scope).model_copy(
            update={
                "unanswered_proactive": 1,
                "first_proactive_notice_sent": True,
            }
        )
        self._save_relationship_profile(scope, profile, now=timestamp)
        self.record_engagement(scope, "proactive_sent", now=timestamp)
        return True

    def mark_sync(
        self, event_id: str, *, succeeded: bool, now: float | None = None
    ) -> None:
        timestamp = time.time() if now is None else float(now)
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """UPDATE sync_outbox
                   SET status=?, attempts=attempts+1, updated_at=?
                   WHERE event_id=?""",
                ("completed" if succeeded else "retry", timestamp, str(event_id)),
            )
            if cursor.rowcount != 1:
                raise ValueError("同步事件不存在")
