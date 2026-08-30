"""AI job interview simulator — 5 rounds of questions + feedback. Pro-gated."""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

import requests
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
try:
    from xiaoning_runtime import defer_stop_event
except ImportError:
    from data.plugins.xiaoning_runtime import defer_stop_event
try:
    from xiaoning_core.ownership import route_allows
except ImportError:
    try:
        from data.plugins.xiaoning_core.ownership import route_allows
    except ImportError:
        def route_allows(_event, _owner):
            return True

try:
    from draw_command.pro_access import get_tier, is_active_pro_group, Tier
except ImportError:
    from data.plugins.draw_command.pro_access import get_tier, is_active_pro_group, Tier

PROXY = "http://127.0.0.1:3000/v1/chat/completions"
PRO_DAILY = 5
PRO_REQUIRED_MSG = "AI 面试需要 X 或 Pro 资格。添加小柠为 QQ 好友即可获得 X 资格。"
PRO_LIMIT_MSG = "AI 面试次数已用完（今日 {used}/{limit}）。明天自动重置。"
ROUNDS = 5
SESSION_TTL = 600  # 10 minutes
_NATURAL_INTERVIEW = re.compile(
    r"^(?:小柠[，,：:\s]*)?(?:帮我|请|我想)?"
    r"(?:开始|进行|模拟|来一场|做一次)\s*"
    r"(?P<role>.{1,80}?)(?:岗位|职位)?(?:的)?(?:模拟)?面试[。！!？?]?$",
    re.I,
)


def parse_interview_start(text: str) -> str | None:
    value = str(text or "").strip()
    if value.startswith(("/interview", "/面试")):
        parts = value.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else "产品经理"
    match = _NATURAL_INTERVIEW.match(value)
    return match.group("role").strip() if match else None


