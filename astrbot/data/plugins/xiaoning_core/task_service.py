"""Canonical task lifecycle rules shared with existing execution backends."""

from __future__ import annotations

from datetime import datetime, timezone

from .models import TaskRecord, TaskState


_TRANSITIONS = {
    TaskState.ACCEPTED: {TaskState.PLANNED, TaskState.CANCELLED, TaskState.FAILED},
    TaskState.PLANNED: {
        TaskState.AWAITING_APPROVAL, TaskState.QUEUED, TaskState.EXECUTING,
        TaskState.CANCELLED, TaskState.FAILED,
    },
    TaskState.AWAITING_APPROVAL: {
        TaskState.QUEUED, TaskState.EXECUTING, TaskState.CANCELLED, TaskState.FAILED,
    },
    TaskState.QUEUED: {
        TaskState.AWAITING_APPROVAL, TaskState.EXECUTING,
        TaskState.CANCELLED, TaskState.TIMEOUT, TaskState.FAILED,
    },
    TaskState.EXECUTING: {
        TaskState.AWAITING_APPROVAL, TaskState.VERIFYING,
        TaskState.CANCELLED, TaskState.TIMEOUT, TaskState.FAILED,
    },
    TaskState.VERIFYING: {
        TaskState.DELIVERING, TaskState.CANCELLED, TaskState.TIMEOUT, TaskState.FAILED,
    },
    TaskState.DELIVERING: {
        TaskState.COMPLETED, TaskState.DELIVERY_PENDING,
        TaskState.CANCELLED, TaskState.TIMEOUT, TaskState.FAILED,
    },
    # A delivery retry resumes only delivery. It can never return to execution.
    TaskState.DELIVERY_PENDING: {
        TaskState.DELIVERING, TaskState.COMPLETED,
        TaskState.CANCELLED, TaskState.TIMEOUT, TaskState.FAILED,
    },
}


def transition_task(
    task: TaskRecord,
    target: TaskState,
    *,
    evidence: dict | None = None,
    delivery_cursor: tuple[str, ...] | None = None,
    now: datetime | None = None,
) -> TaskRecord:
    target = TaskState(target)
    if target not in _TRANSITIONS.get(task.state, set()):
        raise ValueError(f"任务状态迁移无效: {task.state.value} -> {target.value}")
    records = list(task.verification_evidence)
    if evidence:
        records.append(dict(evidence))
    cursor = task.delivery_cursor if delivery_cursor is None else tuple(delivery_cursor)
    if target is TaskState.COMPLETED:
        confirmed = any(
            item.get("kind") == "delivery_receipt" and item.get("confirmed") is True
            for item in records
        )
        if not confirmed:
            raise ValueError("没有收件端交付证据，任务不能进入 completed")
    return task.model_copy(
        update={
            "state": target,
            "verification_evidence": tuple(records),
            "delivery_cursor": cursor,
            "updated_at": now or datetime.now(timezone.utc),
        }
    )


def recovery_action(task: TaskRecord) -> str:
    if task.state is TaskState.DELIVERY_PENDING:
        return "retry_delivery_only"
    if task.state in {TaskState.EXECUTING, TaskState.VERIFYING, TaskState.DELIVERING}:
        return "resume_if_idempotent"
    if task.state in {
        TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED, TaskState.TIMEOUT,
    }:
        return "none"
    return "resume_control_flow"
