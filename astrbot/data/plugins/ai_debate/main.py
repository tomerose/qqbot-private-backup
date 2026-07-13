"""Multi-agent AI panel debate — 4 personas debate, 1 synthesizer. Pro-gated."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import requests
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
try:
    from xiaoning_runtime import defer_stop_event
except ImportError:
    from data.plugins.xiaoning_runtime import defer_stop_event

try:
    from draw_command.pro_access import get_tier, is_active_pro, is_active_pro_group, Tier
except ImportError:
    from data.plugins.draw_command.pro_access import get_tier, is_active_pro, is_active_pro_group, Tier

PROXY = "http://127.0.0.1:3000/v1/chat/completions"
PRO_DAILY = 10
FREE_DAILY = 1
PRO_MSG = "AI 圆桌辩论次数已用完（今日 {used}/{limit}）。请联系管理员获取更多次数。"

PERSONAS = [
    {
        "name": "📊 分析师",
        "role": "system",
        "content": (
            "你是数据驱动的分析师。你重视量化证据、统计数据和逻辑推理。"
            "你的论点要有具体数字或研究支撑。用中文回答，300-500 字。"
        ),
    },
    {
        "name": "🔥 支持者",
        "role": "system",
        "content": (
            "你是乐观的支持者。你看到事物的潜力和积极面。"
            "你善于用生动的例子和类比来说服别人。用中文回答，300-500 字。"
        ),
    },
    {
        "name": "⚠️ 怀疑者",
        "role": "system",
        "content": (
            "你是谨慎的怀疑论者。你关注风险、成本和潜在陷阱。"
            "你从反面论证，压力测试对方的逻辑漏洞。用中文回答，300-500 字。"
        ),
    },
    {
        "name": "🧠 学者",
        "role": "system",
        "content": (
            "你是公正的学者。你综合各方观点，引用学术研究和理论框架。"
            "你避免极端立场，追求客观平衡的分析。用中文回答，300-500 字。"
        ),
    },
]

SYNTH_PROMPT = """你是辩论主持人。以下是 4 位专家对「{topic}」的观点：

{responses}

请用 200-300 字给出综合结论：各方共识是什么？核心分歧在哪？值得进一步思考的问题是什么？"""


def _call_gemini(messages: list[dict], max_tokens: int = 800) -> str:
    resp = requests.post(
        PROXY,
        json={"model": "gemini-2.5-flash", "messages": messages, "max_tokens": max_tokens},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


class AiDebate(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}
        project_root = Path(__file__).resolve().parents[4]
        self._pro_db = (
            project_root / "astrbot" / "data" / "plugin_data"
            / "xiaoning_pro" / "pro_members.db"
        )
        self._daily_free: dict[str, int] = {}  # sender_id -> count

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=960)
    @defer_stop_event
    async def on_message(self, event: AstrMessageEvent):
        text = self._msg(event)
        if not text.startswith("/debate") and not text.startswith("/辩论"):
            return
        event.stop_event()

        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
        if not sender_id:
            return

        topic = text.split(maxsplit=1)[1] if " " in text else ""
        if not topic or len(topic) < 6:
            yield event.plain_result("用法：/debate <话题>。话题至少 6 个字。")
            return

        today = time.strftime("%Y%m%d")
        key = f"{sender_id}:{today}"
        tier = get_tier(sender_id, self._pro_db)
        group_id = str(getattr(event, "get_group_id", lambda: "")() or "")
        in_pro_group = bool(group_id) and is_active_pro_group(group_id, self._pro_db)
        limit = PRO_DAILY if (tier >= Tier.GO or in_pro_group) else FREE_DAILY
        used = self._daily_free.get(key, 0)
        if used >= limit:
            yield event.plain_result(PRO_MSG.format(used=used, limit=limit))
            return

        yield event.plain_result(f"🎤 已邀请 4 位专家入场辩论：{topic}\n⏳ 正在生成观点…")

        try:
            # Phase 1: Parallel persona responses
            async def ask(persona: dict) -> str:
                return await asyncio.to_thread(
                    _call_gemini,
                    [
                        persona,
                        {"role": "user", "content": f"请就以下话题发表你的观点：{topic}"},
                    ],
                )

            tasks = [ask(p) for p in PERSONAS]
            responses: list[str] = await asyncio.gather(*tasks)

            # Phase 2: Synthesis
            resp_text = "\n\n---\n\n".join(
                f"**{p['name']}**：{r}" for p, r in zip(PERSONAS, responses)
            )
            synth = await asyncio.to_thread(
                _call_gemini,
                [
                    {
                        "role": "system",
                        "content": "你是辩论主持人，用中文输出 200-300 字综合结论。",
                    },
                    {
                        "role": "user",
                        "content": SYNTH_PROMPT.format(topic=topic, responses=resp_text),
                    },
                ],
                600,
            )
        except Exception as exc:
            yield event.plain_result(f"辩论生成失败：{type(exc).__name__}")
            return

        # Deliver: each persona + synthesis
        for p, r in zip(PERSONAS, responses):
            yield event.plain_result(f"{p['name']}：{r}")

        self._daily_free[key] = used + 1
        yield event.plain_result(f"🧠 综合结论：{synth}")

    @staticmethod
    def _msg(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_message_str", None)
        if callable(getter):
            return str(getter() or "")
        return str(getattr(event, "message_str", "") or "")
