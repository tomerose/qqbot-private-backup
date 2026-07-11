"""
DEEP营助手 — 仅在群 820762428 生效
"""
import sys, os
sys.path.insert(0, r"D:\Claudecoda学习\diedeepbirth\scripts")

from astrbot.api.star import Context, Star
from astrbot.api.message_components import Plain

DEEP_GROUP = "820762428"

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
except:
    HAS_API = False


class DeepCampHelper(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)

    async def on_message(self, ctx: Context):
        gid = ctx.get_group_id()
        if gid != DEEP_GROUP:
            return

        msg = ctx.get_message_text().strip().lower()

        # Auto-inject KB context when asked about DEEP camp
        deep_keywords = ["任务", "考核", "评分", "deadline", "ddl", "结营", "deep", "夜枭", "壁垒", "提交"]
        if any(kw in msg for kw in deep_keywords):
            ctx.add_context("system", DEEP_KB)

        if not msg.startswith("/"):
            return

        text = ""
        try:
            if not HAS_API:
                yield ctx.reply(Plain(DEEP_KB + "\n\n（实时API暂不可用，以上为缓存数据）"))
                return

            from datetime import datetime
            tasks = fetch_tasks()

            if msg in ("/tasks", "/任务"):
                pub = [t for t in tasks if t.get("status") == "PUBLISHED"]
                lines = []
                for t in pub[:12]:
                    tier = {"REQUIRED": "必", "OPTIONAL": "选", "BONUS": "加分"}.get(t.get("taskTier", ""), "")
                    dl = (t.get("deadline") or "?")[:10]
                    lines.append(f"[{tier}] {t['title'][:40]} | {dl}")
                text = "\n".join(lines) if lines else "暂无已发布任务"

            elif msg in ("/deadline", "/ddl", "/截止"):
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
                    except:
                        pass
                urgent.sort()
                lines = []
                for h, t in urgent[:8]:
                    icon = "!!" if h <= 24 else "!"
                    d = f"{h:.0f}h" if h < 48 else f"{h/24:.0f}d"
                    lines.append(f"[{d}]{icon} {t['title'][:35]}")
                text = "\n".join(lines) if lines else "无临近截止"

            elif msg in ("/status", "/状态"):
                p = len([t for t in tasks if t.get("status") == "PUBLISHED"])
                text = f"DEEP营: {p} 已发布"

            elif msg in ("/submit", "/提交"):
                text = "GitHub: https://github.com/tomerose/deep-camp-phase1-liu"

            elif msg.startswith("/task ") or msg.startswith("/任务 "):
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
            else:
                return

        except Exception as e:
            text = f"获取失败: {e}"

        if text:
            yield ctx.reply(Plain(text))
