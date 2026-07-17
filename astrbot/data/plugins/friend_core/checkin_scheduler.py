"""Periodic scheduler: scan memories → generate check-ins → send via context."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Awaitable, Callable

from astrbot.api import logger

from .memory_scanner import MemoryScanner

SCAN_INTERVAL = 1800  # 30 minutes
QUIET_HOURS_START = 1   # 凌晨1点
QUIET_HOURS_END = 7     # 早上7点


class CheckinScheduler:
    """Background scheduler that drives memory-triggered check-ins."""

    def __init__(
        self,
        context: Any,
        send_checkin: Callable[[str, str], Awaitable[bool]] | None = None,
    ):
        self._context = context
        self._send_checkin_fn = send_checkin
        self._scanner = MemoryScanner()
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the background scan loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("[FriendCore] 关怀调度器已启动 (30min interval)")

    async def stop(self) -> None:
        """Stop the background scan loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("[FriendCore] 关怀调度器已停止")

    async def _loop(self) -> None:
        """Main loop: scan → filter → send."""
        while self._running:
            try:
                await asyncio.sleep(SCAN_INTERVAL)
                if not self._running:
                    break

                # Skip quiet hours
                from datetime import datetime
                hour = datetime.now().hour
                if QUIET_HOURS_START <= hour < QUIET_HOURS_END:
                    continue

                await self._scan_and_send()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[FriendCore] 调度循环异常: {e}")
                await asyncio.sleep(60)

    async def _scan_and_send(self) -> None:
        """Scan all users, send check-ins for events happening now."""
        tasks = await self._scanner.scan_all_users()
        if not tasks:
            return

        now_hour = datetime.now().hour
        for task in tasks:
            relative = task.get("checkin_time", "")
            # Time-of-day filtering
            if relative == "today_evening" and now_hour < 18:
                continue  # wait until evening
            if relative == "tomorrow_morning" and now_hour > 10:
                continue  # missed morning window, skip
            if relative == "next_day_evening" and now_hour < 18:
                continue

            await self._send_checkin(task)

    async def _send_checkin(self, task: dict[str, Any]) -> None:
        """Send a single check-in message to a user."""
        qq_id = task["qq_id"]
        prompt = task.get("checkin_prompt", "")
        if not prompt:
            return

        try:
            if self._send_checkin_fn is None:
                return
            if await self._send_checkin_fn(qq_id, prompt):
                self._scanner.mark_sent(qq_id, task["memory_key"])
                logger.info(f"[FriendCore] 关怀已发送 → {qq_id}: {prompt[:40]}...")
        except Exception as e:
            logger.warning(f"[FriendCore] 关怀发送失败 {qq_id}: {e}")
