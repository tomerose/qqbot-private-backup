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

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:
    from google.cloud import firestore
    from google.cloud.firestore import Client as FirestoreClient
except ImportError:
    firestore = None
    FirestoreClient = None

from .checkin_scheduler import CheckinScheduler
from .commitment_tracker import on_llm_request_extract, on_llm_request_inject
from .persona_prompt import build_persona_prompt
from .scheduled_actions import get_store, POLL_INTERVAL_SECONDS
from .delivery_queue import get_queue
from .birthday import birthday_greeting, generate_birthday_song, is_due_birthday, parse_explicit_birthday
from .group_help import group_help_offer

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
        self._scheduler = CheckinScheduler(context)
        self._db: FirestoreClient | None = None
        # warmth cache: {qq_id: (warmth_score, cached_at)}
        self._warmth_cache: dict[str, tuple[float, float]] = {}
        self._poll_task: asyncio.Task | None = None
        self._last_group_help_at: dict[str, float] = {}
        self._birthday_scan_day = ""
        self._birthday_song_root = Path(__file__).resolve().parents[4] / "claude_workspace" / "birthday_songs"

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

        # Tier injection: let bot know current user's tier via Firestore
        sender = str(getattr(event, "get_sender_id", lambda: "")() or "")
        tier_block = ""
        if sender.isdigit() and self.db:
            try:
                doc = await asyncio.to_thread(
                    self.db.collection("users").document(sender)
                    .collection("profile").document("membership").get
                )
                if doc.exists:
                    data = doc.to_dict() or {}
                    t = data.get("tier", "ordinary")
                    if t == "pro":
                        tier_block = "\n\n【当前用户资格】Pro（邀请制）。所有功能无限制。"
                    elif t == "x":
                        tier_block = "\n\n【当前用户资格】X资格（QQ好友）。享有记忆、作图、视频、Agent等高级功能。对方是好友，可以更亲切自然。"
                    else:
                        tier_block = "\n\n【当前用户资格】普通用户。缺少记忆和高级功能。如果对方问起，可以自然告诉ta：加小柠QQ好友就能自动获得X资格。"
            except Exception:
                pass

        # Group follow-up: if this sender just @mentioned us, hint the bot to continue
        followup_block = ""
        if not event.is_private_chat() and sender.isdigit() and self.db:
            try:
                gid = str(getattr(event, "get_group_id", lambda: "")() or "")
                doc = await asyncio.to_thread(
                    self.db.collection("groups").document(gid)
                    .collection("state").document("active_speaker").get
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

        warmth = float(event.get_extra("_friend_warmth", 0) or 0)
        persona_block = build_persona_prompt(warmth)
        req.system_prompt = (sp + tier_block + followup_block + f"\n\n{PERSONA_MARKER}\n{persona_block}").strip()

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

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=-50)
    async def offer_group_help(self, event: AstrMessageEvent):
        """Speak up only for a clear, public help request and then cool down."""
        if not self.enabled or event.is_private_chat() or event.is_at_or_wake_command:
            return
        offer = group_help_offer(getattr(event, "get_message_str", lambda: "")())
        group_id = str(getattr(event, "get_group_id", lambda: "")() or "")
        record = event.get_extra("_context_aware_current_message_record", None)
        if not offer or not group_id.isdigit() or getattr(record, "talking_to", "") != "group":
            return
        now = time.time()
        if now - self._last_group_help_at.get(group_id, 0) < GROUP_HELP_COOLDOWN_SECONDS:
            return
        self._last_group_help_at[group_id] = now
        yield event.plain_result(offer)
        event.stop_event()

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

    async def _poll_delivery_retries(self) -> int:
        """Async delivery retry: query Firestore, call NapCat for each pending entry."""
        queue = get_queue()
        if not queue.db:
            return 0
        now = time.time()
        processed = 0
        try:
            users_ref = self.db.collection("users").limit(500).stream()
            for user_doc in users_ref:
                qq_id = user_doc.id
                if not qq_id.isdigit():
                    continue
                deliveries = user_doc.reference.collection("pending_deliveries")\
                    .where("status", "==", "pending")\
                    .where("next_retry_at", "<=", now)\
                    .limit(5).stream()
                for doc in deliveries:
                    data = doc.to_dict()
                    local_path = str(data.get("local_path", ""))
                    file_name = str(data.get("file_name", ""))
                    kind = str(data.get("kind", "file"))
                    sender_id = str(data.get("sender_id", ""))
                    group_id = str(data.get("group_id", ""))
                    retry_count = int(data.get("retry_count", 0))
                    task_desc = str(data.get("task_desc", ""))

                    if not Path(local_path).is_file():
                        doc.reference.update({"status": "failed_permanent", "error": "file_missing"})
                        continue

                    # Try delivery via NapCat
                    success = await self._napcat_deliver_file(
                        local_path=local_path, file_name=file_name, kind=kind,
                        sender_id=sender_id, group_id=group_id,
                    )

                    if success:
                        doc.reference.update({"status": "delivered", "delivered_at": now})
                        asyncio.create_task(
                            self._send_reminder_message(sender_id,
                                f"📦 文件「{file_name}」已成功发送～" +
                                (f"\n任务：{task_desc[:80]}" if task_desc else ""))
                        )
                        logger.info("[DeliveryQueue] DELIVERED %s → QQ %s", file_name, sender_id)
                    else:
                        new_count = retry_count + 1
                        if new_count >= 10:
                            doc.reference.update({
                                "status": "failed_permanent",
                                "retry_count": new_count,
                                "failed_at": now,
                            })
                            asyncio.create_task(
                                self._send_reminder_message(sender_id,
                                    f"⚠ 文件「{file_name}」重试{new_count}次仍未送达。"
                                    "请稍后重试或联系小江。")
                            )
                        else:
                            next_at = now + 60 * (2 ** new_count)
                            doc.reference.update({
                                "retry_count": new_count,
                                "next_retry_at": next_at,
                            })
                    processed += 1
        except Exception as e:
            logger.debug("[DeliveryQueue] poll fail: %s", e)
        return processed

    async def _send_reminder_message(self, qq_id: str, message: str) -> bool:
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
            logger.info("[FriendCore] 消息已发送 %s: %s", qq_id, message[:60])
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
            doc = await asyncio.to_thread(
                self.db.collection("users").document(qq_id).collection("profile")
                .document("relationship").get
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
