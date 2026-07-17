"""Friend Core — makes 小柠 feel like a friend, not a tool.

P0: Personality injection into all chat paths
P1: Memory-triggered proactive check-ins
P2: Relationship warmth tracking
P3: Firestore metadata persistence
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, StarTools

try:
    from google.cloud import firestore
    from google.cloud.firestore import Client as FirestoreClient
except ImportError:
    firestore = None
    FirestoreClient = None

from .checkin_scheduler import CheckinScheduler
from .commitment_tracker import on_llm_request_extract, on_llm_request_inject
from .persona_prompt import (
    build_persona_prompt,
    sanitize_conversational_reply,
    sanitize_unverified_artifact_reply,
)
from .scheduled_actions import get_store, POLL_INTERVAL_SECONDS
from .delivery_queue import get_queue
from .birthday import birthday_greeting, generate_birthday_song, is_due_birthday, parse_explicit_birthday
from .gift_store import GiftStore, consent_notice
from .group_help import parse_group_help_confirmation, screen_group_help
from .relationship_state import (
    QUIET_MODE,
    can_send_proactive,
    get_snapshot,
    load_state,
    record_proactive_send,
    save_state,
)

try:
    from draw_command.pro_access import Tier, get_tier
except ImportError:
    from data.plugins.draw_command.pro_access import Tier, get_tier

PERSONA_MARKER = "【小柠人格】"
FIRESTORE_PROJECT = "solar-modem-496213-f5"
FIRESTORE_DATABASE = "qqbot"
WARMTH_CACHE_TTL = 300  # 5 min cache before re-reading from Firestore
GROUP_HELP_COOLDOWN_SECONDS = 600
BIRTHDAY_SEND_HOUR = 9


class FriendCore(Star):
    """Friend Core plugin — personality, memory-driven care, relationship warmth."""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}
        self.enabled = bool(self.config.get("enabled", True))
        self._scheduler = CheckinScheduler(context, self._send_memory_checkin)
        self._db: FirestoreClient | None = None
        # warmth cache: {qq_id: (warmth_score, cached_at)}
        self._warmth_cache: dict[str, tuple[float, float]] = {}
        self._poll_task: asyncio.Task | None = None
        self._last_group_help_at: dict[str, float] = {}
        self._birthday_scan_day = ""
        self._birthday_song_root = Path(__file__).resolve().parents[4] / "claude_workspace" / "birthday_songs"
        self._group_help_llm = bool(self.config.get("group_help_llm", True))
        self._pro_db = Path(
            self.config.get(
                "pro_db_path",
                Path(__file__).resolve().parents[2]
                / "plugin_data"
                / "xiaoning_pro"
                / "pro_members.db",
            )
        )
        self._gift_admin_qq = str(self.config.get("gift_admin_qq", "1211000567"))
        self._gift_enabled = bool(self.config.get("gift_enabled", True)) and self._gift_admin_qq.isdigit()
        gift_root = Path(__file__).resolve().parents[2] / "plugin_data" / "friend_core"
        try:
            self._gift_store = GiftStore(gift_root / "birthday_gifts.sqlite3") if self._gift_enabled else None
        except Exception as exc:
            self._gift_store = None
            logger.warning("[FriendCore] birthday gift vault unavailable: %s", type(exc).__name__)

    @property
    def db(self) -> FirestoreClient | None:
        if self._db is not None:
            return self._db
        if firestore is None:
            return None
        try:
            self._db = firestore.Client(
                project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE
            )
        except Exception as e:
            logger.warning(f"[FriendCore] Firestore 连接失败: {e}")
            return None
        return self._db

    async def initialize(self):
        if self.enabled:
            await self._scheduler.start()
            if self._gift_store:
                await asyncio.to_thread(self._gift_store.purge_expired)
            # ── Google ecosystem: Firestore-backed scheduled action poller + delivery worker ──
            store = get_store()
            queue = get_queue()
            if self.db:
                # Set callbacks so pollers can send QQ messages
                store._send_fn = self._send_reminder_message
                queue._send_fn = self._send_reminder_message
                queue._deliver_fn = self._napcat_deliver_file
                # Start unified poll loop (reminders + delivery retries)
                self._poll_task = asyncio.create_task(self._poll_loop())
                logger.info("[FriendCore] 定时行动 + 交付队列轮询已启动 (每%d秒)", POLL_INTERVAL_SECONDS)
            log_msg = "[FriendCore] Friend Core 已启动"
            if self.db:
                log_msg += " (人格注入 + 记忆关怀 + 定时行动 + 交付队列 + 温度持久化)"
            else:
                log_msg += " (人格注入 + 记忆关怀, 温度仅内存)"
            logger.info(log_msg)

    async def terminate(self):
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None
        await self._scheduler.stop()

    # ── P0: Personality injection ──────────────────────────────────

    def _tier_for(self, sender: str) -> Tier:
        if not sender.isdigit():
            return Tier.ORDINARY
        try:
            return get_tier(sender, self._pro_db)
        except Exception:
            return Tier.ORDINARY

    def _quiet_mode_for(self, sender: str) -> bool:
        if not sender.isdigit():
            return False
        try:
            data_dir = Path(StarTools.get_data_dir("proactive_behavior"))
            state = load_state(data_dir / "relationship_state.json")
            return get_snapshot(state, sender).get("friend_mode") == QUIET_MODE
        except Exception:
            return False

    async def _send_memory_checkin(self, qq_id: str, message: str) -> bool:
        """Send a due memory check-in through the same opt-out and cooldown gate."""
        if self._tier_for(qq_id) < Tier.X:
            return False
        state_path = Path(StarTools.get_data_dir("proactive_behavior")) / "relationship_state.json"
        state = load_state(state_path)
        if not can_send_proactive(state, qq_id, 6 * 3600):
            return False
        cleaned = sanitize_conversational_reply(
            sanitize_unverified_artifact_reply(message)
        ).strip()
        if not cleaned or len(cleaned) > 160:
            return False
        sent = await self._send_reminder_message(qq_id, cleaned)
        if sent:
            record_proactive_send(state, qq_id)
            save_state(state_path, state)
        return sent

    @filter.on_llm_request(priority=-5)
    async def inject_persona(self, event: AstrMessageEvent, req) -> None:
        """Inject 小柠's personality + tier awareness into every LLM request."""
        if not self.enabled:
            return
        if event.get_extra("_skip_persona", False):
            return

        sp = str(getattr(req, "system_prompt", "") or "")
        if PERSONA_MARKER in sp:
            return

        # Membership has one authority: the signed local X/Pro store.
        sender = str(getattr(event, "get_sender_id", lambda: "")() or "")
        quiet_mode = self._quiet_mode_for(sender)
        tier = self._tier_for(sender)
        if tier == Tier.PRO:
            tier_block = "\n\n【当前用户资格】Pro（邀请制）。使用 Pro 能力与额度；具体限制由对应功能执行。"
        elif tier == Tier.X:
            tier_block = "\n\n【当前用户资格】X（QQ 好友）。可使用个人长期记忆和 X 能力；具体额度由对应功能执行。"
        else:
            tier_block = "\n\n【当前用户资格】普通。可使用基础聊天、群上下文和标准语音；不得假装拥有 X/Pro 专属能力或个人长期记忆。"

        voice_block = ""
        if event.get_extra("voice_reply_requested", False):
            voice_block = (
                "\n\n【本轮语音回复】直接回答当前消息，写成自然口语，不念菜单、路径或内部状态。"
            )
            if tier >= Tier.X:
                voice_block += "只可使用系统已注入且与当前话题相关的本人记忆来个性化，群聊中不得带出私密信息。"
            else:
                voice_block += "普通用户只使用当前会话和本群公开上下文，不声称记得跨会话个人信息。"

        # Group follow-up: if this sender just @mentioned us, hint the bot to continue
        followup_block = ""
        if not quiet_mode and not event.is_private_chat() and sender.isdigit() and self.db:
            try:
                gid = str(getattr(event, "get_group_id", lambda: "")() or "")
                doc = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.db.collection("groups").document(gid)
                        .collection("state").document("active_speaker").get
                    ),
                    timeout=4.0,
                )
                if doc.exists:
                    data = doc.to_dict() or {}
                    if data.get("qq") == sender:
                        elapsed = time.time() - float(data.get("at", 0))
                        if elapsed < 300:  # within 5 minutes
                            followup_block = (
                                f"\n\n【群聊连续对话】{sender} 刚才 @了你，现在又发了一条消息。"
                                "以朋友口吻自然接话，不要重复之前的内容，像正常聊天一样回应。"
                            )
                            asyncio.create_task(self._clear_active_speaker(gid))
            except Exception:
                pass

        warmth = 0.0 if quiet_mode else float(event.get_extra("_friend_warmth", 0) or 0)
        persona_block = build_persona_prompt(warmth)
        req.system_prompt = (
            sp
            + tier_block
            + voice_block
            + followup_block
            + f"\n\n{PERSONA_MARKER}\n{persona_block}"
        ).strip()

    @filter.on_llm_response(priority=80)
    async def prevent_fake_local_artifact_reply(
        self, event: AstrMessageEvent, resp: LLMResponse
    ) -> None:
        """Do not let ordinary chat claim that a host-local artifact was delivered."""
        if not self.enabled or getattr(resp, "is_chunk", False):
            return
        original = str(getattr(resp, "completion_text", "") or "")
        request_text = str(
            getattr(event, "get_message_str", lambda: "")() or ""
        )
        cleaned = sanitize_conversational_reply(
            sanitize_unverified_artifact_reply(original, request_text)
        )
        if cleaned != original:
            resp.completion_text = cleaned
            logger.warning("[FriendCore] blocked unverified local artifact reply")

    # ── V3: Commitment tracking ────────────────────────────────────

    @filter.on_llm_request(priority=-3)
    async def extract_commitments(self, event: AstrMessageEvent, req) -> None:
        """Scan last assistant message for promises → store in Firestore."""
        await on_llm_request_extract(event, req)

    @filter.on_llm_request(priority=-6)
    async def inject_commitments(self, event: AstrMessageEvent, req) -> None:
        """Inject pending commitments into system prompt."""
        await on_llm_request_inject(event, req)

    # ── P2+P3: Warmth tracking with Firestore persistence ──────────

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=980)
    async def track_warmth(self, event: AstrMessageEvent):
        """Track relationship warmth and persist to Firestore."""
        if not self.enabled:
            return
        is_direct = event.is_private_chat() or bool(
            getattr(event, "is_at_or_wake_command", False)
        )
        if not is_direct:
            return
        sender = str(getattr(event, "get_sender_id", lambda: "")() or "")
        if not sender or len(sender) < 5:
            return

        # Read warmth (cache-first, fallback to Firestore)
        current = await self._get_warmth(sender)

        # Increment based on interaction type
        if event.is_private_chat():
            current = min(current + 1.0, 100.0)
        elif getattr(event, "is_at_or_wake_command", False):
            current = min(current + 0.8, 100.0)

        # Persist to Firestore (async, non-blocking)
        event.set_extra("_friend_warmth", current)
        asyncio.create_task(self._save_warmth(sender, current))

        # Group follow-up: track who last @mentioned the bot
        if not event.is_private_chat() and getattr(event, "is_at_or_wake_command", False):
            gid = str(getattr(event, "get_group_id", lambda: "")() or "")
            if gid and sender and self.db:
                asyncio.create_task(self._save_active_speaker(gid, sender))

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=979)
    async def capture_explicit_birthday(self, event: AstrMessageEvent):
        """Save only a user's explicit solar-calendar birthday statement."""
        if not self.enabled or not self.db:
            return
        if not (event.is_private_chat() or event.is_at_or_wake_command):
            return
        birthday = parse_explicit_birthday(
            getattr(event, "get_message_str", lambda: "")()
        )
        sender = str(getattr(event, "get_sender_id", lambda: "")() or "")
        if birthday is None or not sender.isdigit():
            return
        name_getter = getattr(event, "get_sender_name", None)
        display_name = str(name_getter() if callable(name_getter) else "")[:30]
        await asyncio.to_thread(
            self._save_birthday, sender, birthday.month, birthday.day, display_name
        )

    def _save_birthday(self, sender: str, month: int, day: int, display_name: str) -> None:
        self.db.collection("users").document(sender).collection("profile").document("birthday").set(
            {
                "month": month,
                "day": day,
                "display_name": display_name,
                "source": "explicit_user_message",
                "updated_at": datetime.now(timezone.utc),
            },
            merge=True,
        )
        logger.info("[FriendCore] birthday saved for QQ %s", sender)

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=991)
    async def birthday_gift_commands(self, event: AstrMessageEvent):
        """Run the private gift state machine before any message reaches an LLM."""
        if not self.enabled or not event.is_private_chat() or not getattr(self, "_gift_store", None):
            return
        text = str(getattr(event, "get_message_str", lambda: "")() or "").strip()
        if not (text.startswith("/生日礼物") or text.startswith("/礼物")):
            return
        sender = str(getattr(event, "get_sender_id", lambda: "")() or "")
        try:
            reply = await self._handle_gift_command(sender, text)
        except Exception as exc:
            logger.warning("[FriendCore] gift command failed: %s", type(exc).__name__)
            reply = "生日礼物流程暂时没有完成，请稍后再试；你的地址不会进入聊天模型或日志。"
        yield event.plain_result(reply)
        event.stop_event()

    async def _handle_gift_command(self, sender: str, text: str) -> str:
        store = self._gift_store
        await asyncio.to_thread(store.purge_expired)
        if text.startswith("/礼物"):
            if sender != self._gift_admin_qq:
                return "该命令仅供礼物管理员在私聊中使用。"
            parts = text.split()
            if len(parts) == 2 and parts[1] == "待审批":
                pending = await asyncio.to_thread(store.pending_candidates)
                if not pending:
                    return "目前没有待审批的生日礼物候选。"
                return "待审批：\n" + "\n".join(
                    f"{item.order_id} | QQ {item.qq_id} | {item.display_name or '未提供称呼'}"
                    for item in pending
                )
            if len(parts) < 3:
                return "管理员格式：/礼物 待审批，/礼物 审批|通知|拒绝|状态|完成 订单号，或 /礼物 发货 订单号 快递公司 单号"
            action, order_id = parts[1], parts[2]
            order = store.get(order_id)
            if order is None:
                return "没有找到该礼物订单。"
            if action == "状态":
                return f"礼物订单 {order.order_id}：{order.status}，用户 QQ {order.qq_id}。"
            if action == "审批":
                order = await asyncio.to_thread(store.transition, order_id, {"candidate"}, "approved")
                sent = await self._send_reminder_message(order.qq_id, consent_notice(self._gift_admin_qq))
                return "已审批并发送隐私同意说明。" if sent else "已审批，但用户通知暂未送达，请稍后重试通知。"
            if action == "通知":
                if order.status != "approved":
                    return "只有已审批且尚未同意的订单可以重发通知。"
                sent = await self._send_reminder_message(order.qq_id, consent_notice(self._gift_admin_qq))
                return "隐私同意说明已重发。" if sent else "通知仍未送达。"
            if action == "拒绝":
                await asyncio.to_thread(store.close, order_id, "rejected")
                await self._send_reminder_message(order.qq_id, "这次生日礼物候选未获批准，因此不会向你收集地址。")
                return "已拒绝；未收集或已删除地址。"
            if action == "完成":
                await asyncio.to_thread(store.close, order_id, "completed")
                await self._send_reminder_message(order.qq_id, "生日礼物订单已完成，感谢你的信任。")
                return "订单已完成，机器人侧不保留地址。"
            if action == "发货" and len(parts) == 5:
                order = await asyncio.to_thread(store.mark_shipped, order_id, parts[3], parts[4])
                await self._send_reminder_message(
                    order.qq_id,
                    f"生日礼物已发货：{order.carrier}，单号 {order.tracking_no}。",
                )
                return "已登记发货并通知用户。"
            return "命令格式不正确，地址不会通过管理员命令查询或重复展示。"

        order = await asyncio.to_thread(store.active_for_user, sender)
        if order is None:
            return "目前没有属于你的生日礼物候选。礼物必须先由管理员审批，小柠不会擅自许诺邮寄。"
        if text == "/生日礼物 状态" or text == "/生日礼物 领取":
            detail = f"；{order.carrier} {order.tracking_no}" if order.status == "shipped" else ""
            return f"礼物订单 {order.order_id} 当前状态：{order.status}{detail}。"
        if text == "/生日礼物 取消":
            if order.status in {"shipped", "completed", "rejected", "cancelled", "expired"}:
                return f"当前状态为 {order.status}，无法再由机器人取消；如已发货请联系管理员。"
            await asyncio.to_thread(store.close, order.order_id, "cancelled")
            return "已撤回领取并删除机器人侧保存的地址密文。"
        if text in {
            "/生日礼物 同意且确认已满14岁",
            "/生日礼物 同意且已获监护人授权",
        }:
            await asyncio.to_thread(store.transition, order.order_id, {"approved"}, "consented")
            return (
                "同意已记录。请仅在当前私聊发送：/生日礼物 地址 收件人|手机号|详细地址\n"
                "不要在群聊发送地址；转交管理员成功后机器人会立即删除地址密文。"
            )
        prefix = "/生日礼物 地址 "
        if text.startswith(prefix):
            order = await asyncio.to_thread(
                store.submit_address, order.order_id, sender, text[len(prefix):]
            )
            address = await asyncio.to_thread(store.relay_address, order.order_id)
            admin_message = (
                f"生日礼物地址（订单 {order.order_id}，用户 QQ {order.qq_id}）：\n"
                f"收件人：{address.recipient}\n手机号：{address.phone}\n地址：{address.address}\n"
                "请仅用于本次寄送，不要转发；机器人将在本消息送达后删除密文。"
            )
            if not await self._send_reminder_message(
                self._gift_admin_qq, admin_message, sensitive=True
            ):
                return "地址已加密保存，但尚未成功转交管理员；最长 7 天后自动删除，请稍后查询状态。"
            await asyncio.to_thread(store.mark_address_relayed, order.order_id)
            return "地址已通过私聊转交管理员，机器人侧地址密文现已删除。等待管理员登记发货。"
        if text == "/生日礼物 重试转交" and order.status == "address_submitted":
            address = await asyncio.to_thread(store.relay_address, order.order_id)
            admin_message = (
                f"生日礼物地址（订单 {order.order_id}，用户 QQ {order.qq_id}）：\n"
                f"收件人：{address.recipient}\n手机号：{address.phone}\n地址：{address.address}\n"
                "请仅用于本次寄送，不要转发；机器人将在本消息送达后删除密文。"
            )
            if not await self._send_reminder_message(
                self._gift_admin_qq, admin_message, sensitive=True
            ):
                return "仍未能安全转交；密文会在 7 天期限到达时自动删除。"
            await asyncio.to_thread(store.mark_address_relayed, order.order_id)
            return "地址已转交管理员并从机器人侧删除。"
        return consent_notice(self._gift_admin_qq) if order.status == "approved" else f"礼物订单当前状态：{order.status}。"

    # ── 高活跃群和常聊用户：更积极地识别求助并展示能力 ──
    _ENGAGED_GROUP_HELP = {"945598390": {"cooldown": 120, "min_confidence": 0.55}}
    _FREQUENT_HELP_USERS = frozenset({
        "3431017350", "943560334", "1410546630", "3174222673",
        "2641419881", "3220305563", "1634854415",
    })

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=-50)
    async def offer_group_help(self, event: AstrMessageEvent):
        """Speak up for clear public help requests, without cold idle messages."""
        if not self.enabled or event.is_private_chat() or event.is_at_or_wake_command:
            return
        text = str(getattr(event, "get_message_str", lambda: "")() or "")
        decision = screen_group_help(text)
        group_id = str(getattr(event, "get_group_id", lambda: "")() or "")
        record = event.get_extra("_context_aware_current_message_record", None)
        if not decision or not group_id.isdigit():
            return
        talking_to = str(getattr(record, "talking_to", "") or "")
        # Context-aware can be absent; the explicit public help signal is the guard.
        if talking_to and talking_to != "group":
            return
        sender = str(getattr(event, "get_sender_id", lambda: "")() or "")
        if self._quiet_mode_for(sender):
            return
        if self._tier_for(sender).value not in decision.capability.tiers:
            return

        # Per-group/per-user tuning: lower barrier for engaged groups and frequent chatters
        engaged = self._ENGAGED_GROUP_HELP.get(group_id, {})
        min_conf = engaged.get("min_confidence", 0.92)
        if sender in self._FREQUENT_HELP_USERS:
            min_conf = min(min_conf, 0.60)
        cooldown = engaged.get("cooldown", GROUP_HELP_COOLDOWN_SECONDS)

        if decision.confidence < min_conf:
            if not self._group_help_llm:
                return
            if decision.confidence < 0.30:
                return
            decision = await self._confirm_group_help(text, decision.capability.id)
            if decision is None:
                return
        now = time.time()
        if now - self._last_group_help_at.get(group_id, 0) < cooldown:
            return
        self._last_group_help_at[group_id] = now
        yield event.plain_result(decision.offer)
        event.stop_event()

    async def _confirm_group_help(self, text: str, capability_id: str):
        """Confirm only ambiguous public-help candidates with structured output."""
        schema = {
            "type": "object",
            "properties": {
                "help_requested": {"type": "boolean"},
                "capability_id": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["help_requested", "capability_id", "confidence"],
            "additionalProperties": False,
        }
        prompt = (
            "判断这条群消息是否在公开向群友求助，并且确实希望有人执行候选能力。"
            "不是求助、只是在闲聊、对特定人说、涉及隐私时 help_requested=false。"
            f"候选能力={capability_id}\n消息={text[:800]}"
        )
        try:
            response = await asyncio.to_thread(
                requests.post,
                "http://127.0.0.1:3000/v1/chat/completions",
                json={
                    "model": "gemini-3.5-flash",
                    "messages": [{"role": "user", "content": prompt}],
                    "response_json_schema": schema,
                    "temperature": 0,
                    "max_tokens": 1000,
                },
                timeout=(2, 8),
            )
            response.raise_for_status()
            raw = response.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            logger.debug("[FriendCore] group help confirmation failed: %s", type(exc).__name__)
            return None
        return parse_group_help_confirmation(raw, capability_id)

    async def _save_active_speaker(self, group_id: str, sender_id: str):
        """Track the last person who @mentioned the bot in a group for follow-up."""
        try:
            doc_ref = self.db.collection("groups").document(group_id)\
                .collection("state").document("active_speaker")
            doc_ref.set({"qq": sender_id, "at": time.time()}, merge=True)
        except Exception:
            pass

    async def _clear_active_speaker(self, group_id: str):
        """Clear active speaker after follow-up response."""
        try:
            self.db.collection("groups").document(group_id)\
                .collection("state").document("active_speaker").delete()
        except Exception:
            pass

    # ── File delivery status ───────────────────────────────────────

    @filter.command("文件状态")
    async def cmd_file_status(self, event: AstrMessageEvent):
        """查询待交付文件状态。"""
        sender = str(getattr(event, "get_sender_id", lambda: "")() or "")
        if not sender.isdigit():
            yield event.plain_result("无法识别用户身份。")
            return
        queue = get_queue()
        count = queue.pending_count(sender)
        if count == 0:
            yield event.plain_result("📦 没有待发送的文件，一切正常～")
        else:
            yield event.plain_result(
                f"📦 有 {count} 个文件在后台重试发送中。\n"
                "系统每 60 秒自动重试，最多重试 10 次。\n"
                "无需手动操作，文件会自动送达。"
            )
        event.stop_event()

    # ── Google ecosystem: scheduled action poller ─────────────────

    async def _poll_scheduled_actions(self):
        """APScheduler job: poll Firestore for due reminders, send them."""
        try:
            store = get_store()
            executed = await asyncio.to_thread(store.poll_and_execute)
            if executed:
                logger.info("[FriendCore] 定时行动已触发 %d 条", executed)
        except Exception:
            pass

    async def _poll_loop(self):
        """Background loop: poll Firestore for scheduled actions + delivery retries every 60s.
        Also runs cleanup of old entries once per hour."""
        tick = 0
        while True:
            try:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                store = get_store()
                queue = get_queue()
                # Poll scheduled reminders (sync Firestore query, runs in thread)
                reminded = await asyncio.to_thread(store.poll_and_execute)
                # Poll delivery retries (async — needs NapCat API)
                delivered = await self._poll_delivery_retries()
                await self._send_due_birthdays()
                if reminded or delivered:
                    logger.info("[FriendCore] poll: %d reminders, %d deliveries",
                                reminded, delivered)
                tick += 1
                if tick >= 60:  # once per hour
                    tick = 0
                    await asyncio.to_thread(store.cleanup_old, 7)
                    await asyncio.to_thread(queue.cleanup)
                    gift_store = getattr(self, "_gift_store", None)
                    if gift_store:
                        await asyncio.to_thread(gift_store.purge_expired)
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    def _due_birthday_profiles(self, today) -> list[tuple[str, object, dict]]:
        due: list[tuple[str, object, dict]] = []
        for user_doc in self.db.collection("users").limit(500).stream():
            qq_id = str(user_doc.id)
            if not qq_id.isdigit():
                continue
            profile = user_doc.reference.collection("profile").document("birthday").get()
            if profile.exists and is_due_birthday(profile.to_dict() or {}, today):
                due.append((qq_id, profile.reference, profile.to_dict() or {}))
        return due

    async def _send_due_birthdays(self) -> None:
        """Send one private birthday greeting and one verified song per user/year."""
        now = datetime.now()
        day_key = now.strftime("%Y%m%d")
        if not self.db or now.hour < BIRTHDAY_SEND_HOUR or self._birthday_scan_day == day_key:
            return
        try:
            due = await asyncio.to_thread(self._due_birthday_profiles, now.date())
        except Exception as exc:
            logger.debug("[FriendCore] birthday scan failed: %s", type(exc).__name__)
            return
        self._birthday_scan_day = day_key
        for qq_id, doc_ref, profile in due:
            if not await self._send_reminder_message(qq_id, birthday_greeting(profile.get("display_name"))):
                continue
            try:
                await asyncio.to_thread(doc_ref.update, {"last_greeted_year": now.year})
                await self._create_birthday_gift_candidate(
                    qq_id, now.year, str(profile.get("display_name") or "")
                )
                await self._send_reminder_message(
                    qq_id,
                    "专属生日歌任务已开始，预计 1–3 分钟；QQ 音频文件成功交付后才会标记完成。",
                )
                song_path = await asyncio.to_thread(generate_birthday_song, self._birthday_song_root)
            except Exception as exc:
                logger.warning("[FriendCore] birthday song generation failed: %s", type(exc).__name__)
                await self._send_reminder_message(qq_id, "这次生日歌没能生成出来，任务未完成；我不会把它说成已经送达。")
                continue

            delivered = await self._napcat_deliver_file(
                local_path=str(song_path), file_name=song_path.name, kind="file",
                sender_id=qq_id, group_id="",
            )
            if delivered:
                await asyncio.to_thread(doc_ref.update, {"last_song_year": now.year})
                await self._send_reminder_message(qq_id, "专属生日歌任务已完成，音频文件已交付。")
                continue

            queued = await asyncio.to_thread(
                get_queue().enqueue,
                local_path=str(song_path), file_name=song_path.name, kind="file",
                sender_id=qq_id, task_desc="专属生日歌（QQ 文件交付中）",
            )
            retry_note = "已加入后台重试队列。" if queued else "文件已安全保留，请稍后重试。"
            await self._send_reminder_message(
                qq_id, f"生日歌已生成，但 QQ 文件尚未交付，任务未完成；{retry_note}"
            )

    async def _create_birthday_gift_candidate(
        self, qq_id: str, year: int, display_name: str
    ) -> None:
        """Create an admin-only candidate; never promise a physical gift yet."""
        store = getattr(self, "_gift_store", None)
        admin_qq = str(getattr(self, "_gift_admin_qq", "") or "")
        if not store or not admin_qq.isdigit():
            return
        try:
            await asyncio.to_thread(store.purge_expired)
            order = await asyncio.to_thread(
                store.create_candidate, qq_id, year, display_name
            )
            if order.status != "candidate":
                return
            await self._send_reminder_message(
                admin_qq,
                f"生日礼物候选：订单 {order.order_id}，用户 QQ {qq_id}，称呼 {display_name or '未提供'}。\n"
                f"请私聊发送 /礼物 审批 {order.order_id} 或 /礼物 拒绝 {order.order_id}。"
                "审批前小柠不会向用户许诺邮寄，也不会收集地址。",
            )
        except Exception as exc:
            logger.warning("[FriendCore] gift candidate failed: %s", type(exc).__name__)

    async def _poll_delivery_retries(self) -> int:
        """Run the shared persistent delivery queue with live NapCat callbacks."""
        queue = get_queue()
        if not queue.db:
            return 0
        try:
            return await queue.poll_and_retry()
        except Exception as e:
            logger.debug("[DeliveryQueue] poll fail: %s", e)
            return 0

    async def _send_reminder_message(
        self, qq_id: str, message: str, *, sensitive: bool = False
    ) -> bool:
        """Send a scheduled reminder to a user via private message."""
        try:
            origin = ""
            for inst in self.context.platform_manager.platform_insts:
                meta = getattr(inst, "metadata", None)
                if meta and hasattr(meta, "id"):
                    origin = str(meta.id)
                    break
            if not origin:
                return False
            from astrbot.api.message_components import Plain
            from astrbot.core.message.message_event_result import MessageChain
            session = f"{origin}:FriendMessage:{qq_id}"
            await self.context.send_message(session, MessageChain([Plain(message)]))
            preview = "[sensitive message omitted]" if sensitive else message[:60]
            logger.info("[FriendCore] 消息已发送 %s: %s", qq_id, preview)
            return True
        except Exception as e:
            logger.debug("[FriendCore] 消息发送失败 %s: %s", qq_id, e)
            return False

    async def _napcat_deliver_file(self, *, local_path: str, file_name: str, kind: str,
                                    sender_id: str, group_id: str) -> bool:
        """Deliver a file to QQ via NapCat APIs. Async — called from poll loop.
        Returns True if any channel succeeded."""
        client = None
        for inst in self.context.platform_manager.platform_insts:
            if hasattr(inst, "get_client"):
                client = inst.get_client()
                break
        if client is None:
            return False

        call_action = getattr(client, "call_action", None)
        if not callable(call_action):
            return False

        upload_path = str(Path(local_path).resolve(strict=True))

        def accepted(result: object) -> bool:
            return not isinstance(result, dict) or result.get("retcode", 0) == 0

        # Strategy: upload_private_file first (file-transfer protocol, bypasses 风控)
        if sender_id.isdigit():
            for attempt in range(3):
                try:
                    result = await call_action(
                        "upload_private_file",
                        user_id=int(sender_id),
                        file=upload_path,
                        name=file_name,
                    )
                    if accepted(result):
                        logger.info("[DeliveryQueue] upload_private_file OK: %s", file_name)
                        return True
                except Exception:
                    pass
                if attempt < 2:
                    await asyncio.sleep(1 * (2 ** attempt))

        # Fallback: upload to group if group context available.
        if group_id.isdigit():
            for attempt in range(3):
                try:
                    result = await call_action(
                        "upload_group_file",
                        group_id=int(group_id),
                        file=upload_path,
                        name=file_name,
                    )
                    if accepted(result):
                        logger.info("[DeliveryQueue] upload_group_file OK: %s", file_name)
                        return True
                except Exception:
                    pass
                if attempt < 2:
                    await asyncio.sleep(1)

        return False

    async def _get_warmth(self, qq_id: str) -> float:
        """Read warmth from cache or Firestore."""
        now = time.time()
        if qq_id in self._warmth_cache:
            score, cached_at = self._warmth_cache[qq_id]
            if now - cached_at < WARMTH_CACHE_TTL:
                return score

        if not self.db:
            return 0.0

        try:
            doc = await asyncio.wait_for(
                asyncio.to_thread(
                    self.db.collection("users").document(qq_id).collection("profile")
                    .document("relationship").get
                ),
                timeout=4.0,
            )
            if doc.exists:
                data = doc.to_dict() or {}
                score = float(data.get("warmth_score", 0))
                self._warmth_cache[qq_id] = (score, now)
                return score
        except Exception:
            pass
        return 0.0

    async def _save_warmth(self, qq_id: str, score: float) -> None:
        """Persist warmth score to Firestore."""
        if not self.db:
            return

        self._warmth_cache[qq_id] = (score, time.time())
        now_utc = datetime.now(timezone.utc)
        try:
            await asyncio.to_thread(
                self.db.collection("users").document(qq_id).collection("profile")
                .document("relationship").set,
                {
                    "warmth_score": score,
                    "last_active": now_utc,
                },
                merge=True,
            )
        except Exception as e:
            logger.debug(f"[FriendCore] 温度持久化失败 {qq_id}: {e}")
