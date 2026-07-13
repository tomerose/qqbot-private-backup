"""Daily group brief — Pro groups get a natural-language recap at 22:00."""

from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from pathlib import Path

import requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star

try:
    from draw_command.pro_access import is_active_pro_group
except ImportError:
    from data.plugins.draw_command.pro_access import is_active_pro_group

PROXY = "http://127.0.0.1:3000/v1/chat/completions"
MIN_MESSAGES = 10
BRIEF_HOUR = 22
BRIEF_MINUTE = 0
_SAMPLE_CAP = 50
_SAMPLE_KEEP = 30

_KEYWORD = re.compile(r"[一-鿿]{2,}")  # 2+ char Chinese words


class GroupBrief(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}
        project_root = Path(__file__).resolve().parents[4]
        self._pro_db = (
            project_root / "astrbot" / "data" / "plugin_data"
            / "xiaoning_pro" / "pro_members.db"
        )
        self._daily: dict[str, dict] = {}
        self.scheduler = AsyncIOScheduler()

    async def initialize(self):
        self.scheduler.add_job(
            self._send_all,
            "cron",
            hour=self.config.get("brief_hour", BRIEF_HOUR),
            minute=self.config.get("brief_minute", BRIEF_MINUTE),
        )
        self.scheduler.start()

    async def terminate(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=10000)
    async def on_message(self, event: AstrMessageEvent):
        group_id = str(getattr(event, "get_group_id", lambda: "")() or "")
        if not group_id:
            return
        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
        text = self._msg(event)[:80].strip()
        if not text:
            return

        day = self._daily.setdefault(
            group_id,
            {"count": 0, "users": set(), "samples": [], "last_ts": 0},
        )
        day["count"] += 1
        day["users"].add(sender_id[-4:])
        day["last_ts"] = time.time()
        if text:
            day["samples"].append(text)
            if len(day["samples"]) > _SAMPLE_CAP:
                day["samples"] = day["samples"][-_SAMPLE_KEEP:]

    async def _send_all(self):
        for gid, stats in list(self._daily.items()):
            if stats["count"] < MIN_MESSAGES:
                continue
            if not is_active_pro_group(gid, self._pro_db):
                continue
            await self._send_brief(gid, stats)
        self._daily.clear()

    async def _send_brief(self, group_id: str, stats: dict):
        # Extract keywords
        all_text = " ".join(stats["samples"])
        words = _KEYWORD.findall(all_text)
        top_words = [w for w, _ in Counter(words).most_common(8) if len(w) >= 2]

        prompt = (
            f"今天群里共 {stats['count']} 条消息，{len(stats['users'])} 人参与聊天。"
        )
        if top_words:
            prompt += f"高频话题词：{'、'.join(top_words)}。"
        prompt += (
            "请用100-200字写一段群聊日报，像真人朋友的语气，自然口语，不要AI腔。"
            "可以调侃活跃的人，提一下大家聊了什么有趣的事，语气轻松有温度。"
            f"开头用「📰 今日群报」作为标题。"
        )

        try:
            resp = await asyncio.to_thread(
                requests.post,
                PROXY,
                json={
                    "model": "gemini-2.5-flash",
                    "messages": [
                        {"role": "system", "content": "你是群里的一员，用口语写日报。"},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 500,
                },
                timeout=45,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()
        except Exception:
            return

        try:
            platform = "aiocqhttp"
            session = f"{platform}:GroupMessage:{group_id}"
            await self.context.send_message(session, [Plain(text)])
        except Exception:
            pass

    @staticmethod
    def _msg(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_message_str", None)
        if callable(getter):
            return str(getter() or "")
        return str(getattr(event, "message_str", "") or "")
