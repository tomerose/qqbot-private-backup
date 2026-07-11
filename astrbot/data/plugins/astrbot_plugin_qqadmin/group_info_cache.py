from __future__ import annotations

import asyncio
import copy
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
    AiocqhttpAdapter,
)

from .data import QQAdminDB


class QQGroupInfoCache:
    def __init__(
        self,
        context: Context,
        db: QQAdminDB,
        ttl_seconds: int = 90,
    ):
        self.context = context
        self.db = db
        self.ttl_seconds = ttl_seconds

        self._lock = asyncio.Lock()
        self._last_refresh_at = 0.0
        self._group_list_cache: list[dict[str, Any]] = []
        self._group_detail_cache: dict[str, dict[str, Any]] = {}
        self._group_clients: dict[str, Any] = {}

    async def list_groups(self, force: bool = False) -> list[dict[str, Any]]:
        if force or not self._is_fresh() or not self._group_list_cache:
            await self._refresh_group_list(force=force)
        return copy.deepcopy(self._group_list_cache)

    async def get_group(self, group_id: str, force: bool = False) -> dict[str, Any]:
        normalized_group_id = str(group_id).strip()
        if not normalized_group_id:
            raise ValueError("group_id must not be empty")

        if (
            force
            or not self._is_fresh()
            or self._find_group_from_cache(normalized_group_id) is None
        ):
            await self._refresh_group_list(force=force)

        cached_detail = self._group_detail_cache.get(normalized_group_id)
        if cached_detail and not force and self._is_fresh():
            return copy.deepcopy(cached_detail)

        detail = await self._load_group_detail(normalized_group_id)
        self._group_detail_cache[normalized_group_id] = detail
        return copy.deepcopy(detail)

    def invalidate(self, group_id: str | None = None) -> None:
        if group_id:
            self._group_detail_cache.pop(str(group_id).strip(), None)
            return
        self._group_detail_cache.clear()

    def remove_group(self, group_id: str | None) -> None:
        normalized_group_id = str(group_id or "").strip()
        if not normalized_group_id:
            return

        self._group_list_cache = [
            item
            for item in self._group_list_cache
            if item.get("group_id") != normalized_group_id
        ]
        self._group_detail_cache.pop(normalized_group_id, None)
        self._group_clients.pop(normalized_group_id, None)

    def _is_fresh(self) -> bool:
        return (time.time() - self._last_refresh_at) < self.ttl_seconds

    async def _refresh_group_list(self, force: bool = False) -> None:
        async with self._lock:
            if not force and self._is_fresh() and self._group_list_cache:
                return

            merged_groups: dict[str, dict[str, Any]] = {}
            group_clients: dict[str, Any] = {}
            missing_detail_group_ids: set[str] = set()

            for client in self._iter_clients():
                try:
                    result = await client.call_action("get_group_list")
                    for item in self._extract_list(result):
                        group_id = str(item.get("group_id", "")).strip()
                        if not group_id or group_id in merged_groups:
                            continue
                        merged_groups[group_id] = self._normalize_group_summary(item)
                        group_clients[group_id] = client
                        if self._needs_detail_refresh(item, group_id):
                            missing_detail_group_ids.add(group_id)
                except Exception as exc:
                    logger.warning("Failed to load QQ group list: %s", exc)

            for group_id in self.db.list_group_ids():
                if group_id not in merged_groups:
                    merged_groups[group_id] = self._build_fallback_group(group_id)
                    missing_detail_group_ids.add(group_id)

            if missing_detail_group_ids:
                await self._hydrate_missing_groups(
                    merged_groups, group_clients, missing_detail_group_ids
                )

            self._group_list_cache = self._sort_groups(list(merged_groups.values()))
            self._group_clients = group_clients
            self._group_detail_cache.clear()
            self._last_refresh_at = time.time()

    async def _load_group_detail(self, group_id: str) -> dict[str, Any]:
        group_detail = self._find_group_from_cache(
            group_id
        ) or self._build_fallback_group(group_id)
        detail, client = await self._fetch_group_detail(
            group_id, preferred_client=self._group_clients.get(group_id)
        )
        if detail:
            group_detail.update(detail)
            if client is not None:
                self._group_clients[group_id] = client

        return group_detail

    async def _hydrate_missing_groups(
        self,
        merged_groups: dict[str, dict[str, Any]],
        group_clients: dict[str, Any],
        group_ids: set[str],
    ) -> None:
        for group_id in sorted(group_ids):
            detail, client = await self._fetch_group_detail(
                group_id,
                preferred_client=group_clients.get(group_id),
            )
            if not detail:
                continue
            merged_groups[group_id].update(detail)
            if client is not None:
                group_clients[group_id] = client

    async def _fetch_group_detail(
        self,
        group_id: str,
        preferred_client: Any | None = None,
    ) -> tuple[dict[str, Any] | None, Any | None]:
        tried_client_ids: set[int] = set()
        clients: list[Any] = []

        if preferred_client is not None:
            clients.append(preferred_client)
            tried_client_ids.add(id(preferred_client))

        for client in self._iter_clients():
            client_id = id(client)
            if client_id in tried_client_ids:
                continue
            tried_client_ids.add(client_id)
            clients.append(client)

        for client in clients:
            try:
                result = await client.call_action(
                    "get_group_info", group_id=int(group_id)
                )
                info = self._extract_object(result)
                if info:
                    detail = self._normalize_group_summary(info)
                    detail["source"] = "live"
                    return detail, client
            except Exception as exc:
                logger.debug(
                    "Failed to fetch QQ group detail for %s: %s", group_id, exc
                )

        return None, None

    def _iter_clients(self) -> list[Any]:
        clients: list[Any] = []
        for inst in self.context.platform_manager.platform_insts:
            if not isinstance(inst, AiocqhttpAdapter):
                continue
            try:
                client = inst.get_client()
            except Exception:
                continue
            if client is not None:
                clients.append(client)
        return clients

    def _find_group_from_cache(self, group_id: str) -> dict[str, Any] | None:
        for item in self._group_list_cache:
            if item.get("group_id") == group_id:
                return copy.deepcopy(item)
        return None

    @staticmethod
    def _extract_list(result: Any) -> list[dict[str, Any]]:
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
        return []

    @staticmethod
    def _extract_object(result: Any) -> dict[str, Any]:
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, dict):
                return data
            return result
        return {}

    @classmethod
    def _normalize_group_summary(cls, raw_group: dict[str, Any]) -> dict[str, Any]:
        group_id = str(raw_group.get("group_id", "")).strip()
        return {
            "group_id": group_id,
            "group_name": str(raw_group.get("group_name", "")).strip()
            or f"群 {group_id}",
            "avatar": cls._build_avatar(group_id),
            "member_count": cls._safe_int(raw_group.get("member_count"), 0),
            "max_member_count": cls._safe_int(raw_group.get("max_member_count"), 0),
            "source": "live",
        }

    @staticmethod
    def _needs_detail_refresh(raw_group: dict[str, Any], group_id: str) -> bool:
        return not str(raw_group.get("group_name", "")).strip() or not group_id

    @classmethod
    def _build_fallback_group(cls, group_id: str) -> dict[str, Any]:
        return {
            "group_id": group_id,
            "group_name": f"群 {group_id}",
            "avatar": cls._build_avatar(group_id),
            "member_count": 0,
            "max_member_count": 0,
            "source": "cached",
        }

    @staticmethod
    def _build_avatar(group_id: str) -> str:
        return f"https://p.qlogo.cn/gh/{group_id}/{group_id}/640"

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _sort_groups(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            groups,
            key=lambda item: (
                not str(item.get("group_id", "")).isdigit(),
                int(item["group_id"]) if str(item.get("group_id", "")).isdigit() else 0,
                item.get("group_name", ""),
            ),
        )
