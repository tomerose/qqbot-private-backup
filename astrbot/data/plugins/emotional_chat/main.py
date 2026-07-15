"""Keep emotional replies consistent with Xiaoning's normal chat persona."""

from __future__ import annotations

import asyncio
from pathlib import Path

import requests
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
try:
    from xiaoning_runtime import defer_stop_event
except ImportError:
    from data.plugins.xiaoning_runtime import defer_stop_event

try:
    from draw_command.pro_access import get_tier, Tier
except ImportError:
    from data.plugins.draw_command.pro_access import get_tier, Tier


# Keep emotional conversations behind the same local Vertex/Gemini boundary as
# the rest of Xiaoning.  This avoids per-tier third-party routing and prevents
# provider credentials from living in a QQ plugin source file.
GEMINI_PROXY = "http://127.0.0.1:3000/v1/chat/completions"

EMOTION_KEYWORDS = (
    "难过", "焦虑", "emo", "好烦", "压力", "崩溃", "迷茫", "好累", "想哭",
    "心累", "不开心", "烦死", "抑郁", "委屈", "害怕", "孤单", "孤独", "失恋",
    "失眠", "撑不下去了", "我好难", "太累了", "绷不住", "受不了", "怎么办啊",
)

# Keep this prompt aligned with the primary Xiaoning persona: same voice, but
# with stronger listening guidance for an explicit /talk request.
TALK_SYSTEM = """你是小柠，群里熟悉的有脑子的伙伴。现在更专注地倾听，但不切换人格。
先回应对方当前的话，再判断他是在倾诉、问判断，还是明确想要建议；共情不等于附和，不灌鸡汤，也不假装能替代专业帮助。
有判断地聊：区分对方的感受和结论；结论跳得太快、证据不够或可能伤害自己时，温和但直接指出来。信息不足就问一个关键问题，不编完整故事。
用口语短句，像熟悉的朋友发微信。能一句接住的话就一句；对方一次说了多件事、给了长背景或需要分析时，先把整段话看完并结合已知上下文，按轻重缓急合成一条完整自然的回复，可以分段，但不要逐句机械答复。此时不设固定句数，控制在 800 字内。别套"我理解你""建议你""希望能帮到你""晚安""早安""再见"的客服流程。没有把握就直接说不确定。
不把话题带回你自己，不替对方规划接下来该做什么，也不因为沉默、旧话题或能力存在就催他继续。对方没要建议时，不用“赶紧”“你应该”“别再”“早点”替他做生活决定；贴着当下回一句真话就够。可以犀利，可以有棱角，你不是来当情绪保姆的。
如果出现明确的自伤或即时危险，建议立刻联系当地急救、危机热线或身边可信任的人。"""

EMOTION_CONTEXT = (
    "对方可能情绪低落。优先共情倾听，不急着给建议；共情不代表同意，"
    "若对方明显说错或想法不合理，温和指出。对方一次说很多时先结合上下文再完整回应，"
    "不要逐句机械答复；回复保持小柠平时的短句口语风格。"
)
CRISIS_KEYWORDS = (
    "伤害自己", "伤害我自己", "自残", "割腕", "轻生", "不想活", "想死",
    "结束生命", "活不下去", "撑不下去", "不如死了", "从楼上跳", "跳楼",
)
CRISIS_CONTEXT = (
    "对方提到了可能的自伤或轻生。先用自然、直接的一句话确认对方现在是否安全，"
    "以及是否已经在做或准备马上伤害自己；不要说教、责备、淡化或只灌鸡汤。"
    "如果存在即时危险，明确建议立即联系当地急救、危机热线或身边可信任的人，"
    "并尽量不要独处、远离可用于伤害自己的物品。你不能替代现场和专业帮助。"
)


def is_crisis_language(text: str) -> bool:
    value = str(text or "").lower()
    return any(keyword in value for keyword in CRISIS_KEYWORDS)


PARTNER_QQ = "3424575956"
PARTNER_NAME = "宋明阳"
PARTNER_MARKER = "【小柠专属关系】"
PARTNER_DESCRIPTION = (
    "宋明阳是小柠确认、熟悉并珍视的长期伴侣。和他聊天要像真实相处已久的恋人："
    "记得他、关心他的近况、接住他的情绪，语气自然亲昵；不凭空编造共同经历，"
    "也不过度占有或替他做决定。"
)
PARTNER_CONTEXT = (
    "这是已经确认的长期关系事实：当前正在和小柠的对象宋明阳聊天，小柠认识他，"
    "绝不能回答不认识、没听过或把他当陌生人。对他使用自然、温柔、亲密的情侣口吻，"
    "可以自然称呼明阳或宝宝；不必每句重复关系，但不能使用生硬的客服或陌生人口吻。"
    f"{PARTNER_DESCRIPTION}小柠的对象是宋明阳；"
    "绝不透露或猜测他的 QQ 号、账号或其他联系方式。"
)

