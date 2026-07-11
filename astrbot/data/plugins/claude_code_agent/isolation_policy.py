"""Fail-closed isolation decisions for unclassified Agent steps."""

from __future__ import annotations

from dataclasses import dataclass

try:
    from .action_policy import ActionClass
    from .task_planner import TaskStep
except ImportError:  # Direct module loading in unit tests.
    from action_policy import ActionClass
    from task_planner import TaskStep


@dataclass(frozen=True)
class IsolationDecision:
    mode: str
    code: str


def choose_isolation(
    step: TaskStep,
    sandbox_ready: bool,
) -> IsolationDecision:
    if step.action_class is not ActionClass.UNKNOWN:
        return IsolationDecision("host", "known_action")
    if bool(sandbox_ready):
        return IsolationDecision("windows_sandbox", "isolated")
    return IsolationDecision("blocked", "isolation_unavailable")
