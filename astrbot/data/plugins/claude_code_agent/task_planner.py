"""Deterministic, bounded task planning for the local Agent pipeline."""

from __future__ import annotations

from dataclasses import dataclass

try:
    from .action_policy import ActionClass, classify_action
    from .agent_core import normalize_backend, validate_task
    from .artifact_staging import is_artifact_request
except ImportError:  # Direct module loading in unit tests.
    from action_policy import ActionClass, classify_action
    from agent_core import normalize_backend, validate_task
    from artifact_staging import is_artifact_request


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
    # ponytail: single invocation — backend LLM plans internally.
    # Regex-splitting lost cross-step context and produced fake "multi-step"
    # plans with no shared state between independent CLI invocations.
    steps = (
        TaskStep(
            task_id=task_id,
            index=0,
            instruction=goal,
            action_class=classify_action(goal).action_class,
            expected_artifact=is_artifact_request(goal),
        ),
    )
    return ExecutionPlan(task_id, backend, steps)
