"""Runtime AI moderation with bounded actions and privacy-minimal context."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque

from astrbot import logger

from .ai_moderation_policy import (
    build_anonymous_context,
    is_candidate,
    parse_decision,
    resolve_action,
    sanitize_message,
)

SYSTEM_PROMPT = """你是谨慎的QQ群内容审核器。输入是匿名化的最近群聊。
只有明确刷屏、批量广告、诈骗诱导、持续定向骚扰或严重人身攻击才算违规。
普通玩笑、朋友调侃、单纯脏话、意见冲突、引用、转述、反讽或语境不清必须判定 none。
只返回一个JSON对象，不要Markdown或解释：
{"decision":"none|recall|recall_and_mute","category":"spam|advertising|fraud|harassment|severe_abuse","confidence":0.0,"reason_code":"repeated_spam|bulk_ad|fraud_lure|targeted_harassment|severe_personal_attack"}
不得输出成员身份、群身份、禁言时长或任何额外动作。"""


class AIModerationHandler:
    def __init__(
        self,
        context,
        store,
        provider_id: str = "deepseek-chat",
        timeout_seconds: float = 8,
        context_messages: int = 8,
        owner_id: str = "1211000567",
    ):
        self.context = context
        self.store = store
        self.provider_id = str(provider_id or "deepseek-chat")
        self.timeout_seconds = max(0.01, min(float(timeout_seconds), 30.0))
        self.context_messages = max(1, min(int(context_messages), 8))
        self.owner_id = str(owner_id)
        self._history: dict[str, deque[tuple[str, str]]] = defaultdict(
            lambda: deque(maxlen=self.context_messages)
        )
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_evaluation: dict[tuple[str, str], float] = {}

    async def _eligible(self, event) -> bool:
        group_id = str(event.get_group_id() or "")
        sender_id = str(event.get_sender_id() or "")
        self_id = str(event.get_self_id() or "")
        if not group_id or not sender_id or not self_id:
            return False
        if sender_id in {self_id, self.owner_id}:
            return False
        try:
            if not self.store.is_enabled(group_id):
                return False
            bot_info, sender_info = await asyncio.gather(
                event.bot.get_group_member_info(
                    group_id=int(group_id), user_id=int(self_id), no_cache=True
                ),
                event.bot.get_group_member_info(
                    group_id=int(group_id), user_id=int(sender_id), no_cache=True
                ),
            )
        except Exception:
            logger.warning("[AIGroupMod] eligibility_check_failed")
            return False
        if str(bot_info.get("role", "")) not in {"admin", "owner"}:
            return False
        return str(sender_info.get("role", "")) not in {"admin", "owner"}

    def _remember_and_count_same(self, group_id: str, sender_id: str, text: str) -> int:
        sanitized = sanitize_message(text)
        history = self._history[group_id]
        same = sum(
            1
            for previous_sender, previous_text in history
            if previous_sender == sender_id and previous_text == sanitized
        )
        history.append((sender_id, sanitized))
        return same

    def _can_evaluate(self, group_id: str, sender_id: str, now: float) -> bool:
        key = (group_id, sender_id)
        previous = self._last_evaluation.get(key, 0.0)
        if now - previous < 5.0:
            return False
        self._last_evaluation[key] = now
        return True

    async def _judge(self, group_id: str) -> str:
        provider = self.context.get_provider_by_id(self.provider_id)
        if provider is None:
            return ""
        prompt = "匿名群聊上下文：\n" + build_anonymous_context(
            list(self._history[group_id]),
            max_messages=self.context_messages,
            max_chars=3000,
        )
        response = await asyncio.wait_for(
            provider.text_chat(system_prompt=SYSTEM_PROMPT, prompt=prompt),
            timeout=self.timeout_seconds,
        )
        return str(getattr(response, "completion_text", "") or "")

    async def _apply(self, event, mute_seconds: int) -> bool:
        try:
            await event.bot.delete_msg(message_id=int(event.message_obj.message_id))
            if mute_seconds > 0:
                await event.bot.set_group_ban(
                    group_id=int(event.get_group_id()),
                    user_id=int(event.get_sender_id()),
                    duration=min(int(mute_seconds), 1800),
                )
            notice = (
                "消息已撤回并执行有限时长禁言，请遵守群聊规则。"
                if mute_seconds > 0
                else "消息已撤回，请遵守群聊规则。"
            )
            try:
                await event.send(event.plain_result(notice))
            except Exception:
                logger.warning("[AIGroupMod] warning_send_failed")
            return True
        except Exception:
            logger.warning("[AIGroupMod] moderation_action_failed")
            return False

    async def handle(self, event) -> None:
        if not await self._eligible(event):
            return
        group_id = str(event.get_group_id())
        sender_id = str(event.get_sender_id())
        text = str(getattr(event, "message_str", "") or "")
        recent_same = self._remember_and_count_same(group_id, sender_id, text)
        if not is_candidate(text, recent_same):
            return
        now = time.time()
        if not self._can_evaluate(group_id, sender_id, now):
            return
        async with self._locks[group_id]:
            try:
                raw = await self._judge(group_id)
            except Exception:
                logger.warning("[AIGroupMod] model_judgement_failed")
                return
            decision = parse_decision(raw)
            if decision.decision == "none":
                return
            try:
                prior_offenses = self.store.offense_count(group_id, sender_id, now)
            except Exception:
                logger.warning("[AIGroupMod] offense_state_read_failed")
                return
            action = resolve_action(decision, prior_offenses)
            if not action.recall:
                return
            success = await self._apply(event, action.mute_seconds)
            try:
                self.store.record_action(
                    group_id,
                    sender_id,
                    "mute" if action.mute_seconds else "recall",
                    action.reason_code,
                    decision.confidence,
                    success,
                    now,
                )
            except Exception:
                logger.warning("[AIGroupMod] audit_write_failed")

    async def terminate(self) -> None:
        self._history.clear()
        self._locks.clear()
        self._last_evaluation.clear()
