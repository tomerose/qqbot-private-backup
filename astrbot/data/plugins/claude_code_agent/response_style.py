"""Short, privacy-safe task replies for the Xiaoning experience layer."""

from __future__ import annotations

import re

from .agent_core import redact_sensitive_text


_PREFIXES = {
    "started": "已开始。",
    "approval_required": "需要你确认。",
    "completed": "已完成。",
    "failed": "未完成。",
}
_SENTENCE = re.compile(r".*?(?:[。！？!?]+|$)", re.S)
_GENERIC_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(token|credential)(\s*[:=]\s*)[^\s,;，。；]+"
)


def compact_text(text: str, max_sentences: int = 4, max_chars: int = 500) -> str:
    sentence_limit = max(1, min(int(max_sentences), 4))
    char_limit = max(1, min(int(max_chars), 500))
    safe = redact_sensitive_text(str(text or "").strip())
    safe = _GENERIC_SECRET_ASSIGNMENT.sub(r"\1\2[已隐藏]", safe)
    sentences = [match.group(0).strip() for match in _SENTENCE.finditer(safe)]
    compacted = "".join(item for item in sentences[:sentence_limit] if item)
    return compacted[:char_limit].strip()


def format_task_reply(kind: str, evidence: str = "", detail: str = "") -> str:
    normalized = str(kind or "").strip().lower()
    if normalized not in _PREFIXES:
        raise ValueError("不支持的体验层事件")
    body = " ".join(
        part.strip() for part in (evidence, detail) if str(part or "").strip()
    )
    return compact_text(f"{_PREFIXES[normalized]} {body}".strip())
