"""Privacy-safe, non-authoritative memory for the Xiaoning experience layer."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path

from .agent_core import redact_sensitive_text
from .encrypted_payload_store import _dpapi, _harden_private_path, _windows_user_sid


class MemoryKind(str, Enum):
    PREFERENCE = "preference"
    RELATIONSHIP = "relationship"
    TASK = "task"


@dataclass(frozen=True)
class MemoryEntry:
    kind: MemoryKind
    key: str
    value: str


@dataclass(frozen=True)
class MemoryDecision:
    allowed: bool
    code: str


_KEYS_BY_KIND = {
    MemoryKind.PREFERENCE: {
        "reply_length",
        "tone",
        "preferred_backend",
        "voice_enabled",
    },
    MemoryKind.RELATIONSHIP: {"relationship_fact"},
    MemoryKind.TASK: {"task_outcome"},
}
_SENSITIVE_WORDS = re.compile(
    r"私聊|聊天记录|病历|健康|身份证|银行卡|密码|凭据|浏览器|通讯录",
    re.I,
)
_GENERIC_SECRET = re.compile(
    r"(?i)\b(?:token|credential|cookie|session)\s*[:=]\s*[^\s,;，。；]+"
)


def validate_memory_entry(
    entry: MemoryEntry,
    *,
    explicit_request: bool = False,
    is_pro: bool = False,
) -> MemoryDecision:
    try:
        kind = MemoryKind(entry.kind)
    except (TypeError, ValueError):
        return MemoryDecision(False, "kind_not_allowed")
    key = str(entry.key or "").strip()
    value = str(entry.value or "").strip()
    if key not in _KEYS_BY_KIND[kind]:
        return MemoryDecision(False, "field_not_allowed")
    if not value or len(value) > 120:
        return MemoryDecision(False, "value_invalid")
    if kind is MemoryKind.RELATIONSHIP and not (explicit_request and is_pro):
        return MemoryDecision(False, "explicit_pro_request_required")
    if (
        redact_sensitive_text(value) != value
        or _GENERIC_SECRET.search(value)
        or _SENSITIVE_WORDS.search(value)
    ):
        return MemoryDecision(False, "sensitive_memory")
    return MemoryDecision(True, "allowed")


class ExperienceMemoryStore:
    """DPAPI ciphertext keyed by anonymous owner HMAC and fixed field name."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        _harden_private_path(self.root)
        sid = _windows_user_sid()
        self._index_key = hashlib.sha256(
            f"xiaoning-experience-memory:{sid}".encode("utf-8")
        ).digest()

    def _path(self, owner: str, key: str) -> Path:
        owner_text = str(owner or "").strip()
        key_text = str(key or "").strip()
        if not owner_text or not key_text:
            raise ValueError("记忆索引无效")
        digest = hmac.new(
            self._index_key,
            f"{owner_text}:{key_text}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return self.root / f"{digest}.bin"

    @staticmethod
    def _entropy(path: Path) -> bytes:
        return hashlib.sha256(
            f"xiaoning-experience-memory-v1:{path.stem}".encode("ascii")
        ).digest()

    def put(
        self,
        *,
        owner: str,
        entry: MemoryEntry,
        explicit_request: bool = False,
        is_pro: bool = False,
    ) -> None:
        decision = validate_memory_entry(
            entry, explicit_request=explicit_request, is_pro=is_pro
        )
        if not decision.allowed:
            raise ValueError(decision.code)
        path = self._path(owner, entry.key)
        payload = json.dumps(
            {**asdict(entry), "kind": MemoryKind(entry.kind).value},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        ciphertext = _dpapi(payload, self._entropy(path), protect=True)
        temporary = self.root / f".{path.stem}.{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_bytes(ciphertext)
            _harden_private_path(self.root, (temporary,))
            os.replace(temporary, path)
            _harden_private_path(self.root, (path,))
        finally:
            temporary.unlink(missing_ok=True)

    def get(self, owner: str, key: str) -> MemoryEntry | None:
        path = self._path(owner, key)
        if not path.is_file():
            return None
        plaintext = _dpapi(
            path.read_bytes(), self._entropy(path), protect=False
        )
        data = json.loads(plaintext.decode("utf-8"))
        entry = MemoryEntry(
            MemoryKind(data["kind"]), str(data["key"]), str(data["value"])
        )
        if entry.key != str(key):
            raise ValueError("记忆索引不匹配")
        return entry

