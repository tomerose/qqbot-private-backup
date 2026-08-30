"""Deterministic backend selection and bounded retry rules."""

from __future__ import annotations

import re
from dataclasses import dataclass

try:
    from .action_policy import ActionClass
    from .task_planner import TaskStep
except ImportError:  # Direct module loading in unit tests.
    from action_policy import ActionClass
    from task_planner import TaskStep


SUPPORTED_BACKENDS = frozenset({"claude", "codex", "workbuddy"})
CODE_HINT = re.compile(
    r"代码|项目|测试|构建|编译|修复|重构|github|python|typescript|next\.js", re.I
)
DESKTOP_HINT = re.compile(r"桌面|窗口|点击|软件|浏览器界面", re.I)


@dataclass(frozen=True)
class BackendRoute:
    backend: str | None
    code: str


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def route_backend(
    step: TaskStep,
    preferred: str,
    available: set[str] | frozenset[str],
    attempted: set[str] | frozenset[str],
) -> BackendRoute:
    allowed = {str(item).lower() for item in available} & SUPPORTED_BACKENDS
    used = {str(item).lower() for item in attempted}
    preferred_name = str(preferred or "").strip().lower()
    if DESKTOP_HINT.search(step.instruction):
        order = ["workbuddy", "codex", "claude"]
    elif CODE_HINT.search(step.instruction):
        order = [preferred_name, "claude", "codex", "workbuddy"]
    else:
        order = [preferred_name, "claude", "codex", "workbuddy"]
    backend = next(
        (name for name in _unique(order) if name in allowed and name not in used),
        None,
    )
    return BackendRoute(backend, "selected" if backend else "no_backend")


def can_retry(
    step: TaskStep,
    *,
    started_side_effect: bool,
    attempts: int,
) -> bool:
    if int(attempts) >= 2:
        return False
    if started_side_effect:
        return False
    if step.action_class is ActionClass.HIGH_IMPACT:
        return False
    return True
