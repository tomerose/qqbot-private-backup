"""Lightweight cache: remember generated files so "send me that file" just works."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

_CACHE_PATH = Path(__file__).resolve().parents[3] / "plugin_data" / "claude_code_agent" / "file_cache.json"
_MAX_ENTRIES = 50
_FILE_REF_PATTERNS = [
    re.compile(p, re.I) for p in [
        r"(?:发|给我|发我|发一下|发来|发过来|传一下|传给我).{0,10}(?:那个|刚才|上次|之前|这个).{0,10}(?:文件|PPT|ppt|文档|报告|照片|图片|压缩包|表格|Excel)",
        r"(?:那个|刚才|上次|之前|这个).{0,6}(?:文件|PPT|ppt|文档|报告|照片|图片).{0,6}(?:发|给|来|一下)",
        r"(?:再|重新|还).{0,4}(?:发|给|传).{0,4}(?:一下|一次|一遍)",
        r"(?:文件|PPT|ppt|报告|文档).{0,4}(?:呢|在哪|好了吗|完成了吗|发了吗)",
    ]
]
_FILENAME_RE = re.compile(r"(?:周深|PPT|pptx?|报告|文档|照片|图片|文件)", re.I)


def _load() -> list[dict]:
    try:
        if _CACHE_PATH.is_file():
            return json.loads(_CACHE_PATH.read_text("utf-8"))
    except Exception:
        pass
    return []


def _save(entries: list[dict]) -> None:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps(entries[-_MAX_ENTRIES:], ensure_ascii=False, indent=2), "utf-8")


def record_file(task: str, file_path: str, sender_id: str = "", job_id: str = "") -> None:
    """Record a successfully delivered file."""
    entries = _load()
    path = Path(file_path)
    entries.append({
        "task": str(task)[:200],
        "file": str(path),
        "name": path.name,
        "sender": str(sender_id)[:20],
        "job_id": str(job_id)[:20],
        "time": time.time(),
    })
    _save(entries)


def is_file_request(text: str) -> bool:
    """Check if a message is asking for a previously generated file."""
    return any(p.search(str(text)) for p in _FILE_REF_PATTERNS)


def find_matching_file(text: str, sender_id: str = "") -> Path | None:
    """Search cache for a file matching the user's vague reference.

    Returns the file path if found and still exists, or None.
    """
    entries = _load()
    if not entries:
        return None

    text_lower = str(text).lower()
    sender = str(sender_id)

    # Score each entry by relevance
    scored = []
    for e in reversed(entries):  # newer first
        score = 0
        task = str(e.get("task", "")).lower()
        name = str(e.get("name", "")).lower()
        combined = f"{task} {name}"

        # Name mention
        for keyword in ["周深", "ppt", "报告", "文档", "照片", "图片"]:
            if keyword in text_lower and keyword in combined:
                score += 3

        # Same sender gets priority
        if sender and str(e.get("sender", "")) == sender:
            score += 2

        # Recency boost
        age_hours = (time.time() - float(e.get("time", 0))) / 3600
        if age_hours < 1:
            score += 3
        elif age_hours < 6:
            score += 2
        elif age_hours < 24:
            score += 1

        if score > 0:
            scored.append((score, e))

    if not scored:
        # If no match, return the most recent file (likely what user means)
        latest = entries[-1]
        path = Path(str(latest["file"]))
        if path.is_file():
            return path
        return None

    scored.sort(key=lambda x: -x[0])
    best = scored[0][1]
    path = Path(str(best["file"]))
    return path if path.is_file() else None


def recent_files(limit: int = 5) -> list[dict]:
    """Return the most recently cached files."""
    entries = _load()
    return list(reversed(entries[-limit:]))
