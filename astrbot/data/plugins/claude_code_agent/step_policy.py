"""Per-step authorization decisions for planned local Agent work."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

try:
    from .action_policy import ActionClass
    from .agent_core import DEFAULT_WORKSPACE, DEFAULT_WORK_DIR
    from .task_planner import TaskStep
except ImportError:  # Direct module loading in unit tests.
    from action_policy import ActionClass
    from agent_core import DEFAULT_WORKSPACE, DEFAULT_WORK_DIR
    from task_planner import TaskStep


@dataclass(frozen=True)
class StepDecision:
    allowed: bool
    requires_approval: bool
    code: str


def step_digest(step: TaskStep) -> str:
    value = f"{step.task_id}:{step.index}:{step.instruction}"
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _directory_within(path: Path, root: Path) -> bool:
    candidate = Path(path)
    if candidate.is_symlink():
        return False
    try:
        resolved = candidate.resolve(strict=True)
        base = Path(root).resolve(strict=False)
    except OSError:
        return False
    return resolved.is_dir() and (resolved == base or base in resolved.parents)


def assess_step(
    step: TaskStep,
    work_dir: Path,
    output_dir: Path,
    *,
    allowed_work_root: Path = DEFAULT_WORK_DIR,
    allowed_output_root: Path = DEFAULT_WORKSPACE,
) -> StepDecision:
    # Unknown local actions are too vague to run without an explicit yes.
    if step.action_class in {ActionClass.HIGH_IMPACT, ActionClass.UNKNOWN}:
        return StepDecision(False, True, step.action_class.value)
    if not _directory_within(work_dir, allowed_work_root):
        return StepDecision(False, True, "outside_work_root")
    if not _directory_within(output_dir, allowed_output_root):
        return StepDecision(False, False, "invalid_output_root")
    return StepDecision(True, False, "allowed")
