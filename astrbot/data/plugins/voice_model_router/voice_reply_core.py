"""Pure voice-output intent and privacy-safe spoken-text preparation."""

from __future__ import annotations

import re


_VOICE_REQUEST = re.compile(r"发语音|语音(?:回答|回复|说)|用语音(?:回答|回复|说)")
_CODE_BLOCK = re.compile(r"```[\s\S]*?```|`[^`\r\n]+`")
_WINDOWS_PATH = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:[A-Z]:\\[^\s，。；：！？!?\"'<>|]+)"
)
_SECRET = re.compile(
    r"(?i)\b(?:api[_ -]?key|token|password|secret)\s*[:=]\s*[^\s，。；]+"
)
_BEARER = re.compile(r"(?i)\b(?:authorization\s*:\s*)?bearer\s+\S+")
_SENTENCE_END = re.compile(r"(?<=[。！？!?；;])")


def wants_voice_reply(text: str) -> bool:
    """Voice input alone is insufficient; require an explicit output request."""
    value = str(text or "").strip()
    return bool(value and _VOICE_REQUEST.search(value))


def _spoken_redaction(text: str) -> str:
    value = _CODE_BLOCK.sub("", str(text or ""))
    value = _WINDOWS_PATH.sub("本机文件", value)
    value = _BEARER.sub("敏感信息已隐藏", value)
    value = _SECRET.sub("敏感信息已隐藏", value)
    value = re.sub(r"[\t\r\n ]+", " ", value)
    return value.strip()


def _split_long_sentence(sentence: str, limit: int) -> list[str]:
    return [sentence[index : index + limit] for index in range(0, len(sentence), limit)]


def prepare_spoken_chunks(
    text: str,
    max_chars: int = 600,
    max_chunks: int = 3,
) -> list[str]:
    """Redact and split speech into bounded natural chunks."""
    total_limit = max(1, min(int(max_chars), 600))
    chunk_limit = min(200, max(40, total_limit // 2))
    chunk_count = max(1, min(int(max_chunks), 3))
    cleaned = _spoken_redaction(text)
    if not cleaned:
        return []

    sentences: list[str] = []
    for part in _SENTENCE_END.split(cleaned):
        value = part.strip()
        if value:
            sentences.extend(_split_long_sentence(value, chunk_limit))

    chunks: list[str] = []
    used = 0
    current = ""
    for sentence in sentences:
        remaining = total_limit - used
        if remaining <= 0:
            break
        value = sentence[:remaining]
        if current and len(current) + len(value) > chunk_limit:
            chunks.append(current)
            used += len(current)
            current = ""
            if len(chunks) >= chunk_count:
                break
            remaining = total_limit - used
            value = sentence[:remaining]
        current += value
    if current and len(chunks) < chunk_count and used < total_limit:
        chunks.append(current[: total_limit - used])
    return [chunk for chunk in chunks if chunk]
