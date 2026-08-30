"""Optional private study-group helper; all IDs and integrations are local configuration."""
import os
import sys

_DEEP_SCRIPTS = os.getenv("XIAONING_DEEP_SCRIPTS", "").strip()
if _DEEP_SCRIPTS:
    sys.path.insert(0, _DEEP_SCRIPTS)

from astrbot.api import logger
from astrbot.api.star import Context, Star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain

DEEP_GROUP = os.getenv("XIAONING_DEEP_GROUP_ID", "").strip()
DEEP_WHITELIST = {
    item.strip()
    for item in os.getenv("XIAONING_DEEP_WHITELIST", "").split(",")
    if item.strip()
}
DEEP_COMMANDS = {
    "/tasks", "/任务", "/deadline", "/ddl", "/截止",
    "/status", "/状态", "/submit", "/提交",
}

DEEP_KB = """
## DEEP营完整任务与评分体系

### 评分机制
综合审核制：内容真实性>表面完整度，思考深度>功能数量。一票否决：作弊/全AI生成/无法运行直接不通关。

### 必做任务（已发布）
- 前置：环境搭建+GitHub+CCSwitch (07-10)
- 001：AI壁垒方向卡 (07-15)
- 002：AI真实场景应用实验 (07-18)
- 003：AI工作流与Agent雏形 (07-18)
- X1：AI加速器-用AI学会任何领域 (07-18)
- 004：AI辅助自动化 (07-24)
- 005：AI数据分析作品集 (07-28)
- 006：真实需求验证 (07-28)
- 007：Claude Code工程化 (08-08)
- 008：AI-Native全栈闭环 (08-08)
- 010：资产闭环结业作品 (08-20)
- 011：认知闭环思维格栅 (08-20)
- AI可靠性交付挑战 (08-16)

### 选做任务 B系列
B1:AI信息壁垒 B2:Code真实问题 B3:Python入门 B4:Python综合 B5:飞书协同 B6:PDF报告 B7:视频短片 B8:语音克隆 B9:RAG知识库 B10:工具开发 B11:多Agent协作 B12:个人品牌 B13:比赛实战 B14:垂直Agent

### 结营规则
必做全完成+结业作品(010+011)通过方可结营。

### 平台
- 网址: mangoleaningos.top
- GitHub: github.com/tomerose/deep-camp-phase1-liu
"""

try:
    from deep_monitor import fetch_tasks
    HAS_API = True
except Exception:
    HAS_API = False


class _LegacyEventAdapter:
    """Keep this scoped legacy plugin on AstrBot's current event API."""

    def __init__(self, event: AstrMessageEvent):
        self._event = event

    def get_group_id(self):
        return str(getattr(self._event, "get_group_id", lambda: "")() or "")

    def get_message_text(self):
        return str(getattr(self._event, "get_message_str", lambda: "")() or "")

    def reply(self, component):
        return self._event.chain_result([component])

    def stop_event(self):
        stopper = getattr(self._event, "stop_event", None)
        if callable(stopper):
            stopper()


class DeepCampHelper(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=955)
    async def on_message(self, event: AstrMessageEvent):
        ctx = _LegacyEventAdapter(event)
        gid = ctx.get_group_id()
        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
        is_deep_group = (gid == DEEP_GROUP)
        is_whitelisted_pm = (not gid and sender_id in DEEP_WHITELIST)
        if not (is_deep_group or is_whitelisted_pm):
            return

        msg = ctx.get_message_text().strip().lower()

        recognized = msg in DEEP_COMMANDS or msg.startswith(("/task ", "/任务 "))
        if not recognized:
            return
        ctx.stop_event()

        text = ""
        try:
            if msg in ("/submit", "/提交"):
                text = "GitHub: https://github.com/tomerose/deep-camp-phase1-liu"
            elif not HAS_API:
                yield ctx.reply(Plain(DEEP_KB + "\n\n（实时API暂不可用，以上为缓存数据）"))
                return
            else:
                from datetime import datetime
                tasks = fetch_tasks()

            if not text and msg in ("/tasks", "/任务"):
                pub = [t for t in tasks if t.get("status") == "PUBLISHED"]
                lines = []
                for t in pub[:12]:
                    tier = {"REQUIRED": "必", "OPTIONAL": "选", "BONUS": "加分"}.get(t.get("taskTier", ""), "")
                    dl = (t.get("deadline") or "?")[:10]
                    lines.append(f"[{tier}] {t['title'][:40]} | {dl}")
                text = "\n".join(lines) if lines else "暂无已发布任务"

            elif not text and msg in ("/deadline", "/ddl", "/截止"):
                now = datetime.utcnow()
                urgent = []
                for t in tasks:
                    if t.get("status") != "PUBLISHED" or not t.get("deadline"):
                        continue
                    try:
                        dl = datetime.fromisoformat(t["deadline"].replace("Z", "+00:00")).replace(tzinfo=None)
                        h = (dl - now).total_seconds() / 3600
                        if h > 0:
                            urgent.append((h, t))
                    except (TypeError, ValueError):
                        pass
                urgent.sort()
                lines = []
                for h, t in urgent[:8]:
                    icon = "!!" if h <= 24 else "!"
                    d = f"{h:.0f}h" if h < 48 else f"{h/24:.0f}d"
                    lines.append(f"[{d}]{icon} {t['title'][:35]}")
                text = "\n".join(lines) if lines else "无临近截止"

            elif not text and msg in ("/status", "/状态"):
                p = len([t for t in tasks if t.get("status") == "PUBLISHED"])
                text = f"DEEP营: {p} 已发布"

            elif not text and (msg.startswith("/task ") or msg.startswith("/任务 ")):
                code = msg.split(" ", 1)[1].strip()
                c = code.lower().strip("#")
                for t in tasks:
                    if c in t["title"].lower() or c == t["id"][:8]:
                        dl = (t.get("deadline") or "?")[:10]
                        tier = {"REQUIRED": "必做", "OPTIONAL": "选做", "BONUS": "加分"}.get(t.get("taskTier", ""), "")
                        desc = (t.get("description") or "")[:300]
                        score = (t.get("scoringCriteria") or "")[:300]
                        text = f"📌 {t['title']}\n[{tier}] 截止:{dl}\n\n📝 说明:\n{desc}\n\n📊 评分:\n{score}"
                        break
                else:
                    text = f"未找到: {code}"
        except Exception as exc:
            logger.warning("[DeepCamp] command failed: %s", type(exc).__name__)
            text = "获取失败，请稍后再试。"

        if text:
            yield ctx.reply(Plain(text))

    @filter.on_llm_request(priority=-15)
    async def inject_deep_context(self, event: AstrMessageEvent, req) -> None:
        group_id = str(getattr(event, "get_group_id", lambda: "")() or "")
        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
        message = str(getattr(event, "get_message_str", lambda: "")() or "").lower()
        is_deep_group = (group_id == DEEP_GROUP)
        is_whitelisted_pm = (not group_id and sender_id in DEEP_WHITELIST)
        if not (is_deep_group or is_whitelisted_pm):
            return
        keywords = ("任务", "考核", "评分", "deadline", "ddl", "结营", "deep", "夜枭", "壁垒", "提交")
        marker = "\u3010DEEP \u8425\u5730\u77e5\u8bc6\u3011"
        if (is_deep_group or is_whitelisted_pm) and any(word in message for word in keywords):
            if marker not in str(getattr(req, "system_prompt", "") or ""):
                req.system_prompt = f"{req.system_prompt or ''}\n\n{marker}\n{DEEP_KB}".strip()
