"""Privacy-safe, conservative task ETA estimates."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil

from .action_policy import ActionClass
from .task_planner import ExecutionPlan


@dataclass(frozen=True)
class EtaEstimate:
    minimum_seconds: int
    maximum_seconds: int

    @property
    def text(self) -> str:
        if self.maximum_seconds < 120:
            return f"预计约 {self.minimum_seconds}–{self.maximum_seconds} 秒"
        return (
            f"预计约 {ceil(self.minimum_seconds / 60)}"
            f"–{ceil(self.maximum_seconds / 60)} 分钟"
        )


def estimate_eta(plan: ExecutionPlan, queue_ahead: int = 0) -> EtaEstimate:
    """Estimate only from bounded plan metadata, never task text or user history."""
    steps = tuple(plan.steps)
    actions = {step.action_class for step in steps}
    if actions.intersection(
        {ActionClass.UNKNOWN, ActionClass.WORKSPACE_WRITE, ActionClass.HIGH_IMPACT}
    ):
        minimum, maximum = 180, 480
    elif any(step.expected_artifact for step in steps):
        minimum, maximum = 120, 240
    else:
        minimum, maximum = 30, 90

    extra_steps = max(0, len(steps) - 1)
    queue_count = max(0, min(int(queue_ahead), 5))
    return EtaEstimate(
        minimum + extra_steps * 45 + queue_count * 120,
        maximum + extra_steps * 90 + queue_count * 180,
    )
