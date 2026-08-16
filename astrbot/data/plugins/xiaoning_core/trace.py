"""Local JSONL domain events without raw conversation content."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from pathlib import Path

from .models import DomainEvent


def new_trace_id() -> str:
    return uuid.uuid4().hex


def fingerprint(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8", errors="replace")).hexdigest()[:16]


class TraceStore:
    def __init__(self, path: Path, *, max_bytes: int = 10 * 1024 * 1024):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max(1024 * 1024, int(max_bytes))
        self._lock = threading.Lock()

    def append(self, event: DomainEvent) -> None:
        line = event.model_dump_json(exclude_none=True) + "\n"
        with self._lock:
            if self.path.exists() and self.path.stat().st_size + len(line.encode("utf-8")) > self.max_bytes:
                archive = self.path.with_suffix(f"{self.path.suffix}.1")
                archive.unlink(missing_ok=True)
                self.path.replace(archive)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)

    def record_route(
        self,
        *,
        trace_id: str,
        scope: str,
        sender_id: str,
        channel: str,
        kind: str,
        owner: str,
        reason_code: str,
        confidence: float,
        should_respond: bool,
        text_length: int,
    ) -> None:
        self.append(
            DomainEvent(
                trace_id=trace_id,
                event_type="route_decision",
                stage="routing",
                attributes={
                    "scope_fp": fingerprint(scope),
                    "sender_fp": fingerprint(sender_id),
                    "channel": channel[:40],
                    "kind": kind,
                    "owner": owner,
                    "reason_code": reason_code,
                    "confidence": round(confidence, 4),
                    "should_respond": should_respond,
                    "text_length": max(0, int(text_length)),
                },
            )
        )

    def record_engagement(
        self,
        *,
        trace_id: str,
        scope: str,
        event_type: str,
        attributes: dict[str, str | int | float | bool | None] | None = None,
    ) -> None:
        bounded = {
            str(key)[:80]: value
            for key, value in (attributes or {}).items()
            if isinstance(value, (str, int, float, bool)) and len(str(value)) <= 120
        }
        bounded["scope_fp"] = fingerprint(scope)
        self.append(
            DomainEvent(
                trace_id=trace_id,
                event_type=str(event_type)[:100],
                stage="engagement",
                attributes=bounded,
            )
        )


def read_events(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line]
