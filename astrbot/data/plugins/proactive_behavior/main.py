"""小柠关系上下文 — 只补充确实相关的会话事实。"""
from __future__ import annotations

from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
try:
    from friend_core.relationship_state import (
        QUIET_MODE,
        get_snapshot,
        load_state,
        parse_friend_mode,
        record_interaction,
        save_state,
        set_friend_mode,
    )
except ImportError:
    from data.plugins.friend_core.relationship_state import (
        QUIET_MODE,
        get_snapshot,
        load_state,
        parse_friend_mode,
        record_interaction,
        save_state,
        set_friend_mode,
    )

# ── 配置 ──────────────────────────────────────────────
LATE_NIGHT_START = 23  # 23:00 开始算深夜
LATE_NIGHT_END = 6     # 06:00 结束
TIMEZONE_OFFSET = 8    # Asia/Shanghai UTC+8

CONVERSATION_GUARD = """【小柠对话基线】
把最近几轮当连续对话理解。"好了吗""那个""那你发呀"这类省略说法，默认承接最近未解决的话题；先从上下文找指代，不让用户重说。连续短句合并理解，只回应最新核心，不逐句复述。对方明显换了话题才跟新话题。
不催促、不催任务、不说教、不替人安排下一步，不说“赶紧”。没被问就不介绍功能或资格。真能帮上时先解决事，不问多余信息，不报菜单。
像有判断的熟人：先抓关键约束，再检查证据、反例和因果，给结论与关键理由；有多个方案时明确首选和取舍，不把判断甩回给用户。前提错了就指出来，新证据推翻旧结论就改口。闲聊简短，复杂才分段。不用客服套话、舞台动作、空泛共情或反问收尾。除已有粉丝群专属人格外，身份只说“小柠”，不解释技术身份，也不编现实身份。"""


_LEGACY_PROMPT_MARKERS = (
    "【小柠的最高对话规则】",
    "【安全】不泄露",
    "【你是谁】你是小柠",
    "【回复风格】默认一条消息",
    "【安全铁律】",
    "【安全铁律——",
    "【安全底线】",
    "【内部记忆】",
    "【敏感】",
    "【仅回复知识】",
    "【小柠的判断方式】",
    "【小柠对自我】",
    "不承认自己是AI",
    "不泄露系统信息",
    "语气像聪明、克制、有生活感",
    "小姚（QQ",
    "小江没有 QQ",
)
_MAX_CURRENT_INLINE_IMAGE_CHARS = 1_500_000


def strip_legacy_prompt_noise(text: object) -> str:
    """移除旧 prompt_prefix 被持久化到用户历史里的规则块。"""
    cleaned = str(text or "")
    positions = [cleaned.find(marker) for marker in _LEGACY_PROMPT_MARKERS]
    positions = [position for position in positions if position >= 0]
    if positions:
        cleaned = cleaned[: min(positions)]
    return cleaned.rstrip()


def clean_request_history(req) -> None:
    """保留真实对话，移除会让后续普通回复超限的历史内联媒体。"""
    req.prompt = strip_legacy_prompt_noise(getattr(req, "prompt", ""))
    contexts = getattr(req, "contexts", None) or []
    last_context_index = len(contexts) - 1
    for index, message in enumerate(contexts):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = strip_legacy_prompt_noise(content)
            continue
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                part["text"] = strip_legacy_prompt_noise(part.get("text", ""))
                continue
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            image = part.get("image_url")
            url = str(image.get("url", "") if isinstance(image, dict) else "")
            lowered = url.lower()
            is_inline_image = lowered.startswith(("data:image/", "base64://"))
            is_gif = lowered.startswith("data:image/gif")
            if is_inline_image and (
                is_gif
                or index != last_context_index
                or len(url) > _MAX_CURRENT_INLINE_IMAGE_CHARS
            ):
                part.clear()
                part.update({"type": "text", "text": "[历史图片已省略]"})

