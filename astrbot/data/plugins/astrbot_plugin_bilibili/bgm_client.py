import json
from typing import Any

import aiohttp

BGM_API_BASE_URL = "https://api.bgm.tv"
DEFAULT_TIMEOUT_SECS = 10
DEFAULT_BANGUMI_USER_AGENT = (
    "soulter/astrbot_plugin_bilibili "
    "(https://github.com/Soulter/astrbot_plugin_bilibili)"
)
MAX_ERROR_BODY_LEN = 300
EP_PAGE_SIZE = 200


class BangumiApiClient:
    """bgm.tv API client."""

    def __init__(
        self,
        token: str = "",
        user_agent: str = DEFAULT_BANGUMI_USER_AGENT,
        *,
        base_url: str = BGM_API_BASE_URL,
        timeout_secs: int = DEFAULT_TIMEOUT_SECS,
    ) -> None:
        self._token = token.strip()
        self._user_agent = user_agent.strip()
        if not self._user_agent:
            raise ValueError("bangumi_user_agent 不能为空")
        self._base_url = base_url.rstrip("/")
        self._timeout_secs = timeout_secs

    def _headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": self._user_agent,
            "Accept-Encoding": "identity",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    @staticmethod
    def _ensure_list_payload(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            raise ValueError("bgm.tv /calendar 响应格式错误：顶层不是数组")
        return [item for item in payload if isinstance(item, dict)]

    @staticmethod
    def _decode_json_or_raise(body: str, endpoint: str) -> Any:
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError(f"bgm.tv {endpoint} 响应不是合法 JSON") from exc

    async def request_json(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        timeout = aiohttp.ClientTimeout(total=self._timeout_secs)
        url = f"{self._base_url}{endpoint}"
        async with aiohttp.ClientSession(
            timeout=timeout, headers=self._headers()
        ) as session:
            async with session.request(
                method=method,
                url=url,
                params=params or {},
                json=json_body,
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    snippet = body[:MAX_ERROR_BODY_LEN]
                    raise RuntimeError(
                        f"bgm.tv {method} {endpoint} 请求失败: HTTP {resp.status}, body={snippet}"
                    )
                return self._decode_json_or_raise(body, endpoint)

    async def get_calendar(self) -> list[dict[str, Any]]:
        payload = await self.request_json("/calendar")
        return self._ensure_list_payload(payload)

    async def get_episodes_page(
        self,
        subject_id: int,
        *,
        ep_type: int = 0,
        limit: int = EP_PAGE_SIZE,
        offset: int = 0,
    ) -> dict[str, Any]:
        payload = await self.request_json(
            "/v0/episodes",
            params={
                "subject_id": subject_id,
                "type": ep_type,
                "limit": limit,
                "offset": offset,
            },
        )
        if not isinstance(payload, dict):
            raise ValueError("bgm.tv /v0/episodes 响应格式错误：顶层不是对象")
        return payload

    async def get_subject(self, subject_id: int) -> dict[str, Any]:
        payload = await self.request_json(f"/v0/subjects/{subject_id}")
        if not isinstance(payload, dict):
            raise ValueError("bgm.tv /v0/subjects/{id} 响应格式错误：顶层不是对象")
        return payload

    async def get_subject_relations(self, subject_id: int) -> list[dict[str, Any]]:
        payload = await self.request_json(f"/v0/subjects/{subject_id}/subjects")
        if not isinstance(payload, list):
            raise ValueError(
                "bgm.tv /v0/subjects/{id}/subjects 响应格式错误：顶层不是数组"
            )
        return [item for item in payload if isinstance(item, dict)]

    async def search_subjects(
        self,
        *,
        keyword: str,
        filter_payload: dict[str, Any] | None = None,
        sort: str = "match",
        limit: int = 5,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"keyword": keyword}
        if sort:
            payload["sort"] = sort
        if filter_payload:
            payload["filter"] = filter_payload
        response = await self.request_json(
            "/v0/search/subjects",
            method="POST",
            params={"limit": limit, "offset": offset},
            json_body=payload,
        )
        if not isinstance(response, dict):
            raise ValueError("bgm.tv /v0/search/subjects 响应格式错误：顶层不是对象")
        data = response.get("data")
        if not isinstance(data, list):
            raise ValueError("bgm.tv /v0/search/subjects 响应格式错误：data 不是数组")
        return [item for item in data if isinstance(item, dict)]
