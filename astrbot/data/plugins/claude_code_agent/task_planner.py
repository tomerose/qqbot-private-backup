"""Deterministic, bounded task planning for the local Agent pipeline."""

from __future__ import annotations

import re
from dataclasses import dataclass

try:
    from .action_policy import ActionClass, classify_action
    from .agent_core import normalize_backend, validate_task
except ImportError:  # Direct module loading in unit tests.
    from action_policy import ActionClass, classify_action
    from agent_core import normalize_backend, validate_task


_SEPARATOR = re.compile(r"(?:，?然后|，?再|；|;|\r?\n)+")
_ARTIFACT_HINT = re.compile(r"生成|创建|导出|写入|编写|制作", re.I)


@dataclass(frozen=True)
class TaskRequest:
    task_id: str
    goal: str
    preferred_backend: str


@dataclass(frozen=True)
class TaskStep:
    task_id: str
    index: int
    instruction: str
    action_class: ActionClass
    expected_artifact: bool


@dataclass(frozen=True)
class ExecutionPlan:
    task_id: str
    preferred_backend: str
    steps: tuple[TaskStep, ...]


def plan_task(request: TaskRequest) -> ExecutionPlan:
    task_id = str(request.task_id or "").strip()
    if not task_id or len(task_id) > 64:
        raise ValueError("任务编号无效")
    goal = validate_task(request.goal)
    backend = normalize_backend(request.preferred_backend)
    clauses = [item.strip(" ，") for item in _SEPARATOR.split(goal)]
    clauses = [item for item in clauses if item][:8] or [goal]
    steps = tuple(
        TaskStep(
            task_id=task_id,
            index=index,
            instruction=clause,
            action_class=classify_action(clause).action_class,
            expected_artifact=bool(_ARTIFACT_HINT.search(clause)),
        )
        for index, clause in enumerate(clauses)
    )
    return ExecutionPlan(task_id, backend, steps)