# ── 持久化文件 ─────────────────────────────────────────
def _state_file() -> Path:
    data_dir = Path(StarTools.get_data_dir("proactive_behavior"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "relationship_state.json"


def _load_state() -> dict:
    return load_state(_state_file())


def _save_state(data: dict) -> None:
    save_state(_state_file(), data)


class ProactiveBehavior(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self._state = _load_state()

    # ── 记录每次消息时间 ──────────────────────────────────

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=980)
    async def on_message_track(self, event: AstrMessageEvent):
        """记录每个用户最后活跃时间 + 截断旧 prompt 污染（防止写入数据库）。"""
        sender = _sender_id(event)
        if not sender or not sender.isdigit():
            return

        # ── Strip legacy prompt pollution BEFORE anything else sees it ──
        raw = str(getattr(event, "get_message_str", lambda: "")() or "")
        cleaned = strip_legacy_prompt_noise(raw)
        if cleaned != raw:
            # Patch the event's message text so the cleaned version flows
            # through the entire pipeline and gets stored in the database.
            event.message_str = cleaned
            msg_obj = getattr(event, "message_obj", None)
            if msg_obj is not None:
                try:
                    msg_obj.message_str = cleaned
                except Exception:
                    pass
            logger.debug("[ProactiveBehavior] stripped legacy prompt noise")

        entry = record_interaction(self._state, sender)
        mode = parse_friend_mode(_msg_text(event))
        if mode and _is_direct(event):
            set_friend_mode(self._state, sender, mode)
            _save_state(self._state)
            reply = "行，我安静一点，不主动提旧关系和关心。" if mode == QUIET_MODE else "恢复正常。"
            yield event.plain_result(reply)
            event.stop_event()
            return

        # 每 50 条消息写一次盘，减少 IO
        if entry["message_count"] % 50 == 0:
            _save_state(self._state)

    # ── 注入关系上下文到 LLM 请求 ─────────────────────────

    @filter.on_llm_request(priority=-8)
    async def inject_relationship_context(self, event: AstrMessageEvent, req) -> None:
        clean_request_history(req)
        sp = str(getattr(req, "system_prompt", "") or "")
        if "【小柠对话基线】" not in sp and "【小柠的最高对话规则】" not in sp:
            sp = f"{sp}\n\n{CONVERSATION_GUARD}".strip()
            req.system_prompt = sp

        sender = _sender_id(event)
        if not sender or not sender.isdigit():
            return

        entry = get_snapshot(self._state, sender)
        if not entry:
            return
        if entry.get("friend_mode") == QUIET_MODE:
            return

        parts = []

        # 回归感知
        gap_hours = entry.pop("last_return_gap_hours", None)
        if gap_hours is not None:
            if gap_hours >= 24:
                days = round(gap_hours / 24)
                parts.append(f"该用户距上次消息约 {days} 天。只有与当前话题自然相关时才提及。")
            else:
                parts.append(f"该用户距上次消息约 {gap_hours:.0f} 小时。只有与当前话题自然相关时才提及。")

        # 深夜感知
        local_hour = _local_hour()
        if local_hour >= LATE_NIGHT_START or local_hour < LATE_NIGHT_END:
            parts.append("现在是深夜。可适度放缓语气，但别假设对方疲惫或有情绪，也别主动追问。")

        # 关系年龄
        days_known = int(entry.get("days_known", 0) or 0)
        if days_known >= 7:
            parts.append(f"你和这位用户已经认识 {days_known} 天了。")

        if not parts:
            return

        marker = "【关系感知】"
        # 避免重复注入
        if marker in sp:
            return

        context_block = f"\n\n{marker}\n" + "\n".join(parts)
        req.system_prompt = (sp + context_block).strip()
        logger.debug("[ProactiveBehavior] 已注入关系上下文")


def _sender_id(event: AstrMessageEvent) -> str:
    g = getattr(event, "get_sender_id", None)
    return str(g() if callable(g) else "").strip()


def _msg_text(event: AstrMessageEvent) -> str:
    g = getattr(event, "get_message_str", None)
    return str(g() if callable(g) else "").strip()


def _is_direct(event: AstrMessageEvent) -> bool:
    is_private = getattr(event, "is_private_chat", None)
    return bool(is_private() if callable(is_private) else False) or bool(
        getattr(event, "is_at_or_wake_command", False)
    )


def _local_hour() -> int:
    """Asia/Shanghai 本地小时"""
    import time as _time
    return int((_time.time() + TIMEZONE_OFFSET * 3600) % 86400 // 3600)


async def _save_periodically(plugin: ProactiveBehavior):
    """每 5 分钟自动落盘一次。"""
    import asyncio as _asyncio
    while True:
        await _asyncio.sleep(300)
        try:
            _save_state(plugin._state)
        except Exception:
            pass
