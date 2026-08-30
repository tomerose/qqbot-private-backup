"""Periodically return new QQ profile likes without replaying old history."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from astrbot.api import logger
from astrbot.api.star import Context, Star, StarTools

LIKE_CHECK_SECONDS = 3600
OWNER_QQ = os.getenv("XIAONING_OWNER_ID", "").strip()


class LikeBack(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}
        data_dir = Path(StarTools.get_data_dir("like_back"))
        data_dir.mkdir(parents=True, exist_ok=True)
        self._store = data_dir / "likes.json"
        self._loop: asyncio.Task | None = None
        self._initialized = self._store.is_file()
        self._known_likes = self._load_known()

    def _load_known(self) -> set[str]:
        try:
            data = json.loads(self._store.read_text(encoding="utf-8"))
            return {str(item) for item in data.get("known", [])}
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._initialized = False
            return set()

    def _save_known(self) -> None:
        temporary = self._store.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"known": sorted(self._known_likes)}, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.replace(self._store)

    async def initialize(self):
        self._loop = asyncio.create_task(self._scheduler_loop())
        logger.info("[LikeBack] scheduler started")

    async def terminate(self):
        if self._loop:
            self._loop.cancel()
            try:
                await self._loop
            except asyncio.CancelledError:
                pass

    async def _scheduler_loop(self):
        await asyncio.sleep(60)
        while True:
            try:
                await self._scan_and_like_back()
            except Exception:
                logger.debug("[LikeBack] scan cycle failed")
            await asyncio.sleep(LIKE_CHECK_SECONDS)

    @staticmethod
    def _like_entries(profile: dict) -> list[dict]:
        vote_info = profile.get("voteInfo") or {}
        entries = vote_info.get("userInfos") if isinstance(vote_info, dict) else []
        return [item for item in entries if isinstance(item, dict)] if isinstance(entries, list) else []

    async def _scan_and_like_back(self):
        api = await self._get_api()
        if api is None:
            return
        try:
            profile = await api.call_action("get_profile_like")
        except Exception:
            logger.debug("[LikeBack] get_profile_like failed")
            return
        if not isinstance(profile, dict):
            return

        new_likes: list[dict] = []
        for entry in self._like_entries(profile):
            uid = str(entry.get("uin") or entry.get("uid") or "")
            if not uid.isdigit():
                continue
            count = max(int(entry.get("count") or 1), 1)
            key = f"{uid}:{entry.get('latestTime', 0)}:{count}"
            if key not in self._known_likes:
                new_likes.append(
                    {
                        "key": key,
                        "user_id": uid,
                        "nickname": entry.get("nick") or uid,
                        "times": count,
                    }
                )

        # First successful scan establishes a baseline and never replays old likes.
        if not self._initialized:
            self._known_likes.update(entry["key"] for entry in new_likes)
            self._initialized = True
            self._save_known()
            return
        if not new_likes:
            return

        returned: list[dict] = []
        for entry in new_likes:
            try:
                await api.call_action("send_like", user_id=entry["user_id"], times=1)
                self._known_likes.add(entry["key"])
                returned.append(entry)
            except Exception:
                logger.debug("[LikeBack] send_like failed")
        if not returned:
            return
        self._save_known()

        lines = ["新点赞提醒："]
        for entry in returned[:5]:
            lines.append(
                f"{entry['nickname']} 点了 {entry['times']} 次赞，已自动回赞。"
            )
        try:
            await api.call_action(
                "send_private_msg",
                user_id=str(self.config.get("owner_qq") or OWNER_QQ),
                message="\n".join(lines),
            )
        except Exception:
            logger.debug("[LikeBack] owner notify failed")

    async def _get_api(self):
        for _ in range(6):
            for instance in self.context.platform_manager.platform_insts:
                client = instance.get_client()
                if hasattr(client, "call_action"):
                    return client
            await asyncio.sleep(10)
        return None