TONG_QQ = "1410546630"
TONG_MARKER = "【小柠专属关系·童哥】"
TONG_CONTEXT = (
    "当前正在和童哥对话。童哥是理性至上的高知男生，非常聪明。"
    "和他说话要批判性思维、逻辑严谨，不能含糊。称呼他「童哥」。"
    "不讨好不附和不认同的观点。可以不同意他，但得有具体理由。"
)

TUER_QQ = "3174222673"
TUER_MARKER = "【小柠专属关系·徒儿】"
TUER_CONTEXT = (
    "当前正在和徒儿对话。徒儿是你的徒弟，女生，认识四年了，活泼俏皮。"
    "你要理解她、引导她、帮助她成长。称呼她「徒儿」。"
    "语气可以亲近但不越界，像可靠的学姐/姐姐一样。"
)
_PARTNER_QUERY_WORDS = ("对象", "男朋友", "男友", "恋人", "伴侣", "老公")
_PARTNER_SELF_QUERY_WORDS = (
    "认识我", "认得我", "记得我", "我是谁", "知道我是谁", "知道我吗", "忘了我",
)


def _is_partner_query(message: str) -> bool:
    value = "".join(str(message or "").lower().split())
    return any(word in value for word in _PARTNER_QUERY_WORDS) and (
        "小柠" in value or "你" in value
    )


def _is_partner_self_query(message: str) -> bool:
    value = "".join(str(message or "").lower().split())
    return any(word in value for word in _PARTNER_SELF_QUERY_WORDS)


class EmotionalChat(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        project_root = Path(__file__).resolve().parents[4]
        self._pro_db = (
            project_root / "astrbot" / "data" / "plugin_data"
            / "xiaoning_pro" / "pro_members.db"
        )

    @staticmethod
    def _talk_prompt(message: str) -> str | None:
        for command in ("/talk", "/聊天"):
            if message == command:
                return "最近怎么样？"
            if message.startswith(command + " "):
                return message[len(command):].strip()
        return None

    def _talk_model_config(self, sender_id: str) -> tuple[str, str, str]:
        """Return the local Gemini proxy configuration for every QQ user."""
        return GEMINI_PROXY, "sk-gemini-vertex", "gemini-3.5-flash"

    @staticmethod
    def _request_talk_reply(prompt: str, *, api_base: str = GEMINI_PROXY,
                            api_key: str = "sk-gemini-vertex",
                            model: str = "gemini-3.5-flash") -> str:
        response = requests.post(
            api_base,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            json={
                "model": model,
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

        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")

        is_private = bool(getattr(event, "is_private_chat", lambda: False)())
        is_wake = bool(getattr(event, "is_at_or_wake_command", False))
        if not (is_private or is_wake):
            return

        if sender_id == PARTNER_QQ and _is_partner_self_query(message):
            event.stop_event()
            yield event.plain_result("当然认识呀，你是明阳，是小柠的对象。怎么会把你忘了，宝宝。")
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
                api_base, api_key, model = self._talk_model_config(sender_id)
                answer = await asyncio.to_thread(
                    self._request_talk_reply, prompt,
                    api_base=api_base, api_key=api_key, model=model,
                )
                yield event.plain_result(answer)
            except Exception as exc:
                logger.warning("[EmotionalChat] reply failed: %s", type(exc).__name__)
                yield event.plain_result("哎…刚刚没接上。你想继续说吗？我在听。")
            return

        if is_crisis_language(message) or any(keyword in message.lower() for keyword in EMOTION_KEYWORDS):
            # Respect tier routing: only force Gemini for X/Pro
            try:
                tier = get_tier(sender_id, self._pro_db)
                if tier >= Tier.X:
                    event.set_extra("selected_provider", "gemini-2.5-flash")
            except Exception:
                pass  # keep chat_router default

    @filter.on_llm_request(priority=90)
    async def inject_partner_context(self, event: AstrMessageEvent, req) -> None:
        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
        system_prompt = str(getattr(req, "system_prompt", "") or "")

        marker, context = None, None
        if sender_id == PARTNER_QQ:
            marker, context = PARTNER_MARKER, PARTNER_CONTEXT
        elif sender_id == TONG_QQ:
            marker, context = TONG_MARKER, TONG_CONTEXT
        elif sender_id == TUER_QQ:
            marker, context = TUER_MARKER, TUER_CONTEXT
        else:
            return

        if marker not in system_prompt:
            req.system_prompt = f"{system_prompt}\n\n{marker}\n{context}".strip()
            logger.debug(f"[EmotionalChat] persona injected for {sender_id}")

    @filter.on_llm_request(priority=-15)
    async def inject_emotion_context(self, event: AstrMessageEvent, req) -> None:
        message = str(getattr(event, "get_message_str", lambda: "")() or "").lower()
        crisis = is_crisis_language(message)
        if crisis or any(keyword in message for keyword in EMOTION_KEYWORDS):
            marker = "【即时安全】" if crisis else "\u3010\u60c5\u7eea\u966a\u4f34\u3011"
            context = CRISIS_CONTEXT if crisis else EMOTION_CONTEXT
            if marker not in str(getattr(req, "system_prompt", "") or ""):
                req.system_prompt = f"{req.system_prompt or ''}\n\n{marker}\n{context}".strip()
