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
    ambiguous: bool = False       # True when intent overlaps with search/action
    clarification: str = ""       # human-readable disambiguation prompt

# ── Ambiguity: when NL could be Agent task OR search/action report ──
# A report can mean either a researched answer or a local file.  Creation
# verbs alone do not settle that question; concrete delivery targets do.
_REPORTISH = re.compile(
    r"研究|调研|分析|比较|对比|决策|规划.*行程|规划.*旅行|攻略|深度研究|"
    r"报告|调查|评估|汇总|整理|资料",
    re.I,
)
_DELIVERY_ACTION = re.compile(r"导出|保存|交付|下载|发给?我|做成", re.I)
# Strong disambiguators — these override the ambiguity check
_FORCE_AGENT = re.compile(
    r"(?:用\s*(?:claude|codex|workbuddy)|"
    r"代码|程序|脚本|项目|仓库|git|github|"
    r"数据库|接口|日志|压缩包|磁盘|disk|"
    r"python|typescript|javascript|node)",
    re.I,
)
_FORCE_SEARCH = re.compile(
    r"^(?:搜索|搜一下|查一下|查查看)\s",
    re.I,
)
_CONTEXT_OBJECT = re.compile(
    r"这份|那份|这个文件|那个文件|该文件|附件|刚才.*文件|上次.*文件|刚才.*报告|上次.*报告",
    re.I,
)
_READ_ONLY_TASK = re.compile(
    r"^(?:看(?:看|一下)?|解释(?:一下)?|评价(?:一下)?|点评(?:一下)?|"
    r"说说|讲讲|聊聊|总结(?:一下)?|分析(?:一下)?|研究(?:一下)?|"
    r"调研(?:一下)?)",
    re.I,
)
_REPORT_CREATION = re.compile(r"生成|创建|制作|编写|撰写|做(?:一份|个)?|写(?:一份|个)?", re.I)
_REPORT_ACTION = re.compile(r"^(?:深度研究|深入研究|研究|调研|比较|对比|规划)", re.I)


def _check_ambiguity(task: str) -> tuple[bool, str]:
    """Return (is_ambiguous, clarification_message) for a task that matched
    Agent routing but might also be a search/action report request."""
    if _FORCE_AGENT.search(task):
        return False, ""
    if _FORCE_SEARCH.match(task):
        return False, ""
    if _CONTEXT_OBJECT.search(task):
        return False, ""
    if (
        _REPORTISH.search(task)
        and not _DELIVERY_ACTION.search(task)
        and not _CONCRETE_AGENT_TARGET.search(task)
    ):
        return True, (
            "你是想让我查资料后直接给结论，还是做成 Word、PPT 这类文件发你？"
        )
    return False, ""


_STATUS_TEXTS = {
    "任务进度怎么样",
    "看看任务进度",
    "任务状态",
    "进度怎么样",
    "刚才的任务怎么样",
    "刚才那个任务怎么样",
    "上次任务怎么样",
    "文件发了吗",
    "结果发了吗",
    "任务完成了吗",
}
_STATUS_PATTERN = re.compile(
    r"(?:刚才|刚刚|上次|之前|那个|这个)?.{0,8}(?:任务|文件|结果).{0,8}"
    r"(?:进度|状态|完成|好了|发了|发到|送到|交付)",
    re.I,
)
_CANCEL_TEXTS = {
    "取消刚才的任务",
    "取消刚刚的任务",
    "取消任务",
    "停止任务",
    "停下任务",
    "别做了",
    "不用做了",
}
_CONFIRM_TEXTS = {
    "确认执行",
    "我确认执行",
    "确认这个任务",
    "同意执行",
    "可以执行",
    "继续执行",
}
_TASK_PREFIX = re.compile(
    r"^(?:小柠[，, ]*)?"
    r"(?:帮我|请你|麻烦你|请(?:你)?|帮忙)"
    r"[，, ]*"
    r"(.{3,})$",
    re.S,
)
_GREETING_ONLY = re.compile(
    r"^(?:你?好[呀啊]?|嗨|hi|hello|早[啊呀]?|晚安|再见|bye|在[吗么]?|嗯+|哦|谢谢|多谢|OK|ok|好的?|收到|明白|知道了)[!！。.,，]?$",
    re.I,
)
_BACKEND_PREFIX = re.compile(
    r"^用\s*(claude(?:\s*code)?|codex|workbuddy)\s*", re.I
)
_CONCRETE_AGENT_TARGET = re.compile(
    r"代码|项目|仓库|脚本|程序|测试|构建|编译|部署|服务|目录|文件|数据|"
    r"文档|表格|txt|csv|markdown|docx|xlsx|pptx|zip|json|"
    r"png|jpe?g|webp|gif|"
    r"数据库|网页|网站|接口|日志|压缩包|磁盘|disk|git|github|python|typescript|"
    r"浏览器|cookie|密码|通讯录|私聊记录|"
    r"javascript|node(?:\.js)?|excel|word|ppt|pdf",
    re.I,
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
    raw = str(text or "").strip()
    normalized = _normalized_control_text(text)
    if normalized in _STATUS_TEXTS or _STATUS_PATTERN.search(raw):
        return NaturalAgentIntent("status")
    if normalized in _CANCEL_TEXTS:
        return NaturalAgentIntent("cancel")
    if normalized in _CONFIRM_TEXTS:
        return NaturalAgentIntent("confirm")
    if _GREETING_ONLY.match(raw):
        return None

    match = _TASK_PREFIX.match(raw)
    if not match:
        return None
    backend, task = _extract_backend(match.group(1))
    if not task:
        return None
    task = validate_task(task)
    if not backend and _READ_ONLY_TASK.match(task):
        return None
    concrete_target = bool(
        _CONCRETE_AGENT_TARGET.search(task)
        or _CONTEXT_OBJECT.search(task)
        or (_DELIVERY_ACTION.search(task) and _REPORTISH.search(task))
    )
    if not (backend or concrete_target):
        if (
            (_REPORT_CREATION.search(task) or _REPORT_ACTION.search(task))
            and _REPORTISH.search(task)
        ):
            ambiguous, clarification = _check_ambiguity(task)
            return NaturalAgentIntent(
                "run", task, ambiguous=ambiguous, clarification=clarification
            )
        return None
    ambiguous, clarification = _check_ambiguity(task)
    return NaturalAgentIntent("run", task, backend, ambiguous=ambiguous, clarification=clarification)


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