def _call(messages: list[dict], max_tokens: int = 600) -> str:
    resp = requests.post(
        PROXY,
        json={"model": "gemini-3.7-flash", "messages": messages, "max_tokens": max_tokens},
        timeout=45,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


class AiInterview(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}
        project_root = Path(__file__).resolve().parents[4]
        self._pro_db = (
            project_root / "astrbot" / "data" / "plugin_data"
            / "xiaoning_pro" / "pro_members.db"
        )
        self._sessions: dict[str, dict] = {}
        self._daily_usage: dict[str, int] = {}

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=960)
    @defer_stop_event
    async def on_message(self, event: AstrMessageEvent):
        if not route_allows(event, "ai_interview"):
            return
        text = self._msg(event)
        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
        if not sender_id:
            return

        session_key = str(getattr(event, "unified_msg_origin", "") or sender_id)

        # End must be checked before the broader start prefix.
        if text.strip().lower() in {"/interview end", "/面试 end", "/面试 结束", "结束面试", "停止面试"}:
            event.stop_event()
            session = self._sessions.pop(session_key, None)
            yield event.plain_result("面试已结束。" if session else "没有进行中的面试。")
            return

        # Start interview
        role = parse_interview_start(text)
        if role is not None:
            is_private = bool(getattr(event, "is_private_chat", lambda: False)())
            is_wake = bool(getattr(event, "is_at_or_wake_command", False))
            if not (is_private or is_wake):
                return
            event.stop_event()
            group_id = str(getattr(event, "get_group_id", lambda: "")() or "")
            in_pro_group = bool(group_id) and is_active_pro_group(group_id, self._pro_db)
            if get_tier(sender_id, self._pro_db) < Tier.X and not in_pro_group:
                yield event.plain_result(PRO_REQUIRED_MSG)
                return

            today = time.strftime("%Y%m%d")
            dk = f"{sender_id}:{today}"
            used = self._daily_usage.get(dk, 0)
            if used >= PRO_DAILY:
                yield event.plain_result(PRO_LIMIT_MSG.format(used=used, limit=PRO_DAILY))
                return

            if not role or len(role) > 80:
                yield event.plain_result("岗位名称需在 1-80 个字符之间。")
                return
            difficulty = "中等"
            if "--难" in role or "--困难" in role:
                difficulty = "困难"
                role = role.replace("--困难", "").replace("--难", "").strip()
            if "--易" in role or "--简单" in role:
                difficulty = "简单"
                role = role.replace("--易", "").replace("--简单", "").strip()

            self._sessions[session_key] = {
                "role": role,
                "difficulty": difficulty,
                "round": 0,
                "history": [],
                "started": time.time(),
            }
            question = await self._ask_next(session_key)
            if question is None:
                self._sessions.pop(session_key, None)
                yield event.plain_result("面试启动失败，请稍后再试，本次不计次数。")
                return
            self._daily_usage[dk] = used + 1
            yield event.plain_result(
                f"🎯 面试开始！\n岗位：{role}\n难度：{difficulty}\n"
                f"共 {ROUNDS} 轮。回复 '/interview end' 可随时结束。\n\n"
                f"📝 第 1/{ROUNDS} 题：{question}"
            )
            return

        # Answer in ongoing interview
        session = self._sessions.get(session_key)
        if session is None:
            return
        if time.time() - session["started"] > SESSION_TTL:
            del self._sessions[session_key]
            return

        event.stop_event()
        session["history"].append({"role": "user", "content": text})
        session["round"] += 1

        if session["round"] >= ROUNDS:
            yield event.plain_result("📊 正在生成面试评估…")
            result = await self._final_eval(session_key)
            yield event.plain_result(result or "评估生成失败，请稍后再试。")
            return

        question = await self._ask_next(session_key)
        yield event.plain_result(
            f"📝 第 {session['round'] + 1}/{ROUNDS} 题：{question}"
            if question else "提问生成失败，请稍后继续回答。"
        )

    async def _ask_next(self, session_key: str) -> str | None:
        session = self._sessions[session_key]
        if session["round"] == 0:
            prompt = (
                f"你是{ session['role'] }岗位的面试官（难度：{ session['difficulty'] }）。"
                f"这是第 1 题。请直接提问，不要寒暄。"
            )
        else:
            last_answer = session["history"][-1]["content"]
            prompt = (
                f"你是{ session['role'] }岗位的面试官。"
                f"候选人的上一轮回答：{ last_answer }\n"
                f"请基于这个回答进行追问，考察更深层的理解。"
                f"这是第 { session['round'] + 1 }/{ROUNDS} 题。只输出问题本身，不评价。"
            )

        try:
            question = await asyncio.to_thread(
                _call,
                [{"role": "system", "content": prompt}, {"role": "user", "content": "请提问"}],
            )
            session["history"].append({"role": "assistant", "content": question})
            return question
        except Exception:
            return None

    async def _final_eval(self, session_key: str) -> str | None:
        session = self._sessions.pop(session_key, {})
        history_text = "\n".join(
            f"{'面试官' if h['role'] == 'assistant' else '候选人'}：{h['content']}"
            for h in session.get("history", [])
        )
        try:
            result = await asyncio.to_thread(
                _call,
                [
                    {
                        "role": "system",
                        "content": (
                            f"你是{ session.get('role', '?') }岗位的面试官。"
                            f"请基于以下面试记录给出评分和反馈。"
                            f"输出格式：\n"
                            f"✅ 技术深度 X/10\n"
                            f"✅ 表达清晰度 X/10\n"
                            f"✅ 实例运用 X/10\n"
                            f"💡 综合建议（2-3 句）"
                        ),
                    },
                    {"role": "user", "content": history_text},
                ],
                500,
            )
            return f"📊 面试评估：\n\n{result}"
        except Exception:
            return None

    @staticmethod
    def _msg(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_message_str", None)
        if callable(getter):
            return str(getter() or "")
        return str(getattr(event, "message_str", "") or "")
