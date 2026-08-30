"""Low-noise task progress emission policy."""

from __future__ import annotations

from dataclasses import dataclass, field

from .task_orchestrator import TaskEvent


_PRIMARY_EVENTS = {"started", "approval_required", "completed", "failed"}


@dataclass
class _TaskProgress:
    started_at: float
    emitted: set[str] = field(default_factory=set)
    stage_update_sent: bool = False


class ProgressPolicy:
    def __init__(self, stage_delay_seconds: float = 90.0):
        self.stage_delay_seconds = max(0.0, float(stage_delay_seconds))
        self._state: dict[str, _TaskProgress] = {}

    def should_emit(self, event: TaskEvent, now: float) -> bool:
        task_id = str(event.task_id or "")
        current = float(now)
        state = self._state.setdefault(task_id, _TaskProgress(current))
        kind = str(event.kind or "")
        if kind in _PRIMARY_EVENTS:
            if kind in state.emitted:
                return False
            state.emitted.add(kind)
            return True
        if (
            current - state.started_at >= self.stage_delay_seconds
            and not state.stage_update_sent
        ):
            state.stage_update_sent = True
            return True
        return False

