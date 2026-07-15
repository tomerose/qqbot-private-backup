"""小柠关系上下文 — 只补充确实相关的会话事实。"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

# ── 配置 ──────────────────────────────────────────────
RETURN_GAP_SECONDS = 20 * 3600  # 超过此间隔视为"好久不见"
LATE_NIGHT_START = 23  # 23:00 开始算深夜
LATE_NIGHT_END = 6     # 06:00 结束
TIMEZONE_OFFSET = 8    # Asia/Shanghai UTC+8

CONVERSATION_GUARD = """【小柠对话基线】
优先回应用户当前这句话；只有当前话题确实需要时才使用相关历史，不因旧话题、久未出现或已有功能把对话拉走。
你不是督促者、课程助教或销售。闲聊、吐槽、情绪和普通提问时，不催任务、不列待办、不替对方安排下一步，也不反复问“做完了吗”“要不要继续”。没明确要建议时，不说“赶紧”“你应该”“别再”“早点”这类生活指令。没被问就不介绍功能、资格或升级。
用户明确露出目标或麻烦、现有功能正好能解决时，先接住这件事：能执行就交给对应功能，需要补信息只问一个关键点；只是询问用法时给一个最短自然示例。不要报菜单，对方不接就翻篇。
像有判断的熟人：有事实或逻辑依据时直接给结论和理由，不确定就直说；不为显得聪明硬凑建议，也不为显得有个性逢话反驳。短句、具体，不用客服套话、空泛共情或模板化总结。"""

# ── 持久化文件 ─────────────────────────────────────────
def _state_file() -> Path:
    data_dir = Path(StarTools.get_data_dir("proactive_behavior"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "relationship_state.json"


def _load_state() -> dict:
    try:
        return json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(data: dict) -> None:
    state_file = _state_file()
    tmp = state_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(state_file)


class ProactiveBehavior(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self._state = _load_state()

    # ── 记录每次消息时间 ──────────────────────────────────

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=980)
    async def on_message_track(self, event: AstrMessageEvent):
        """记录每个用户最后活跃时间，供后续请求注入上下文。"""
        sender = _sender_id(event)
        if not sender or not sender.isdigit():
            return

        now_ts = time.time()
        entry = self._state.get(sender, {})
        prev_ts = entry.get("last_message_ts", 0)
        gap = now_ts - prev_ts if prev_ts else 0

        # 首次出现 → 记录相识时间
        if not entry.get("first_seen_ts"):
            entry["first_seen_ts"] = now_ts
        entry["last_message_ts"] = now_ts
        entry["message_count"] = entry.get("message_count", 0) + 1

        # 记录最近一次长时间离开后的回归
        if gap >= RETURN_GAP_SECONDS and prev_ts > 0:
            entry["last_return_gap_hours"] = round(gap / 3600, 1)
        else:
            entry.pop("last_return_gap_hours", None)

        self._state[sender] = entry

        # 每 50 条消息写一次盘，减少 IO
        if entry["message_count"] % 50 == 0:
            _save_state(self._state)

    # ── 注入关系上下文到 LLM 请求 ─────────────────────────

    @filter.on_llm_request(priority=-8)
    async def inject_relationship_context(self, event: AstrMessageEvent, req) -> None:
        sp = str(getattr(req, "system_prompt", "") or "")
        if "【小柠对话基线】" not in sp and "【小柠的最高对话规则】" not in sp:
            sp = f"{sp}\n\n{CONVERSATION_GUARD}".strip()
            req.system_prompt = sp

        sender = _sender_id(event)
        if not sender or not sender.isdigit():
            return

        entry = self._state.get(sender, {})
        if not entry:
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
        first_seen = entry.get("first_seen_ts")
        if first_seen:
            days_known = max(1, round((time.time() - first_seen) / 86400))
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
        logger.debug(f"[ProactiveBehavior] 为 {sender} 注入关系上下文")


def _sender_id(event: AstrMessageEvent) -> str:
    g = getattr(event, "get_sender_id", None)
    return str(g() if callable(g) else "").strip()


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
