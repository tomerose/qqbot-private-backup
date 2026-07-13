"""Time capsule — send a message to your future self via QQ DM."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools
try:
    from xiaoning_runtime import defer_stop_event
except ImportError:
    from data.plugins.xiaoning_runtime import defer_stop_event

MAX_CAPSULES_PER_USER = 5
DURATION_RE = re.compile(r"(\d+)\s*(天|周|个?月|年)(后|之后)?")
DATA_FILE = "capsules.json"
MSG_CREATED = (
    "⏳ 胶囊已封存。\n"
    "送达时间：{deliver_str}\n"
    "当前活跃胶囊：{count}/{max_count}\n"
    "届时我会私聊提醒你。"
)


def _parse_duration(text: str) -> timedelta | None:
    m = DURATION_RE.search(str(text or ""))
    if not m:
        return None
    num = int(m.group(1))
    unit = m.group(2)
    if "年" in unit:
        return timedelta(days=num * 365)
    if "月" in unit:
        return timedelta(days=num * 30)
    if "周" in unit:
        return timedelta(weeks=num)
    return timedelta(days=num)


def _after_duration(text: str) -> str:
    """Return the message portion after the duration clause."""
    m = DURATION_RE.search(str(text or ""))
    if not m:
        return text
    start = m.end()
    rest = text[start:].strip()
    return re.sub(r"^提醒我[：:]?\s*", "", rest)


class TimeCapsule(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}
        self.scheduler = AsyncIOScheduler()
        self.data_dir = StarTools.get_data_dir("time_capsule")
        os.makedirs(self.data_dir, exist_ok=True)
        self.data_file = os.path.join(self.data_dir, DATA_FILE)
        self._capsules: list[dict] = []

    async def initialize(self):
        self._capsules = self._load()
        now = time.time()
        pending: list[dict] = []
        for cap in list(self._capsules):
            if cap["deliver_at"] > now:
                self._schedule(cap)
                pending.append(cap)
            else:
                # Deliver immediately if overdue (bot was down)
                if not await self._deliver(cap):
                    cap["deliver_at"] = time.time() + 300
                    self._schedule(cap)
                    pending.append(cap)
        self._capsules = pending
        self._save()
        self.scheduler.start()

    async def terminate(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def _load(self) -> list[dict]:
        try:
            with open(self.data_file, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _save(self) -> None:
        temporary = f"{self.data_file}.tmp"
        with open(temporary, "w", encoding="utf-8") as fh:
            json.dump(self._capsules, fh, ensure_ascii=False, indent=2)
        os.replace(temporary, self.data_file)

    def _schedule(self, cap: dict) -> None:
        deliver_ts = cap["deliver_at"]
        dt = datetime.fromtimestamp(deliver_ts)
        self.scheduler.add_job(
            self._fire,
            "date",
            run_date=dt,
            args=[cap.copy()],
            id=cap["id"],
            replace_existing=True,
        )

    async def _fire(self, cap: dict) -> None:
        if await self._deliver(cap):
            self._capsules = [c for c in self._capsules if c["id"] != cap["id"]]
        else:
            retry_at = time.time() + 300
            for current in self._capsules:
                if current["id"] == cap["id"]:
                    current["deliver_at"] = retry_at
                    cap = current.copy()
                    break
            self._schedule(cap)
        self._save()

    async def _deliver(self, cap: dict) -> bool:
        try:
            platform = cap.get("platform", "aiocqhttp")
            session = f"{platform}:FriendMessage:{cap['sender_id']}"
            msg = (
                f"💌 来自 {cap['from_str']} 的你：\n\n"
                f"{cap['message']}\n\n"
                f"—— 时间胶囊 #{cap['id']}"
            )
            return bool(await self.context.send_message(session, [Plain(msg)]))
        except Exception:
            return False

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=980)
    @defer_stop_event
    async def on_message(self, event: AstrMessageEvent):
        text = self._msg(event)
        if not text.startswith("/capsule") and not text.startswith("/胶囊"):
            return
        event.stop_event()

        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
        if not sender_id:
            return

        # Parse: /capsule 6个月后 提醒我：记得坚持学英语
        parts = re.split(r"\s+", text, maxsplit=2)
        if len(parts) < 2:
            active = [c for c in self._capsules if c["sender_id"] == sender_id]
            yield event.plain_result(
                f"用法：/capsule 6个月后 提醒我：你想对未来自己说的话\n"
                f"你当前有 {len(active)} 个活跃胶囊。"
            )
            return

        duration = _parse_duration(parts[1] if len(parts) > 1 else "")
        if duration is None:
            yield event.plain_result(
                "时间格式有误。例如：/capsule 6个月后 提醒我：记得学英语"
            )
            return

        message = _after_duration(parts[2]) if len(parts) > 2 else parts[1]
        if not message or len(message) < 2:
            yield event.plain_result("胶囊内容太短，请写至少 2 个字。")
            return

        active = [c for c in self._capsules if c["sender_id"] == sender_id]
        if len(active) >= MAX_CAPSULES_PER_USER:
            yield event.plain_result(
                f"你已有 {len(active)} 个活跃胶囊（上限 {MAX_CAPSULES_PER_USER}）。"
                f"请等待部分胶囊送达后再创建新的。"
            )
            return

        deliver_at = time.time() + duration.total_seconds()
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        platform = origin.split(":", 1)[0] if ":" in origin else "aiocqhttp"
        dt = datetime.fromtimestamp(deliver_at)

        cap = {
            "id": f"cap-{uuid.uuid4().hex[:12]}",
            "sender_id": sender_id,
            "platform": platform,
            "message": message,
            "deliver_at": deliver_at,
            "from_str": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "created_at": time.time(),
        }
        self._capsules.append(cap)
        self._save()
        self._schedule(cap)

        yield event.plain_result(
            MSG_CREATED.format(
                deliver_str=dt.strftime("%Y 年 %m 月 %d 日 %H:%M"),
                count=len(active) + 1,
                max_count=MAX_CAPSULES_PER_USER,
            )
        )

    @staticmethod
    def _msg(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_message_str", None)
        if callable(getter):
            return str(getter() or "")
        return str(getattr(event, "message_str", "") or "")
