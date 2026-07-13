"""Google-grounded search, Maps lookup, and sandboxed calculations."""

from __future__ import annotations

import asyncio
import re

import requests
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

SEARCH_PROXY_URL = "http://127.0.0.1:3000/v1/chat/completions"
SEARCH_MODEL = "gemini-2.5-flash-search"
SEARCH_TIMEOUT = (15, 60)

_SEARCH_COMMAND = re.compile(r"^\s*/search\s+(.+?)\s*$", re.I)
_CALC_COMMAND = re.compile(r"^\s*/calc\s+(.+?)\s*$", re.I)
_NATURAL_SEARCH = re.compile(
    r"^\s*(?:小柠[，,：:\s]*)?"
    r"(?:搜索|查一下|查查看|帮我搜|帮我查|查一查)\s*(?P<query>.+?)\s*$",
    re.I,
)
_NATURAL_MAPS = re.compile(
    r"^\s*(?:小柠[，,：:\s]*)?(?:(?:帮我|给我)[，,：:\s]*)?"
    r"(?:(?:附近|周边|旁边).+|.+(?:在哪里|在哪儿|怎么去)|"
    r".{0,20}(?:餐厅|咖啡|奶茶|火锅|银行|医院|加油站|停车场|酒店|商场|超市|地铁站|公交|地图|导航))\s*$",
    re.I,
)
_NATURAL_CURRENT = re.compile(
    r"^\s*(?:小柠[，,：:\s]*)?"
    r"(?:今天|现在|当前|最近|最新).*(?:天气|新闻|发生|热点|股价|汇率|比赛)\s*$",
    re.I,
)
_VIDEO_SEARCH = re.compile(
    r"(?:(?:找|搜|搜索|查找)(?:一下)?[^\n]{0,12}(?:视频|短片|小视频|影片)|"
    r"(?:视频|短片|小视频|影片)[^\n]{0,12}(?:找|搜|搜索|查找))",
    re.I,
)

SEARCH_MEMORY = (
    "【实时搜索能力】用户明确说 /search、搜索、查一下，或询问附近地点、实时天气和新闻时，"
    "会调用 Google 搜索或 Google Maps；/calc 会在 Google 托管的隔离环境中计算。"
    "这些能力对所有版本开放。视频搜索由独立视频功能处理，不能改成普通网页搜索。"
)


def is_video_search_intent(text: str) -> bool:
    """Keep video requests on the media-delivery path."""
    value = str(text or "").strip()
    if value.lower().startswith(("/findvideo", "/findvid", "/搜视频", "/找视频")):
        return True
    return bool(_VIDEO_SEARCH.search(value))


def _detect_search_mode(query: str) -> dict[str, bool]:
    location_keywords = (
        "附近", "周边", "旁边", "在哪里", "在哪儿", "怎么去", "多远", "地址",
        "餐厅", "咖啡", "奶茶", "火锅", "银行", "医院", "加油站", "停车场",
        "酒店", "商场", "超市", "地铁站", "公交", "地图", "导航",
    )
    maps = any(keyword in query for keyword in location_keywords)
    return {"google_search": not maps, "google_maps": maps, "code_execution": False}


async def _call_proxy(query: str, flags: dict[str, bool]) -> tuple[str, list[dict]]:
    payload = {
        "model": SEARCH_MODEL if flags["google_search"] else "gemini-2.5-flash",
        "google_search": flags["google_search"],
        "google_maps": flags["google_maps"],
        "code_execution": flags["code_execution"],
        "max_tokens": 2048,
        "messages": [{"role": "user", "content": query}],
    }
    for attempt in range(2):
        response = await asyncio.to_thread(
            requests.post,
            SEARCH_PROXY_URL,
            json=payload,
            timeout=SEARCH_TIMEOUT,
        )
        if response.status_code < 500 or attempt:
            break
        await asyncio.sleep(0.5)
    response.raise_for_status()
    body = response.json()
    content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
    sources = (body.get("grounding") or {}).get("sources") or []
    return str(content or ""), sources if isinstance(sources, list) else []


class SearchCommand(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=978)
    async def on_message(self, event: AstrMessageEvent):
        text = str(getattr(event, "get_message_str", lambda: "")() or "").strip()
        if not (event.is_private_chat() or event.is_at_or_wake_command):
            return

        # The video plugin must be allowed to download and return the media file.
        if is_video_search_intent(text):
            return

        query: str | None = None
        calc_mode = False
        match = _SEARCH_COMMAND.match(text)
        if match:
            query = match.group(1).strip()
        else:
            match = _CALC_COMMAND.match(text)
            if match:
                calc_mode = True
                query = match.group(1).strip()
            else:
                match = _NATURAL_SEARCH.match(text)
                if match:
                    query = match.group("query").strip()

        if query is None and (_NATURAL_MAPS.match(text) or _NATURAL_CURRENT.match(text)):
            query = re.sub(r"^小柠[，,：:\s]*", "", text).strip()
        if query is None:
            return

        flags = _detect_search_mode(query)
        if calc_mode:
            flags = {"google_search": False, "google_maps": False, "code_execution": True}
            query = f"请用 Python 计算并核对这个问题，只返回必要的计算过程和结果：{query}"

        if calc_mode:
            yield event.plain_result("正在计算…")
        elif flags["google_maps"]:
            yield event.plain_result("正在查询地点…")
        else:
            yield event.plain_result("正在搜索…")

        try:
            content, sources = await _call_proxy(query, flags)
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            logger.warning("[SearchCmd] search failed: %s", type(exc).__name__)
            yield event.plain_result("搜索暂时不可用，请稍后再试。")
            event.stop_event()
            return

        result = content.strip() or "搜索没有返回有效结果。"
        links = " | ".join(
            f"{source.get('title') or '来源'}：{source.get('uri')}"
            for source in sources[:3]
            if isinstance(source, dict) and source.get("uri")
        )
        if links:
            result = f"{result}\n\n来源：{links}"
        if len(result) > 1800:
            result = result[:1799] + "…"
        yield event.plain_result(result)
        event.stop_event()

    @filter.on_llm_request(priority=-18)
    async def inject_search_memory(self, event: AstrMessageEvent, req) -> None:
        system_prompt = str(getattr(req, "system_prompt", "") or "")
        if "【实时搜索能力】" not in system_prompt:
            req.system_prompt = f"{system_prompt}\n\n{SEARCH_MEMORY}".strip()
