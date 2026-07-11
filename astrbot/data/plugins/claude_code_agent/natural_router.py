"""Conservative natural-language routing for the owner-only local Agent."""

from __future__ import annotations

import re
from dataclasses import dataclass

try:
    from .agent_core import normalize_backend, validate_task
except ImportError:  # Direct module loading in unit tests.
    from agent_core import normalize_backend, validate_task


@dataclass(frozen=True)
class NaturalAgentIntent:
    action: str
    task: str = ""
    backend: str = ""


_STATUS_TEXTS = {"任务进度怎么样", "看看任务进度", "任务状态", "进度怎么样"}
_CANCEL_TEXTS = {"取消刚才的任务", "取消任务", "停止任务", "停下任务"}
_CONFIRM_TEXTS = {"确认执行", "我确认执行", "确认这个任务"}
_TASK_PREFIX = re.compile(r"^(?:小柠[，, ]*)?(?:帮我|请你|麻烦你)\s*(.+)$", re.S)
_BACKEND_PREFIX = re.compile(
    r"^用\s*(claude(?:\s*code)?|codex|workbuddy)\s*", re.I
)


def _normalized_control_text(text: str) -> str:
    return str(text or "").strip().rstrip("。！!？?").strip().lower()


def _extract_backend(task: str) -> tuple[str, str]:
    match = _BACKEND_PREFIX.match(task)
    if not match:
        return "", task.strip()
    raw = match.group(1).lower().replace(" ", "")
    backend = "claude" if raw.startswith("claude") else normalize_backend(raw)
    return backend, task[match.end() :].strip()


def route_natural_agent(text: str) -> NaturalAgentIntent | None:
    """Return an Agent intent only for explicit, low-ambiguity owner language."""
    normalized = _normalized_control_text(text)
    if normalized in _STATUS_TEXTS:
        return NaturalAgentIntent("status")
    if normalized in _CANCEL_TEXTS:
        return NaturalAgentIntent("cancel")
    if normalized in _CONFIRM_TEXTS:
        return NaturalAgentIntent("confirm")

    match = _TASK_PREFIX.match(str(text or "").strip())
    if not match:
        return None
    backend, task = _extract_backend(match.group(1))
    if not task:
        return None
    return NaturalAgentIntent("run", validate_task(task), backend)


def _component_targets_self(component: object, self_id: str) -> bool:
    if isinstance(component, dict):
        kind = str(component.get("type", "")).rsplit(".", 1)[-1].lower()
        target = component.get("qq", component.get("target", ""))
        return kind == "at" and str(target) == self_id
    kind = type(component).__name__.lower()
    component_type = str(getattr(component, "type", "")).rsplit(".", 1)[-1].lower()
    target = getattr(component, "qq", getattr(component, "target", ""))
    return (kind == "at" or component_type == "at") and str(target) == self_id


def extract_natural_agent_text(
    text: str,
    components: object,
    self_id: str,
    group_id: str,
) -> str:
    """Allow private text directly; require a real At segment in group chats."""
    candidate = str(text or "").strip()
    if not candidate:
        return ""
    if not str(group_id or "").strip():
        return candidate
    if not isinstance(components, (list, tuple)):
        return ""
    return candidate if any(_component_targets_self(item, str(self_id)) for item in components) else ""
