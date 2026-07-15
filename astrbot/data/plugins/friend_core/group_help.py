"""High-confidence, low-noise group-help opportunities."""

from __future__ import annotations

import re


_FILE_WORK = re.compile(r"文件|表格|报告|PPT|幻灯|文案|代码|图片|视频", re.I)
_RESEARCH = re.compile(r"推荐|对比|资料|查(?:一下|下)?|攻略|路线|哪里|怎么去", re.I)
_HELP_SIGNAL = re.compile(
    r"(?:求助|救命|谁(?:会|知道|懂)|有没有人|能不能(?:帮|看)|帮(?:我|忙)|"
    r"怎么(?:办|做|弄|解决|处理)|如何(?:做|弄|解决|处理))",
    re.I,
)


def group_help_offer(text: object) -> str | None:
    """Return one useful offer only for a clear public request for help."""
    message = str(text or "").strip()
    if not _HELP_SIGNAL.search(message):
        return None
    if _FILE_WORK.search(message):
        return "这个我能直接处理。把文件和要交付的结果说清楚，我做好后把成品发回群里。"
    if _RESEARCH.search(message):
        return "这个我能帮你查清再整理重点。把地点、预算或筛选条件补一句就行。"
    return "这个我能一起拆。把目标和卡住的地方说具体点，我直接给可执行的答案。"
