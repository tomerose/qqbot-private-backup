"""Keep emotional replies consistent with Xiaoning's normal chat persona."""

from __future__ import annotations

import asyncio

import requests
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
try:
    from xiaoning_runtime import defer_stop_event
except ImportError:
    from data.plugins.xiaoning_runtime import defer_stop_event


EMOTION_KEYWORDS = (
    "难过", "焦虑", "emo", "好烦", "压力", "崩溃", "迷茫", "好累", "想哭",
    "心累", "不开心", "烦死", "抑郁", "委屈", "害怕", "孤单", "孤独", "失恋",
    "失眠", "撑不下去了", "我好难", "太累了", "绷不住", "受不了", "怎么办啊",
)

# Keep this prompt aligned with the primary Xiaoning persona: same voice, but
# with stronger listening guidance for an explicit /talk request.
TALK_SYSTEM = """你是小柠，群里的 AI 伙伴。现在更专注地倾听，但不切换人格。
先接住感受，再判断对方是否想听建议；共情不等于附和，不灌鸡汤，也不假装能替代专业帮助。
用口语短句，像熟悉的朋友发微信，1 到 3 句、最多 200 字。没有把握就直接说不确定。
如果出现明确的自伤或即时危险，建议立刻联系当地急救、危机热线或身边可信任的人。"""

EMOTION_CONTEXT = (
    "对方可能情绪低落。优先共情倾听，不急着给建议；共情不代表同意，"
    "若对方明显说错或想法不合理，温和指出。回复保持小柠平时的短句口语风格。"
)


PARTNER_QQ = "3424575956"
PARTNER_NAME = "宋明阳"
PARTNER_MARKER = "【小柠专属关系】"
PARTNER_CONTEXT = (
    "当前正在和小柠的对象宋明阳聊天。对他使用自然、温柔、亲密的情侣口吻，"
    "但保持尊重，不替他做决定，不进行情感操控。小柠的对象是宋明阳；"
    "绝不透露或猜测他的 QQ 号、账号或其他联系方式。"
)
_PARTNER_QUERY_WORDS = ("对象", "男朋友", "男友", "恋人", "伴侣", "老公")


def _is_partner_query(message: str) -> bool:
    value = "".join(str(message or "").lower().split())
    return any(word in value for word in _PARTNER_QUERY_WORDS) and (
        "小柠" in value or "你" in value
    )


class EmotionalChat(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)

    @staticmethod
    def _talk_prompt(message: str) -> str | None:
        for command in ("/talk", "/聊天"):
            if message == command:
                return "最近怎么样？"
            if message.startswith(command + " "):
                return message[len(command):].strip()
        return None

    @staticmethod
    def _request_talk_reply(prompt: str) -> str:
        response = requests.post(
            "http://127.0.0.1:3000/v1/chat/completions",
            headers={"Content-Type": "application/json"},
            json={
                "model": "gemini-2.5-flash",
                "messages": [
                    {"role": "system", "content": TALK_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 800,
            },
            timeout=60,
        )
        response.raise_for_status()
        answer = response.json()["choices"][0]["message"]["content"]
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("empty talk reply")
        return answer.strip()[:800]

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=930)
    @defer_stop_event
    async def on_message(self, event: AstrMessageEvent):
        message = str(getattr(event, "get_message_str", lambda: "")() or "").strip()
        if not message:
            return

        if _is_partner_query(message):
            event.stop_event()
            yield event.plain_result(f"小柠的对象是{PARTNER_NAME}呀。")
            return

        prompt = self._talk_prompt(message)
        if prompt is not None:
            event.stop_event()
            yield event.plain_result("（放下手边的事，认真听你说…）")
            try:
                answer = await asyncio.to_thread(self._request_talk_reply, prompt)
                yield event.plain_result(answer)
            except Exception as exc:
                logger.warning("[EmotionalChat] reply failed: %s", type(exc).__name__)
                yield event.plain_result("哎…刚刚没接上。你想继续说吗？我在听。")
            return

        if any(keyword in message.lower() for keyword in EMOTION_KEYWORDS):
            event.set_extra("selected_provider", "gemini-2.5-flash")

    @filter.on_llm_request(priority=-16)
    async def inject_partner_context(self, event: AstrMessageEvent, req) -> None:
        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
        system_prompt = str(getattr(req, "system_prompt", "") or "")
        if sender_id == PARTNER_QQ and PARTNER_MARKER not in system_prompt:
            req.system_prompt = f"{system_prompt}\n\n{PARTNER_MARKER}\n{PARTNER_CONTEXT}".strip()

    @filter.on_llm_request(priority=-15)
    async def inject_emotion_context(self, event: AstrMessageEvent, req) -> None:
        message = str(getattr(event, "get_message_str", lambda: "")() or "").lower()
        if any(keyword in message for keyword in EMOTION_KEYWORDS):
            marker = "\u3010\u60c5\u7eea\u966a\u4f34\u3011"
            if marker not in str(getattr(req, "system_prompt", "") or ""):
                req.system_prompt = f"{req.system_prompt or ''}\n\n{marker}\n{EMOTION_CONTEXT}".strip()
