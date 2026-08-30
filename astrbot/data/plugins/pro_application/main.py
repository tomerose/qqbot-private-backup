"""Pro 开通与管理系统 — 全部由小柠自动处理。

管理命令需要 passphrase 二次认证。Passphrase 仅通过环境变量
XIAONING_PRO_PASSPHRASE 设置，不出现在任何配置文件或群聊消息中。
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import zoneinfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
try:
    from xiaoning_runtime import defer_stop_event
except ImportError:
    from data.plugins.xiaoning_runtime import defer_stop_event
from astrbot.core.message.message_event_result import MessageChain

from .pro_store import Application, ProStore, ProStoreError


REVIEWER_ID = os.getenv("PUBLIC_REVIEWER_ID", "").strip()
APPLICATION_EMAIL = os.getenv("PUBLIC_CONTACT_EMAIL", "").strip()
SUMMARY_GROUP_ID = os.getenv("PUBLIC_SUMMARY_GROUP_ID", "").strip()
SUMMARY_HOUR = 22
SUMMARY_MINUTE = 0
REVIEW_DAY = "sun"
REVIEW_HOUR = 20
REVIEW_MINUTE = 0
DIEDEEP_PATH = Path(os.getenv("XIAONING_DIEDEEP_PATH", "__disabled__"))

# Invite system
INVITE_EXPIRE_HOURS = 72
INVITE_CODE_PREFIX = "XIAONING-"

# Friend-X sync: check friend list every 10 min, auto-grant/revoke X资格
FRIEND_X_SYNC_INTERVAL_MINUTES = 10
FRIEND_X_REVOKE_GRACE_CHECKS = 2  # Must be non-friend for N consecutive checks

# Passphrase auth session
AUTH_SESSION_TTL = 300         # 5 min
MAX_AUTH_ATTEMPTS = 3
AUTH_LOCKOUT_SECONDS = 900     # 15 min


class ProApplication(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        data_root = Path(__file__).resolve().parents[2] / "plugin_data" / "xiaoning_pro"
        self.context = context
        self.store = ProStore(data_root / "pro_members.db", reviewer_id=REVIEWER_ID)
        self._clock = time.time
        # Passphrase auth session state (in-memory only — lost on restart)
        self._auth_sessions: dict[str, float] = {}       # reviewer_id -> expires_at
        self._auth_failures: dict[str, tuple[int, float]] = {}  # reviewer_id -> (count, lockout_until)
        self._invite_lock = asyncio.Lock()
        # Daily summary + weekly review scheduler
        self._summary_scheduler: AsyncIOScheduler | None = None
        self._summary_lock = asyncio.Lock()
        self._review_lock = asyncio.Lock()
        # Friend-X sync: track consecutive non-friend checks for grace period
        self._friend_x_non_friend_count: dict[str, int] = {}
        self._friend_x_lock = asyncio.Lock()

    # ── Daily summary ──────────────────────────────────────────────

    async def _start_daily_summary(self) -> None:
        """Start the daily summary + weekly review scheduler."""
        if self._summary_scheduler is not None:
            return
        tz = zoneinfo.ZoneInfo("Asia/Shanghai")
        self._summary_scheduler = AsyncIOScheduler(timezone=tz)
        self._summary_scheduler.add_job(
            self._daily_summary_job,
            "cron",
            hour=SUMMARY_HOUR,
            minute=SUMMARY_MINUTE,
            id="pro_daily_summary",
            replace_existing=True,
        )
        self._summary_scheduler.add_job(
            self._weekly_review_job,
            "cron",
            day_of_week=REVIEW_DAY,
            hour=REVIEW_HOUR,
            minute=REVIEW_MINUTE,
            id="pro_weekly_review",
            replace_existing=True,
        )
        self._summary_scheduler.add_job(
            self._friend_x_sync_job,
            "interval",
            minutes=FRIEND_X_SYNC_INTERVAL_MINUTES,
            id="friend_x_sync",
            replace_existing=True,
            next_run_time=None,  # manual first run in on_platform_loaded
        )
        self._summary_scheduler.start()
        logger.info(
            f"[Pro] 每日总结({SUMMARY_HOUR:02d}:{SUMMARY_MINUTE:02d}) + "
            f"周复盘({REVIEW_DAY} {REVIEW_HOUR:02d}:{REVIEW_MINUTE:02d}) + "
            f"好友X同步(每{FRIEND_X_SYNC_INTERVAL_MINUTES}分钟) 已启动"
        )

    async def _daily_summary_job(self) -> None:
        """Scheduler entry point — delegates to the main summary logic."""
        try:
            await self._run_summary(SUMMARY_GROUP_ID)
        except Exception:
            logger.exception("[Pro] 每日总结任务异常")

    async def _weekly_review_job(self) -> None:
        """Scheduler entry point — weekly review."""
        try:
            await self._run_weekly_review()
        except Exception:
            logger.exception("[Pro] 周复盘任务异常")

    async def _run_summary(self, group_id: str) -> str:
        """Fetch group messages, summarize via LLM, send to pro users."""
        async with self._summary_lock:
            # 1. Get aiocqhttp client
            client = None
            for inst in self.context.platform_manager.platform_insts:
                if hasattr(inst, "get_client"):
                    client = inst.get_client()
                    break
            if client is None:
                raise RuntimeError("未找到 QQ 平台连接")

            # 2. Fetch group messages
            now = self._clock()
            try:
                result = await client.call_action(
                    "get_group_msg_history",
                    group_id=int(group_id),
                    message_seq=0,
                    count=200,
                )
                messages = result.get("messages", [])
            except Exception:
                logger.exception("[Pro] 获取群消息失败")
                return "获取群消息失败"

            if not messages:
                return "今日群内无消息"

            # 3. Build chat transcript
            lines: list[str] = []
            for msg in reversed(messages[-200:]):
                sender = msg.get("sender", {})
                name = sender.get("card") or sender.get("nickname") or str(sender.get("user_id", "?"))
                text = str(msg.get("message", "") or msg.get("raw_message", ""))[:200]
                if text.strip():
                    lines.append(f"[{name}]: {text}")
            transcript = "\n".join(lines)

            # 4. Summarize via LLM
            provider = self.context.get_using_provider()
            if provider is None:
                return "LLM 不可用"

            today = datetime.now().strftime("%Y年%m月%d日")
            llm_response = await provider.text_chat(
                system_prompt="你是小柠的群聊总结助手。用中文精简总结群聊内容，突出主要话题和精彩发言。控制在200字以内。",
                prompt=f"以下是群 {group_id} 在 {today} 的聊天记录：\n{transcript}\n\n请总结今日群聊的主要内容：",
            )
            summary = (llm_response.completion_text or "").strip()
            if not summary:
                return "LLM 总结为空"

            # 5. Send to pro users
            pro_qqs = self.store.list_active_pro_qqs(now=now)
            if not pro_qqs:
                return "无 Pro 用户可发送"

            sent_count = 0
            origin = ""
            for inst in self.context.platform_manager.platform_insts:
                meta = getattr(inst, "metadata", None)
                if meta and hasattr(meta, "id"):
                    origin = str(meta.id)
                    break

            full_msg = f"【小柠每日群总结 · {today}】\n群：{group_id}\n\n{summary}"
            for qq_id in pro_qqs:
                try:
                    session = f"{origin}:FriendMessage:{qq_id}" if origin else f"aiocqhttp:FriendMessage:{qq_id}"
                    await self.context.send_message(session, MessageChain([Plain(full_msg)]))
                    sent_count += 1
                except Exception:
                    logger.debug("[Pro] 发送总结失败")

            logger.info(f"[Pro] 每日总结已发送给 {sent_count}/{len(pro_qqs)} 位 Pro 用户")
            return f"总结已发送给 {sent_count} 位 Pro 用户"

    # ── Weekly review ──────────────────────────────────────────────

    def _week_daily_files(self) -> list[Path]:
        """Return daily/*.md files from Monday–Sunday of the current week."""
        today = datetime.now()
        monday = today - timedelta(days=today.weekday())
        files: list[Path] = []
        daily_dir = DIEDEEP_PATH / "daily"
        if not daily_dir.is_dir():
            return files
        for i in range(7):
            day = monday + timedelta(days=i)
            candidate = daily_dir / f"{day.strftime('%Y-%m-%d')}.md"
            if candidate.is_file():
                files.append(candidate)
        return files

    async def _run_weekly_review(self) -> str:
        """周日复盘：读 fitness/log.md + 本周 daily → LLM 填充复盘 → 发送 owner → 保存调整方向。"""
        async with self._review_lock:
            now = self._clock()
            today = datetime.now()
            week_start = today - timedelta(days=today.weekday())
            week_end = week_start + timedelta(days=6)

            # 1. Read fitness log
            fitness_path = DIEDEEP_PATH / "fitness" / "log.md"
            fitness_text = ""
            if fitness_path.is_file():
                fitness_text = fitness_path.read_text(encoding="utf-8")

            # 2. Read daily files
            daily_files = self._week_daily_files()
            daily_texts: list[str] = []
            for f in daily_files:
                try:
                    daily_texts.append(f"--- {f.stem} ---\n{f.read_text(encoding='utf-8')}")
                except Exception:
                    pass
            daily_all = "\n\n".join(daily_texts) if daily_texts else "（本周暂无日志）"

            # 3. LLM analysis
            provider = self.context.get_using_provider()
            if provider is None:
                return "LLM 不可用"

            system_prompt = (
                "你是小柠的周复盘助手。根据用户提供的健身日志和每日记录，生成简洁的周复盘。"
                "只输出复盘内容，不要客套话。按以下格式输出：\n"
                "## 本周数据\n- 体重变化/跳绳总量/A日数/情绪均分（从数据中提取）\n"
                "## 亮点\n- 做得好的地方\n"
                "## 踩坑\n- 遇到什么问题\n"
                "## 下周调整\n- 具体可执行的调整建议（每条一行，将以 // 开头作为指令发送给计划生成器）\n"
                "## 情绪趋势\n- 情绪变化描述"
            )
            week_range = f"{week_start.strftime('%m/%d')} → {week_end.strftime('%m/%d')}"
            llm_response = await provider.text_chat(
                system_prompt=system_prompt,
                prompt=(
                    f"请对 {week_range} 这一周进行复盘：\n\n"
                    f"## 健身数据\n{fitness_text[-3000:]}\n\n"
                    f"## 每日日志\n{daily_all[-5000:]}"
                ),
            )
            review = (llm_response.completion_text or "").strip()
            if not review:
                return "LLM 复盘为空"

            # 4. Extract adjustment direction (lines starting with "## 下周调整")
            direction_lines: list[str] = []
            in_direction = False
            for line in review.splitlines():
                if line.startswith("## 下周调整"):
                    in_direction = True
                    continue
                if in_direction and line.startswith("##"):
                    break
                if in_direction and line.strip().startswith("-"):
                    direction_lines.append(f"// {line.strip().lstrip('- ')}")

            # 5. Save direction for Monday Codex
            direction_path = DIEDEEP_PATH / "plan" / "next-week-direction.md"
            direction_path.parent.mkdir(parents=True, exist_ok=True)
            direction_path.write_text(
                f"# 下周调整方向 ({week_range})\n\n"
                + ("\n".join(direction_lines) if direction_lines else "// 按计划继续"),
                encoding="utf-8",
            )

            # 6. Send review to owner
            origin = ""
            for inst in self.context.platform_manager.platform_insts:
                meta = getattr(inst, "metadata", None)
                if meta and hasattr(meta, "id"):
                    origin = str(meta.id)
                    break
            session = f"{origin}:FriendMessage:{REVIEWER_ID}" if origin else f"aiocqhttp:FriendMessage:{REVIEWER_ID}"
            full_msg = (
                f"【小柠周复盘 · {week_range}】\n\n{review}\n\n"
                f"📁 调整方向已保存 → plan/next-week-direction.md"
            )
            await self.context.send_message(session, MessageChain([Plain(full_msg)]))
            logger.info(f"[Pro] 周复盘已发送给 owner")
            return f"周复盘完成（{week_range}）"

    async def _quick_friend_grant(self, event, sender_id: str) -> None:
        """Per-message quick check: if sender is QQ friend but has no tier, grant X."""
        if not await self._is_qq_friend(event, sender_id):
            return
        try:
            state, _ = self.store.claim_friend_x(sender_id, now=self._clock())
            if state == "granted":
                logger.info("[Pro] 即时X授予")
                asyncio.create_task(self._notify_x_granted(sender_id))
        except Exception:
            pass

    async def _notify_x_granted(self, qq_id: str) -> None:
        """Send a private message to notify user they got X资格."""
        try:
            origin = ""
            for inst in self.context.platform_manager.platform_insts:
                meta = getattr(inst, "metadata", None)
                if meta and hasattr(meta, "id"):
                    origin = str(meta.id)
                    break
            if not origin:
                return
            session = f"{origin}:FriendMessage:{qq_id}"
            msg = (
                "🎉 小柠检测到你加了好友，已自动为你开通 X资格！\n\n"
                "X资格解锁：\n"
                "• 长期记忆 — 小柠会记住你分享的事，聊天时自然提起\n"
                "• 跨对话任务追踪 — 帮你盯进行中的任务\n"
                "• 深度思考 + 私聊增强对话\n"
                "• AI作图 1次/天 | AI视频 1次/天 | 视频制作 1次/天\n"
                "• AI辩论/面试/文档分析 | 网页工坊 | 搜索行动包\n"
                "• Agent任务 1次/周 | 文件交付\n"
                "• AI早报每日推送\n\n"
                "发送 /pro status 查看资格详情。\n"
                "就像跟朋友聊天一样，有什么需要直接说就行～"
            )
            await self.context.send_message(session, MessageChain([Plain(msg)]))
            logger.info("[Pro] X资格通知已发送")
        except Exception:
            pass

    # ── Friend-X sync ──────────────────────────────────────────────

    async def _friend_x_sync_job(self) -> None:
        """Scheduler entry point — delegate to the main sync logic."""
        try:
            await self._sync_friend_x()
        except Exception:
            logger.exception("[Pro] 好友X同步异常")

    async def _sync_friend_x(self) -> None:
        """Sync X资格: grant to new friends, revoke from removed friends (grace period)."""
        async with self._friend_x_lock:
            now = self._clock()
            # 1. Get friend list from QQ
            friend_ids: set[str] = set()
            for inst in self.context.platform_manager.platform_insts:
                client = None
                if hasattr(inst, "get_client"):
                    client = inst.get_client()
                if client is None:
                    continue
                try:
                    result = await client.call_action("get_friend_list")
                except Exception:
                    logger.debug("[Pro] get_friend_list 失败，跳过本轮同步")
                    return  # fail-safe: skip this round, retry next interval
                if isinstance(result, dict):
                    result = result.get("data", result.get("friends", []))
                if isinstance(result, list):
                    for item in result:
                        if isinstance(item, dict):
                            uid = str(item.get("user_id", ""))
                            if uid.isdigit() and len(uid) >= 5:
                                friend_ids.add(uid)
                break  # only use first platform instance

            if not friend_ids:
                logger.debug("[Pro] 好友列表为空，跳过X同步")
                return

            # 2. Grant X to new friends
            granted = 0
            granted_ids: list[str] = []
            for uid in friend_ids:
                if uid == REVIEWER_ID:
                    continue  # owner already has PRO
                try:
                    state, _ = self.store.claim_friend_x(uid, now=now)
                    if state == "granted":
                        granted += 1
                        granted_ids.append(uid)
                        self._friend_x_non_friend_count.pop(uid, None)
                    elif state == "already_member":
                        self._friend_x_non_friend_count.pop(uid, None)
                except Exception:
                    pass

            # 3. Revoke X from non-friends (grace period: N consecutive checks)
            revoked = 0
            active_x = self._list_active_x_qqs(now=now)
            for uid in active_x:
                if uid in friend_ids:
                    self._friend_x_non_friend_count.pop(uid, None)
                    continue
                count = self._friend_x_non_friend_count.get(uid, 0) + 1
                self._friend_x_non_friend_count[uid] = count
                if count >= FRIEND_X_REVOKE_GRACE_CHECKS:
                    try:
                        if self.store.revoke_friend_x(uid, now=now):
                            revoked += 1
                            self._friend_x_non_friend_count.pop(uid, None)
                    except Exception:
                        pass

            # 4. Notify newly granted users via private message
            for uid in granted_ids:
                asyncio.create_task(self._notify_x_granted(uid))

            if granted or revoked:
                logger.info(
                    "[Pro] 好友X同步: +%d X资格, -%d X资格, %d好友",
                    granted, revoked, len(friend_ids),
                )

    def _list_active_x_qqs(self, *, now: float) -> list[str]:
        """Return all QQ IDs with active X tier (friend-granted only)."""
        import sqlite3 as _sqlite3
        from contextlib import closing as _closing
        try:
            with _closing(_sqlite3.connect(str(self.store.path.resolve(strict=True)))) as conn:
                conn.row_factory = _sqlite3.Row
                rows = conn.execute(
                    """SELECT qq_id FROM applications
                       WHERE tier = 'x' AND state = 'active' AND pro_expires_at >= ?
                         AND application_id LIKE 'FRIEND-X-%'""",
                    (float(now),),
                ).fetchall()
                return [str(r["qq_id"]) for r in rows]
        except Exception:
            return []

    # ── Passphrase auth ────────────────────────────────────────────

    def _check_auth(self, sender_id: str, now: float) -> None:
        """Raise ProStoreError if sender is not the reviewer or hasn't authenticated.

        Non-reviewers get a generic denial. The reviewer must authenticate
        via /pro auth before management commands work.
        """
        if sender_id != REVIEWER_ID:
            raise ProStoreError("reviewer_required")
        # Check lockout
        failure = self._auth_failures.get(sender_id)
        if failure:
            _count, lockout_until = failure
            if now < lockout_until:
                remaining = int(lockout_until - now)
                raise ProStoreError(f"小柠安全锁已激活，{remaining} 秒后再试。")
        # Check session expiry
        expires = self._auth_sessions.get(sender_id, 0.0)
        if now >= expires:
            raise ProStoreError("请先用 /pro auth 验证身份。")

    def _authenticate(self, reviewer_id: str, passphrase: str, now: float) -> None:
        """Verify passphrase and create a short-lived management session."""
        # Check lockout first
        failure = self._auth_failures.get(reviewer_id)
        if failure:
            _count, lockout_until = failure
            if now < lockout_until:
                remaining = int(lockout_until - now)
                raise ProStoreError(f"小柠安全锁已激活，{remaining} 秒后再试。")

        expected = os.environ.get("XIAONING_PRO_PASSPHRASE", "")
        if not expected:
            raise ProStoreError("小柠 Pro 管理系统未配置。")
        if not self.store.verify_passphrase(passphrase):
            count = (failure[0] if failure else 0) + 1
            if count >= MAX_AUTH_ATTEMPTS:
                lockout_until = now + AUTH_LOCKOUT_SECONDS
                self._auth_failures[reviewer_id] = (count, lockout_until)
                raise ProStoreError(f"验证失败次数过多，小柠安全锁已激活 {AUTH_LOCKOUT_SECONDS // 60} 分钟。")
            self._auth_failures[reviewer_id] = (count, now)
            raise ProStoreError("身份验证未通过。")

        # Success — clear failures, create session
        self._auth_failures.pop(reviewer_id, None)
        self._auth_sessions[reviewer_id] = now + AUTH_SESSION_TTL

    @staticmethod
    def _text(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_message_str", None)
        if callable(getter):
            return str(getter() or "").strip()
        return str(getattr(event, "message_str", "") or "").strip()

    @staticmethod
    def _sender(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_sender_id", None)
        return str(getter() if callable(getter) else "").strip()

    # ── Invite system ────────────────────────────────────────────────

    def _invite_path(self) -> Path:
        configured = getattr(self, "_invite_file", None)
        p = (
            Path(configured)
            if configured is not None
            else Path(__file__).resolve().parents[2]
            / "plugin_data"
            / "xiaoning_pro"
            / "invites.json"
        )
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def _load_invites(self) -> dict:
        p = self._invite_path()
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("codes", {}), dict):
                    return data
            except (json.JSONDecodeError, OSError):
                pass
        return {"codes": {}}

    def _save_invites(self, data: dict) -> None:
        target = self._invite_path()
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(target)

    @contextmanager
    def _invite_file_guard(self):
        """Serialize invite read-modify-write cycles across bot and web UI."""
        lock_path = self._invite_path().with_suffix(".lock")
        with open(lock_path, "a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _sign_invite(
        self,
        code: str,
        target_qq: str,
        tier: str,
        days: int,
        expires_at: float,
        used: bool,
    ) -> str:
        """HMAC-SHA256 sign the invite payload with ProStore signing key."""
        import hashlib, hmac
        key = getattr(self.store, '_signing_key', None)
        if key is None:
            return ""
        payload = (
            f"{code}|{target_qq}|{tier}|{days}|{expires_at:.6f}|{int(bool(used))}"
        ).encode("utf-8")
        return hmac.new(key, payload, hashlib.sha256).hexdigest()

    def _verify_invite_sig(self, code: str, entry: dict) -> bool:
        """Verify the stored HMAC signature matches."""
        import hashlib, hmac
        key = getattr(self.store, '_signing_key', None)
        if key is None:
            return False
        expected = entry.get("_sig", "")
        if not expected:
            return False  # unsigned entry → reject
        payload = (
            f"{code}|{entry['target_qq']}|{entry['tier']}|{entry['days']}|"
            f"{entry['expires_at']:.6f}|{int(bool(entry.get('used')))}"
        ).encode("utf-8")
        actual = hmac.new(key, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(actual, expected)

    def _generate_invite(self, target_qq: str, tier: str, days: int, now: float) -> str:
        with self._invite_file_guard():
            store = self._load_invites()
            codes = store.setdefault("codes", {})
            code = f"{INVITE_CODE_PREFIX}{secrets.token_hex(4).upper()}"
            while code in codes:
                code = f"{INVITE_CODE_PREFIX}{secrets.token_hex(4).upper()}"
            expires_at = now + INVITE_EXPIRE_HOURS * 3600
            entry = {
                "target_qq": target_qq,
                "tier": tier,
                "days": days,
                "created_at": now,
                "expires_at": expires_at,
                "used": False,
            }
            entry["_sig"] = self._sign_invite(
                code, target_qq, tier, days, expires_at, False
            )
            if not entry["_sig"]:
                raise ProStoreError("signing_key_unavailable")
            codes[code] = entry
            self._save_invites(store)
        logger.info("[Pro] Invite created for tier=%s days=%s", tier, days)
        return code

    def _redeem_invite(self, code: str, sender_id: str, now: float) -> tuple[bool, str]:
        with self._invite_file_guard():
            store = self._load_invites()
            entry = store.get("codes", {}).get(code.upper())
            if entry is None:
                return False, "邀请码无效，请检查是否输入正确。"
            if not self._verify_invite_sig(code.upper(), entry):
                logger.warning("[Pro] Invite signature mismatch — possible tampering")
                return False, "邀请码无效，请检查是否输入正确。"
            if entry["used"]:
                return False, "该邀请码已被使用。"
            if now > entry["expires_at"]:
                return False, "邀请码已过期，请联系管理员获取新码。"
            if entry["target_qq"] != sender_id:
                return False, "该邀请码绑定的 QQ 号与你不同，请确认。"
            entry["used"] = True
            entry["used_at"] = now
            entry["_sig"] = self._sign_invite(
                code.upper(), entry["target_qq"], entry["tier"],
                entry["days"], entry["expires_at"], True,
            )
            self._save_invites(store)
            try:
                self.store.grant(
                    sender_id, REVIEWER_ID, entry["days"], now=now, tier=entry["tier"]
                )
            except ProStoreError as error:
                entry["used"] = False
                entry.pop("used_at", None)
                entry["_sig"] = self._sign_invite(
                    code.upper(), entry["target_qq"], entry["tier"],
                    entry["days"], entry["expires_at"], False,
                )
                self._save_invites(store)
                logger.warning("[Pro] Invite grant rejected")
                return False, "开通失败，请稍后重试。"
        label = "X" if entry["tier"] == "x" else "Pro"
        return True, (
            f"小柠已为你开通 {label}（{entry['days']} 天）！\n"
            f"发送 /pro status 查看资格详情。"
        )

    @staticmethod
    async def _is_qq_friend(event: AstrMessageEvent, sender_id: str) -> bool:
        bot = getattr(event, "bot", None)
        call_action = getattr(bot, "call_action", None) if bot is not None else None
        if not callable(call_action):
            return False
        try:
            result = await call_action("get_friend_list")
        except Exception as exc:
            logger.warning("[Pro] friend check failed: %s", type(exc).__name__)
            return False
        if isinstance(result, dict):
            result = result.get("data", result.get("friends", []))
        return isinstance(result, list) and any(
            str(item.get("user_id", "")) == sender_id
            for item in result
            if isinstance(item, dict)
        )

    def _list_invites(self) -> str:
        store = self._load_invites()
        codes = store.get("codes", {})
        if not codes:
            return "暂无邀请码。"
        lines = []
        for code, entry in sorted(codes.items(), key=lambda x: x[1].get("created_at", 0), reverse=True):
            status = "✓已用" if entry["used"] else ("⏳待兑" if self._clock() < entry["expires_at"] else "✗过期")
            lines.append(
                f"{code} → QQ {entry['target_qq']} {entry['tier'].upper()} {entry['days']}天 {status}"
            )
        return "\n".join(lines[:15])

    @staticmethod
    def _format_expire(expires_at: float, now: float) -> str:
        remaining = max(0, int(expires_at - now))
        days = remaining // 86400
        hours = (remaining % 86400) // 3600
        if days > 0:
            return f"{days} 天"
        return f"{hours} 小时"

    @staticmethod
    def _tokens(text: str) -> list[str] | None:
        value = str(text or "").strip()
        normalized = "".join(value.lower().split())
        if normalized in {"申请pro", "申请pro资格", "开通pro", "申请x", "开通x", "申请go", "开通go"}:
            return ["status"]
        parts = value.split()
        if not parts:
            return None
        first = parts[0].lower()
        if first in {"/pro", "/invite", "/redeem"}:
            return [part.strip() for part in parts[1:] if part.strip()]
        return None

    @staticmethod
    def _invite_help() -> str:
        return (
            "【X/Pro 说明】\n"
            "添加小柠为 QQ 好友即自动获得 X资格（无需口令，系统自动检测）。\n"
            "Pro 资格通过邀请码开通。\n\n"
            "拥有者：/invite <QQ号> <x|pro> [天数]\n"
            "受邀人：私聊发送 /redeem <邀请码>\n\n"
            "查看资格：/pro status"
        )

    @staticmethod
    def _status_reply(application: Application | None, now: float | None = None) -> str:
        if application is None:
            return "你目前暂无 X/Pro 有效资格。"
        states = {
            "pending_email": "待发送邮件",
            "awaiting_review": "小柠审核中",
            "approval_pending_confirm": "小柠安全确认中",
            "awaiting_verify": "等待 QQ 验证",
            "active": "资格已开通",
            "denied": "申请未通过",
            "revoked": "Pro 已撤销",
            "expired": "申请已过期",
            "pro_expired": "Pro 已到期",
            "verification_expired": "验证码已过期",
            "verification_locked": "验证码已锁定",
        }
        if application.state == "active" and application.pro_expires_at is not None:
            label = "X" if application.tier == "x" else "Pro"
            checked_at = time.time() if now is None else float(now)
            return (
                f"当前资格：{label}（有效）。\n"
                f"剩余时间：{ProApplication._format_expire(application.pro_expires_at, checked_at)}。"
            )
        return f"当前状态：{states.get(application.state, '未知')}。"

    @staticmethod
    def _error_reply(error: ProStoreError) -> str:
        msg = str(error)
        # Dynamic messages (may contain parameters like lockout seconds)
        if msg.startswith("小柠安全锁"):
            return msg
        mapping = {
            "application_pending": "你已有未完成的 Pro 申请，请先完成或等待它过期。",
            "application_owner": "该申请不属于当前 QQ。",
            "application_expired": "申请已过期，请重新申请。",
            "application_state": "当前申请状态不支持此操作。",
            "reviewer_required": "此操作仅小柠系统可执行。",
            "duration_invalid": "有效期需在 1 到 365 天之间。",
            "resend_rate_limited": "刚补发过验证码，请 1 分钟后再试。",
            "verification_invalid": "验证码无效或已失效。",
            "verification_locked": "验证码已锁定，请重新申请。",
            "qq_id_invalid": "QQ 号无效。",
            "请先用 /pro auth 验证身份。": "请先用 /pro auth 验证身份。",
            "小柠 Pro 管理系统未配置。": "小柠 Pro 管理系统当前不可用。",
            "身份验证未通过。": "身份验证未通过。",
        }
        if msg in mapping:
            return mapping[msg]
        if msg.startswith("auth_"):
            return "身份验证未通过。"
        return "操作未完成，请稍后重试。"

    async def _send_private_code(
        self, event: AstrMessageEvent, qq_id: str, code: str
    ) -> bool:
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        if ":" not in origin:
            return False
        platform = origin.split(":", 1)[0]
        session = f"{platform}:FriendMessage:{qq_id}"
        message = MessageChain(
            [Plain(f"你的 Pro 验证码：{code}\n请在 10 分钟内回复：/pro verify {code}")]
        )
        try:
            return bool(await self.context.send_message(session, message))
        except Exception:
            return False

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=970)
    @defer_stop_event
    async def on_message(self, event: AstrMessageEvent):
        raw_text = str(self._text(event) or "")
        sender_id = self._sender(event)
        now = float(self._clock())

        # Lightweight friend-X check on @mention or private chat (5 min cooldown)
        if sender_id.isdigit() and sender_id != REVIEWER_ID:
            is_private = event.is_private_chat()
            is_at = getattr(event, "is_at_or_wake_command", False)
            if is_private or is_at:
                last = getattr(self, "_last_quick_friend_check", {})
                if now - last.get(sender_id, 0) > 300:
                    last[sender_id] = now
                    self._last_quick_friend_check = last
                    try:
                        app = self.store.status_for(sender_id, now=now)
                        has_active = app is not None and app.state == "active" and app.pro_expires_at and app.pro_expires_at >= now
                    except Exception:
                        has_active = False
                    if not has_active:
                        asyncio.create_task(self._quick_friend_grant(event, sender_id))

        # ── /redeem <code> — 兑换邀请码（私聊）──
        if raw_text.lower().startswith("/redeem"):
            event.stop_event()
            if not event.is_private_chat():
                yield event.plain_result("请在私聊中兑换邀请码。")
                return
            parts = raw_text.split(maxsplit=1)
            if len(parts) < 2:
                yield event.plain_result("用法：/redeem <邀请码>")
                return
            code = parts[1].strip().upper()
            async with self._invite_lock:
                _ok, msg = self._redeem_invite(code, sender_id, now)
            yield event.plain_result(msg)
            return

        # ── /invite <QQ> <x|pro> [days] — 生成邀请码（拥有者）──
        if raw_text.lower().startswith("/invite"):
            event.stop_event()
            if sender_id != REVIEWER_ID:
                yield event.plain_result("此操作仅小柠拥有者可用。")
                return
            if not event.is_private_chat():
                yield event.plain_result("请在私聊中生成邀请码。")
                return
            try:
                self._check_auth(sender_id, now)
            except ProStoreError as error:
                yield event.plain_result(self._error_reply(error))
                return
            parts = raw_text.split()
            if len(parts) < 3:
                yield event.plain_result("用法：/invite <QQ号> <x|pro> [天数]\n示例：/invite 123456 pro 30")
                return
            target_qq = parts[1].strip()
            if not target_qq.isdigit() or len(target_qq) < 5:
                yield event.plain_result("QQ 号格式不正确。")
                return
            tier = parts[2].strip().lower()
            if tier not in {"x", "pro"}:
                yield event.plain_result("tier 必须为 x 或 pro。X资格请引导对方添加小柠为QQ好友自动获得。")
                return
            days = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else 30
            max_days = 90 if tier == "x" else 365
            days = max(1, min(days, max_days))
            async with self._invite_lock:
                code = self._generate_invite(target_qq, tier, days, now)
            label = "X" if tier == "x" else "Pro"
            yield event.plain_result(
                f"邀请码已生成：{code}\n"
                f"目标：QQ {target_qq}\n"
                f"资格：{label} {days} 天\n"
                f"有效期：{INVITE_EXPIRE_HOURS} 小时\n\n"
                f"将该邀请码发给对方，对方私聊小柠发送 /redeem {code} 即可开通。"
            )
            return

        tokens = self._tokens(raw_text)
        if tokens is None:
            return
        event.stop_event()
        action = tokens[0].lower() if tokens else "status"
        try:
            # ── /pro status ──
            if action == "status":
                yield event.plain_result(
                    self._status_reply(self.store.status_for(sender_id, now=now), now)
                )
                return

            # ── /pro auth <passphrase> — 身份验证（拥有者管理命令前置）──
            if action == "auth" and len(tokens) == 2:
                if sender_id != REVIEWER_ID:
                    yield event.plain_result("此操作仅小柠拥有者可用。")
                    return
                if not event.is_private_chat():
                    yield event.plain_result("请在私聊中进行身份验证。")
                    return
                self._authenticate(sender_id, tokens[1], now)
                yield event.plain_result(f"身份验证通过，{AUTH_SESSION_TTL // 60} 分钟内可执行管理操作。")
                return

            # ── 以下命令需要身份验证 ──
            management_actions = {
                "revoke", "grant", "invites", "audit", "summary", "review", "group"
            }
            if action in management_actions and not event.is_private_chat():
                yield event.plain_result("请在私聊中执行 Pro 管理操作。")
                return
            if action == "revoke" and len(tokens) == 2:
                self._check_auth(sender_id, now)
                yield event.plain_result("小柠已撤销。" if self.store.revoke(tokens[1], sender_id, now=now) else "该 QQ 当前没有有效资格。")
                return

            if action == "grant" and len(tokens) in {2, 3}:
                self._check_auth(sender_id, now)
                days = int(tokens[2]) if len(tokens) == 3 and tokens[2].isdigit() else 30
                max_days = 90 if raw_text.lower().startswith("/x") else 365
                days = max(1, min(days, max_days))
                tier = "x" if raw_text.lower().startswith("/x") else "pro"
                self.store.grant(tokens[1], sender_id, days, now=now, tier=tier)
                label = "X" if tier == "x" else "Pro"
                yield event.plain_result(f"小柠已授予 {tokens[1]} {label}（{days} 天）。")
                return

            if action == "invites":
                self._check_auth(sender_id, now)
                yield event.plain_result(self._list_invites())
                return

            if action == "audit" and len(tokens) == 2:
                self._check_auth(sender_id, now)
                events = self.store.audit_for(tokens[1], sender_id, now=now)
                details = "\n".join(
                    f"{item.event_type} | {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(item.event_at))}"
                    for item in events
                )
                yield event.plain_result(f"操作记录：\n{details or '暂无记录'}")
                return

            if action == "summary":
                self._check_auth(sender_id, now)
                gid = tokens[1] if len(tokens) >= 2 else SUMMARY_GROUP_ID
                yield event.plain_result("小柠正在生成每日总结…")
                result = await self._run_summary(gid)
                yield event.plain_result(result)
                return

            if action == "review":
                self._check_auth(sender_id, now)
                yield event.plain_result("小柠正在进行周复盘…")
                result = await self._run_weekly_review()
                yield event.plain_result(result)
                return

            if action == "group" and len(tokens) >= 2:
                self._check_auth(sender_id, now)
                sub = tokens[1].lower()
                if sub == "add" and len(tokens) == 3:
                    gid = tokens[2]
                    if not gid.isdigit():
                        yield event.plain_result("群号无效。")
                        return
                    self.store.activate_group(gid, sender_id, now=now)
                    yield event.plain_result(f"小柠已激活群 {gid} 的 Pro 功能。")
                    return
                if sub == "remove" and len(tokens) == 3:
                    ok = self.store.deactivate_group(tokens[2], sender_id, now=now)
                    yield event.plain_result("小柠已移除 Pro 群。" if ok else "该群不是 Pro 群。")
                    return
                if sub == "list":
                    groups = self.store.list_active_groups(sender_id, now=now)
                    yield event.plain_result("小柠 Pro 群：" + ("、".join(groups) if groups else "无"))
                    return

            yield event.plain_result(self._invite_help())
        except ProStoreError as error:
            yield event.plain_result(self._error_reply(error))
        except ValueError:
            yield event.plain_result("参数格式不正确。")

    # ── Lifecycle ──────────────────────────────────────────────────

    @filter.on_platform_loaded()
    async def on_platform_loaded(self) -> None:
        """平台连接后启动每日总结调度器 + 首次好友X同步。"""
        await self._start_daily_summary()
        # Initial friend-X sync (runs after a short delay for platform to fully init)
        asyncio.create_task(self._delayed_initial_friend_sync())

    async def _delayed_initial_friend_sync(self) -> None:
        """Wait for platform to stabilise, then run initial friend-X sync."""
        await asyncio.sleep(30)  # 30s delay for platform to be fully ready
        try:
            await self._sync_friend_x()
        except Exception:
            logger.exception("[Pro] 初始好友X同步失败")

    async def terminate(self) -> None:
        """插件终止时关闭调度器。"""
        if self._summary_scheduler is not None:
            try:
                self._summary_scheduler.shutdown(wait=False)
            except Exception:
                pass
            self._summary_scheduler = None
        logger.info("[Pro] 插件已关闭")
