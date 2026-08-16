"""Scan Firestore memories for time-sensitive events → schedule proactive check-ins.

Reads the same Firestore collection as xiaoning_memory (users/{qq}/memories),
identifies memories about plans/events/deadlines, and generates check-in tasks.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import requests

try:
    from google.cloud import firestore
    from google.cloud.firestore import Client as FirestoreClient
except ImportError:
    firestore = None
    FirestoreClient = None

from astrbot.api import logger

PROXY_CHAT = "http://127.0.0.1:3000/v1/chat/completions"
FIRESTORE_PROJECT = "solar-modem-496213-f5"
FIRESTORE_DATABASE = "qqbot"
SCAN_INTERVAL_SECONDS = 1800  # 30 minutes
FIRESTORE_SCAN_TIMEOUT_SECONDS = 12

# Prompt: extract time-sensitive events from memories
EVENT_EXTRACTION_PROMPT = """分析以下用户的记忆列表。找出其中包含时间承诺/计划/事件/截止日的条目。

每条记忆格式: [category] key: value

时间敏感的类型：
- plan: 计划、日程、约定（面试、考试、旅行、聚会、看病）
- deadline: 截止日（提交、汇报、到期）
- event: 已知事件（生日、纪念日、毕业典礼）

对于每个时间敏感的记忆，推断最佳关怀时间：
- "明天"的事 → 当天晚上问候
- "下周"的事 → 前一天晚上问候
- 不确定时间的 → 不生成

返回 JSON 数组（只返回真正需要跟进的事件，不确定的不要）：
[{"memory_key": "key名", "memory_value": "完整value", "checkin_time": "relative", "checkin_prompt": "个性化关怀语，自然口语，不超过50字"}]

checkin_time: "today_evening" / "tomorrow_morning" / "next_day_evening" / skip

如果没有任何值得跟进的事件，返回 []。
只返回 JSON 数组，不要其他文字。"""


class MemoryScanner:
    """Scans Firestore memories, detects time-sensitive events, schedules check-ins."""

    def __init__(self):
        self._db: FirestoreClient | None = None
        self._last_scan: float = 0
        # Track already-sent check-ins to avoid duplicates: {qq_id: {memory_key: sent_time}}
        self._sent_checkins: dict[str, dict[str, float]] = {}

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
            logger.error(f"[FriendCore] Firestore 连接失败: {e}")
            return None
        return self._db

    async def scan_all_users(self) -> list[dict[str, Any]]:
        """Scan all users' memories, return list of check-in tasks to send now."""
        if firestore is None:
            return []

        now = time.time()
        if now - self._last_scan < SCAN_INTERVAL_SECONDS:
            return []
        self._last_scan = now

        tasks: list[dict[str, Any]] = []
        try:
            snapshots = await asyncio.wait_for(
                asyncio.to_thread(self._read_all_memories),
                timeout=FIRESTORE_SCAN_TIMEOUT_SECONDS,
            )
            for qq_id, memories in snapshots:
                user_tasks = await self._extract_events(qq_id, memories)
                tasks.extend(user_tasks)
        except TimeoutError:
            logger.warning("[FriendCore] memory scan timed out")
        except Exception as e:
            logger.warning(f"[FriendCore] 记忆扫描异常: {e}")

        # Clean up old sent records (>7 days)
        cutoff = now - 7 * 86400
        for qq_id in list(self._sent_checkins.keys()):
            self._sent_checkins[qq_id] = {
                k: t for k, t in self._sent_checkins[qq_id].items() if t > cutoff
            }

        return tasks

    def _read_all_memories(self) -> list[tuple[str, list[dict]]]:
        """Read Firestore snapshots outside AstrBot's event loop."""
        if not self.db:
            return []
        snapshots = []
        for user_doc in self.db.collection("users").stream():
            memories = self._read_memories(user_doc.id)
            if memories:
                snapshots.append((user_doc.id, memories))
        return snapshots

    def _read_memories(self, qq_id: str) -> list[dict]:
        """Read all memories for a user from Firestore."""
        if not self.db:
            return []
        try:
            docs = (
                self.db.collection("users")
                .document(qq_id)
                .collection("memories")
                .stream()
            )
            return [{**doc.to_dict(), "doc_id": doc.id} for doc in docs]
        except Exception:
            return []

    async def _extract_events(self, qq_id: str, memories: list[dict]) -> list[dict[str, Any]]:
        """Use Gemini to identify time-sensitive events in memories."""
        if not memories:
            return []

        # Build memory text for LLM
        lines = []
        for m in memories[:30]:  # limit to avoid prompt overflow
            lines.append(
                f"[{m.get('category', '?')}] {m.get('key', '?')}: {m.get('value', '?')}"
            )
        memory_text = "\n".join(lines)

        try:
            resp = await asyncio.to_thread(
                requests.post,
                PROXY_CHAT,
                json={
                    "model": "gemini-3.7-flash",
                    "messages": [
                        {"role": "system", "content": EVENT_EXTRACTION_PROMPT},
                        {"role": "user", "content": memory_text[:3000]},
                    ],
                    "max_tokens": 500,
                },
                timeout=20,
            )
            raw = resp.json()["choices"][0]["message"]["content"].strip()
            raw = raw.lstrip("```json").lstrip("```").rstrip("```").strip()
            events = json.loads(raw)
            if not isinstance(events, list):
                return []
        except Exception as e:
            logger.debug("[FriendCore] 事件提取失败: %s", type(e).__name__)
            return []

        # Filter: dedup + time-relevant + not already sent
        tasks = []
        sent = self._sent_checkins.get(qq_id, {})
        for ev in events:
            key = str(ev.get("memory_key", ""))
            if not key:
                continue
            if key in sent:
                continue  # already sent

            relative = str(ev.get("checkin_time", ""))
            if relative in ("skip", ""):
                continue

            tasks.append({
                "qq_id": qq_id,
                "memory_key": key,
                "memory_value": str(ev.get("memory_value", "")),
                "checkin_prompt": str(ev.get("checkin_prompt", "")),
                "checkin_time": relative,
            })

            # Mark as sent
            if qq_id not in self._sent_checkins:
                self._sent_checkins[qq_id] = {}
            self._sent_checkins[qq_id][key] = time.time()

        return tasks

    def mark_sent(self, qq_id: str, memory_key: str) -> None:
        """Mark a check-in as sent to prevent duplicates."""
        if qq_id not in self._sent_checkins:
            self._sent_checkins[qq_id] = {}
        self._sent_checkins[qq_id][memory_key] = time.time()
