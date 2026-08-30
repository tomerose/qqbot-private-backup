"""Runtime AI moderation with bounded actions and privacy-minimal context."""

from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict, deque

from astrbot import logger

from .ai_moderation_policy import (
    build_anonymous_context,
    is_candidate,
    matches_ai_identity_attack,
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

DEFAULT_IDENTITY_REBUTTAL = (
    "严正警告：本群已明令禁止提及“人工智障”“人机”“机器人”“AI”。"
    "无论玩笑、引用、讨论或是否指向小柠，出现即触发；请立即停止，勿再试探规则。"
)

DEFAULT_INSULT_WARNING = (
    "警告：群聊可以尖锐，但别用辱骂代替论证。请针对观点说话，停止人身攻击。"
)


class AIModerationHandler:
    def __init__(
        self,
        context,
        store,
        provider_id: str = "gemini-2.5-flash",
        timeout_seconds: float = 8,
        context_messages: int = 8,
        owner_id: str = "",
        ai_moderation_group_ids: set[str] | list[str] | tuple[str, ...] = (),
        identity_guard_enabled: bool = False,
        identity_guard_group_ids: set[str] | list[str] | tuple[str, ...] = (),
        identity_guard_terms: set[str] | list[str] | tuple[str, ...] = (
            "人工智障",
            "人机",
            "机器人",
            "AI",
        ),
        identity_guard_mute_seconds: int = 60,
        identity_guard_rebuttal: str = DEFAULT_IDENTITY_REBUTTAL,
        insult_warning_enabled: bool = False,
        insult_warning_group_ids: set[str] | list[str] | tuple[str, ...] = (),
        insult_warning_terms: set[str] | list[str] | tuple[str, ...] = (),
        insult_warning_text: str = DEFAULT_INSULT_WARNING,
    ):
        self.context = context
        self.store = store
        self.provider_id = str(provider_id or "gemini-2.5-flash")
        self.timeout_seconds = max(0.01, min(float(timeout_seconds), 30.0))
        self.context_messages = max(1, min(int(context_messages), 8))
        self.owner_id = str(owner_id)
        self.ai_moderation_group_ids = {
            str(group_id) for group_id in ai_moderation_group_ids if str(group_id)
        }
        self.identity_guard_enabled = bool(identity_guard_enabled)
        self.identity_guard_group_ids = {
            str(group_id) for group_id in identity_guard_group_ids if str(group_id)
        }
        self.identity_guard_terms = tuple(
            str(term) for term in identity_guard_terms if str(term)
        )
        self.identity_guard_mute_seconds = max(
            1, min(int(identity_guard_mute_seconds), 1800)
        )
        self.identity_guard_rebuttal = (
            str(identity_guard_rebuttal or "").strip() or DEFAULT_IDENTITY_REBUTTAL
        )
        self.insult_warning_enabled = bool(insult_warning_enabled)
        self.insult_warning_group_ids = {
            str(group_id) for group_id in insult_warning_group_ids if str(group_id)
        }
        self.insult_warning_terms = tuple(
            self._normalize_warning_text(term)
            for term in insult_warning_terms
            if self._normalize_warning_text(term)
        )
        self.insult_warning_text = (
            str(insult_warning_text or "").strip() or DEFAULT_INSULT_WARNING
        )
        self._history: dict[str, deque[tuple[str, str]]] = defaultdict(
            lambda: deque(maxlen=self.context_messages)
        )
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_evaluation: dict[tuple[str, str], float] = {}
        self._bot_moderation_cache: dict[str, tuple[bool, float]] = {}

    async def _eligible(self, event) -> bool:
        group_id = str(event.get_group_id() or "")
        sender_id = str(event.get_sender_id() or "")
        self_id = str(event.get_self_id() or "")
        if not group_id or not sender_id or not self_id:
            return False
        if sender_id in {self_id, self.owner_id}:
            return False
        now = time.monotonic()
        cached = self._bot_moderation_cache.get(group_id)
        if cached is None or cached[1] <= now:
            try:
                bot_info = await event.bot.get_group_member_info(
                    group_id=int(group_id), user_id=int(self_id), no_cache=True
                )
            except Exception:
                self._bot_moderation_cache[group_id] = (False, now + 60)
                logger.warning("[AIGroupMod] bot_role_lookup_failed")
                return False
            can_moderate = str(bot_info.get("role", "")) in {"admin", "owner"}
            self._bot_moderation_cache[group_id] = (can_moderate, now + 60)
        else:
            can_moderate = cached[0]
        if not can_moderate:
            return False
        try:
            sender_info = await event.bot.get_group_member_info(
                group_id=int(group_id), user_id=int(sender_id), no_cache=True
            )
        except Exception:
            logger.debug("[AIGroupMod] sender_role_lookup_failed")
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

    async def _identity_warning_allowed(self, event) -> bool:
        """Allow a warning only for an ordinary member; lookup failure stays silent."""
        try:
            sender_info = await event.bot.get_group_member_info(
                group_id=int(event.get_group_id()),
                user_id=int(event.get_sender_id()),
                no_cache=True,
            )
        except Exception:
            logger.debug("[AIGroupMod] identity_sender_role_lookup_failed")
            return False
        return str(sender_info.get("role", "")) not in {"admin", "owner"}

    async def _send_identity_rebuttal(self, event) -> bool:
        try:
            await event.send(event.plain_result(self.identity_guard_rebuttal))
        except Exception:
            logger.warning("[AIGroupMod] identity_rebuttal_send_failed")
            return False
        try:
            event.stop_event()
        except Exception:
            pass
        return True

    @staticmethod
    def _normalize_warning_text(text: str) -> str:
        return re.sub(r"[\s._\-*]+", "", str(text or "").casefold())

    def _matches_insult_warning(self, text: str) -> bool:
        normalized = self._normalize_warning_text(text)
        return bool(normalized) and any(
            term in normalized for term in self.insult_warning_terms
        )

    async def _send_insult_warning(self, event) -> bool:
        try:
            await event.send(event.plain_result(self.insult_warning_text))
        except Exception:
            logger.warning("[AIGroupMod] insult_warning_send_failed")
            return False
        try:
            event.stop_event()
        except Exception:
            pass
        return True

    async def _apply_identity_guard(self, event) -> bool:
        """A visible rebuttal must succeed before the bounded mute is attempted."""
        if not await self._send_identity_rebuttal(event):
            return False
        try:
            await event.bot.set_group_ban(
                group_id=int(event.get_group_id()),
                user_id=int(event.get_sender_id()),
                duration=self.identity_guard_mute_seconds,
            )
            return True
        except Exception:
            logger.warning("[AIGroupMod] identity_mute_failed")
            return False

    async def handle(self, event) -> None:
        group_id = str(event.get_group_id() or "")
        sender_id = str(event.get_sender_id() or "")
        self_id = str(event.get_self_id() or "")
        if not group_id or not sender_id or sender_id in {self_id, self.owner_id}:
            return
        identity_guard_match = (
            self.identity_guard_enabled
            and group_id in self.identity_guard_group_ids
            and matches_ai_identity_attack(
                getattr(event, "message_str", ""), self.identity_guard_terms
            )
        )
        if identity_guard_match:
            can_enforce = await self._eligible(event)
            if not can_enforce and not await self._identity_warning_allowed(event):
                return
            async with self._locks[group_id]:
                if can_enforce:
                    action_type = "mute"
                    success = await self._apply_identity_guard(event)
                else:
                    action_type = "warn"
                    success = await self._send_identity_rebuttal(event)
                try:
                    self.store.record_action(
                        group_id,
                        sender_id,
                        action_type,
                        "targeted_harassment",
                        1.0,
                        success,
                        time.time(),
                    )
                except Exception:
                    logger.warning("[AIGroupMod] identity_audit_write_failed")
            return
        insult_warning_match = (
            self.insult_warning_enabled
            and group_id in self.insult_warning_group_ids
            and self._matches_insult_warning(getattr(event, "message_str", ""))
        )
        if insult_warning_match:
            if not await self._identity_warning_allowed(event):
                return
            async with self._locks[group_id]:
                success = await self._send_insult_warning(event)
                try:
                    self.store.record_action(
                        group_id,
                        sender_id,
                        "warn",
                        "insult_language",
                        1.0,
                        success,
                        time.time(),
                    )
                except Exception:
                    logger.warning("[AIGroupMod] insult_warning_audit_write_failed")
            return
        if (
            group_id not in self.ai_moderation_group_ids
            and not self.store.is_enabled(group_id)
        ):
            return
        text = str(getattr(event, "message_str", "") or "")
        recent_same = self._remember_and_count_same(group_id, sender_id, text)
        if not is_candidate(text, recent_same):
            return
        if not await self._eligible(event):
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
        self._bot_moderation_cache.clear()
