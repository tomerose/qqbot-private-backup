"""
/think — Gemini thinking for X/Pro users.

Triggers: /think, /推理, or natural "深度思考/仔细分析/好好想想 + question"
"""

import asyncio
import re
from pathlib import Path

import requests
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.api import logger

try:
    from draw_command.pro_access import get_tier, Tier
except ImportError:
    from data.plugins.draw_command.pro_access import get_tier, Tier

GEMINI_PROXY = "http://127.0.0.1:3000/v1/chat/completions"
REQUIRED_MSG = "深度思考需要 X 或 Pro 资格。添加小柠为 QQ 好友即可获得 X 资格。"

THINK_SYSTEM = (
    "用中文回答。你是严谨的分析师：给出结论、关键依据和不确定之处。"
    "不要展示内部思维链或隐藏推理过程，不说废话。"
)

_NATURAL_TRIGGERS = re.compile(
    r"^(?:小柠[，,\s]*)?"
    r"(?:深度思考|深入分析|仔细分析|好好想想|认真想|"
    r"深度想|仔细想|认真分析|分析一下|推理一下)"
    r"[：:，,\s]*(.{4,})$",
    re.I,
)

def extract_question(message: str) -> str | None:
    text = str(message or "").strip()
    command = re.match(r"^/(?:think|推理)(?:\s+(.+))?$", text, re.I)
    if command:
        return (command.group(1) or "").strip()
    natural = _NATURAL_TRIGGERS.match(text)
    return natural.group(1).strip() if natural else None


class DeepThink(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        project_root = Path(__file__).resolve().parents[4]
        self._pro_db = (
            project_root / "astrbot" / "data" / "plugin_data"
            / "xiaoning_pro" / "pro_members.db"
        )

    def _think_config(self, sender_id: str) -> tuple[str, str, str, str]:
        """Return the single supported chat backend."""
        return GEMINI_PROXY, "sk-gemini-vertex", "gemini-3.6-flash", "Gemini 3.6 Flash"

    @staticmethod
    def _call_think(question: str, *, api_base: str, api_key: str,
                    model: str) -> str:
        """Call Gemini with explicit thinking enabled."""
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": THINK_SYSTEM},
                {"role": "user", "content": question},
            ],
            "max_tokens": 4096,
        }
        # Gemini thinking mode via proxy
        if "gemini" in model.lower():
            payload["thinking"] = True
        response = requests.post(
            api_base,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            json=payload,
            timeout=120,
        )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]

        # Older proxy responses may contain a visible thought block.
        if "━━━━━━━━━━" in content:
            content = content.rsplit("━━━━━━━━━━", 1)[-1].strip()
        if len(content) > 3500:
            content = content[:3400] + "\n\n…（截断）"
        return content

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=965)
    async def on_message(self, ctx: AstrMessageEvent):
        if not (ctx.is_private_chat() or ctx.is_at_or_wake_command):
            return
        getter = getattr(ctx, "get_message_str", None)
        if not callable(getter):
            getter = getattr(ctx, "get_message_text", None)
        msg = str(getter() if callable(getter) else "").strip()
        if not msg:
            return

        question = extract_question(msg)
        if question is None:
            return
        if not question:
            yield ctx.plain_result("用法：/think <问题>")
            return

        sender_id = str(getattr(ctx, "get_sender_id", lambda: "")() or "")
        try:
            tier = get_tier(sender_id, self._pro_db)
        except Exception:
            tier = Tier.ORDINARY
        if tier < Tier.X:
            yield ctx.plain_result(REQUIRED_MSG)
            return
        api_base, api_key, model, label = self._think_config(sender_id)
        yield ctx.plain_result(f"🤔 深度思考中（{label}），稍等…")

        try:
            content = await asyncio.to_thread(
                self._call_think, question,
                api_base=api_base, api_key=api_key, model=model,
            )
            yield ctx.plain_result(content)
        except Exception as exc:
            logger.error("DeepThink failed: %s", type(exc).__name__)
            yield ctx.plain_result("脑子卡住了，待会再试。")
