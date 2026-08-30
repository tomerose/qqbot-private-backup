"""Dependency-injected orchestration for planned local Agent steps."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

try:
    from .backend_router import BackendRoute, can_retry
    from .step_policy import StepDecision
    from .task_planner import ExecutionPlan, TaskStep
    from .task_verifier import VerificationEvidence
except ImportError:  # Direct module loading in unit tests.
    from backend_router import BackendRoute, can_retry
    from step_policy import StepDecision
    from task_planner import ExecutionPlan, TaskStep
    from task_verifier import VerificationEvidence


@dataclass(frozen=True)
class StepExecution:
    exit_code: int | None
    deliverables: tuple[object, ...]
    verification_exit: int | None
    started_side_effect: bool
    response: str = ""
    error_code: str = ""


@dataclass(frozen=True)
class TaskEvent:
    kind: str
    task_id: str
    step_index: int
    code: str


@dataclass(frozen=True)
class TaskOutcome:
    verified: bool
    events: tuple[TaskEvent, ...]
    state: str
    deliverables: tuple[object, ...] = ()
    responses: tuple[str, ...] = ()


Policy = Callable[[TaskStep], StepDecision]
Router = Callable[[TaskStep, frozenset[str]], BackendRoute]
Executor = Callable[[TaskStep, BackendRoute], Awaitable[StepExecution]]
Verifier = Callable[[TaskStep, StepExecution], Awaitable[VerificationEvidence]]
ApprovalCheck = Callable[[TaskStep], bool]


class TaskOrchestrator:
    def __init__(
        self,
        *,
        policy: Policy,
        router: Router,
        executor: Executor,
        verifier: Verifier,
        approval_check: ApprovalCheck,
    ):
        self.policy = policy
        self.router = router
        self.executor = executor
        self.verifier = verifier
        self.approval_check = approval_check

    async def run(self, plan: ExecutionPlan, *, start_index: int = 0) -> TaskOutcome:
        cursor = int(start_index)
        if cursor < 0 or cursor > len(plan.steps):
            raise ValueError("任务步骤游标无效")
        events = [TaskEvent("started", plan.task_id, cursor, "started")]
        all_deliverables: list[object] = []
        responses: list[str] = []
        for step in plan.steps[cursor:]:
            decision = self.policy(step)
            approved = decision.requires_approval and self.approval_check(step)
            if decision.requires_approval and not approved:
                events.append(
                    TaskEvent(
                        "approval_required",
                        plan.task_id,
                        step.index,
                        decision.code,
                    )
                )
                return TaskOutcome(
                    False, tuple(events), "awaiting_approval",
                    tuple(all_deliverables), tuple(responses),
                )
            if not decision.allowed and not approved:
                events.append(
                    TaskEvent("failed", plan.task_id, step.index, decision.code)
                )
                return TaskOutcome(
                    False, tuple(events), "failed", tuple(all_deliverables), tuple(responses)
                )

            attempted: set[str] = set()
            while True:
                route = self.router(step, frozenset(attempted))
                if route.backend is None:
                    events.append(
                        TaskEvent("failed", plan.task_id, step.index, route.code)
                    )
                    return TaskOutcome(
                        False, tuple(events), "failed", tuple(all_deliverables), tuple(responses)
                    )
                if route.backend in attempted:
                    events.append(
                        TaskEvent(
                            "failed", plan.task_id, step.index, "backend_repeated"
                        )
                    )
                    return TaskOutcome(
                        False, tuple(events), "failed", tuple(all_deliverables), tuple(responses)
                    )
                attempted.add(route.backend)
                execution = await self.executor(step, route)
                evidence = await self.verifier(step, execution)
                if evidence.verified:
                    all_deliverables.extend(execution.deliverables)
                    if execution.response:
                        responses.append(execution.response)
                    events.append(
                        TaskEvent(
                            "step_completed",
                            plan.task_id,
                            step.index,
                            evidence.code,
                        )
                    )
                    break
                if can_retry(
                    step,
                    started_side_effect=execution.started_side_effect,
                    attempts=len(attempted),
                ):
                    continue
                events.append(
                    TaskEvent("failed", plan.task_id, step.index, evidence.code)
                )
                return TaskOutcome(
                    False, tuple(events), "failed", tuple(all_deliverables), tuple(responses)
                )
        final_index = plan.steps[-1].index if plan.steps else 0
        events.append(TaskEvent("completed", plan.task_id, final_index, "completed"))
        return TaskOutcome(
            True,
            tuple(events),
            "completed",
            tuple(all_deliverables),
            tuple(responses),
        )
