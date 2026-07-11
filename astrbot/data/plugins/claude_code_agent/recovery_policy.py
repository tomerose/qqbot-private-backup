"""Deterministic replay-safety policy for interrupted local Agent jobs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:
    from .agent_core import validate_task
    from .action_policy import ActionClass, classify_action
except ImportError:  # Direct module loading in unit tests.
    from agent_core import validate_task
    from action_policy import ActionClass, classify_action


@dataclass(frozen=True)
class RecoveryAssessment:
    resumable: bool
    reason: str


def assess_recovery(task: str, work_dir: Path, base_dir: Path) -> RecoveryAssessment:
    """Allow replay only for strict read-only work inside the recovery root."""
    value = validate_task(task)
    try:
        Path(work_dir).resolve(strict=False).relative_to(Path(base_dir).resolve(strict=False))
    except (OSError, ValueError):
        return RecoveryAssessment(False, "outside_recovery_root")
    if classify_action(value).action_class is not ActionClass.READ_ONLY:
        return RecoveryAssessment(False, "action_not_read_only")
    return RecoveryAssessment(True, "replay_safe")
