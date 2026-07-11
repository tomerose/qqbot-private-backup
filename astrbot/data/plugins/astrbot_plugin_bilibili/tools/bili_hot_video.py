import html
import re
from datetime import datetime
from typing import Any, Optional

from astrbot.api import FunctionTool
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from mcp.types import CallToolResult
from pydantic import Field
from pydantic.dataclasses import dataclass

DEFAULT_LIMIT = 8
MAX_LIMIT = 20
MIN_LIMIT = 1
DEFAULT_PAGE = 1
HOT_SORT = "hot"
VALID_SEARCH_SORT = {"totalrank", "click", "pubdate", "dm", "stow", "scores"}


def _normalize_limit(limit: int) -> int:
    if limit < MIN_LIMIT:
        return MIN_LIMIT
    if limit > MAX_LIMIT:
        return MAX_LIMIT
    return limit


def _normalize_page(page: int) -> int:
    return max(DEFAULT_PAGE, page)


def _normalize_keyword(keyword: str) -> str:
    return keyword.strip()


def _normalize_sort(sort: str, *, has_keyword: bool) -> str:
    value = sort.strip().lower()
    if not value:
        return HOT_SORT if not has_keyword else "totalrank"
    if has_keyword:
        if value == HOT_SORT:
            return "totalrank"
        if value in VALID_SEARCH_SORT:
            return value
        return "totalrank"
    return HOT_SORT


def _clean_title(raw: Any) -> str:
    if not isinstance(raw, str):
        return "未知标题"
    unescaped = html.unescape(raw)
    cleaned = re.sub(r"<[^>]+>", "", unescaped).strip()
    return cleaned or "未知标题"


def _format_count(raw: Any) -> str:
    if isinstance(raw, str):
        text = raw.strip()
        return text or "未知"
    if isinstance(raw, (int, float)):
        value = float(raw)
        if value >= 100000000:
            return f"{value / 100000000:.1f}亿"
        if value >= 10000:
            return f"{value / 10000:.1f}万"
        return str(int(value))
    return "未知"


def _format_pubdate(raw: Any) -> str:
    if isinstance(raw, int) and raw > 0:
        return datetime.fromtimestamp(raw).strftime("%Y-%m-%d")
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return "未知"
        if len(text) >= 10 and text[4] == "-" and text[7] == "-":
            return text[:10]
        return text
    return "未知"


def _extract_hot_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("list", [])
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _extract_search_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("result", [])
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _build_line(index: int, item: dict[str, Any], *, source: str) -> str:
    title = _clean_title(item.get("title"))
    if source == HOT_SORT:
        owner = item.get("owner", {})
        author = owner.get("name") if isinstance(owner, dict) else "未知UP"
        stat = item.get("stat", {})
        bvid = item.get("bvid") or "未知BV"
        play = _format_count(stat.get("view") if isinstance(stat, dict) else None)
        danmaku = _format_count(stat.get("danmaku") if isinstance(stat, dict) else None)
        like = _format_count(stat.get("like") if isinstance(stat, dict) else None)
        pubdate = _format_pubdate(item.get("pubdate"))
    else:
        author = item.get("author") or "未知UP"
        bvid = item.get("bvid") or "未知BV"
        play = _format_count(item.get("play"))
        danmaku = _format_count(item.get("video_review"))
        like = _format_count(item.get("like"))
        pubdate = _format_pubdate(item.get("pubdate"))
    return (
        f"{index}. {title}\n"
        f"BV号: {bvid}\n"
        f"链接: https://www.bilibili.com/video/{bvid}\n"
        f"UP: {author}\n"
        f"播放: {play} | 弹幕: {danmaku} | 点赞: {like} | 发布: {pubdate}"
    )


def _format_result(title: str, items: list[dict[str, Any]], *, source: str) -> str:
    lines = [f"{title}:"]
    for index, item in enumerate(items, start=1):
        lines.append(_build_line(index, item, source=source))
        lines.append("")
    lines.append("请分点回答，不要输出 markdown。")
    lines.append("回答时每条推荐必须保留对应 BV号。")
    return "\n".join(lines)


@dataclass
class BiliSearchHotVideosTool(FunctionTool):
    name: str = "bili_search_hot_videos"
    description: str = (
        "当用户想找哔哩哔哩热门视频、热榜视频、近期高热视频时调用。"
        "支持两种模式：无关键词时返回全站热门；有关键词时按视频搜索并按热度/播放/最新等排序。"
    )
    bili_client: Any = None
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "可选。搜索关键词。留空时返回全站热门视频。",
                },
                "sort": {
                    "type": "string",
                    "description": (
                        "排序方式。无关键词时固定为 hot；有关键词时可用 "
                        "totalrank/click/pubdate/dm/stow/scores。默认 hot。"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "返回数量，范围 1-20，默认 8。",
                    "minimum": MIN_LIMIT,
                    "maximum": MAX_LIMIT,
                },
                "page": {
                    "type": "integer",
                    "description": "页码，默认 1。",
                    "minimum": DEFAULT_PAGE,
                },
                "tid": {
                    "type": "integer",
                    "description": "可选。视频分区 tid，仅在关键词搜索模式下生效。",
                },
            },
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        keyword: str = "",
        sort: str = HOT_SORT,
        limit: int = DEFAULT_LIMIT,
        page: int = DEFAULT_PAGE,
        tid: Optional[int] = None,
    ) -> str | CallToolResult:
        _ = context
        if self.bili_client is None:
            raise RuntimeError("bili_client 未初始化")

        normalized_keyword = _normalize_keyword(keyword)
        normalized_limit = _normalize_limit(limit)
        normalized_page = _normalize_page(page)
        normalized_sort = _normalize_sort(sort, has_keyword=bool(normalized_keyword))

        if normalized_keyword:
            payload = await self.bili_client.search_videos(
                normalized_keyword,
                order=normalized_sort,
                page=normalized_page,
                page_size=normalized_limit,
                video_zone_type=tid,
            )
            if not payload:
                return "搜索热门视频失败，请稍后重试。"
            items = _extract_search_items(payload)
            if not items:
                return "未找到符合条件的视频。可以换个关键词或排序方式。"
            title = f"B站热门视频搜索结果（关键词：{normalized_keyword}，排序：{normalized_sort}）"
            return _format_result(title, items[:normalized_limit], source="search")

        payload = await self.bili_client.get_hot_videos(
            pn=normalized_page, ps=normalized_limit
        )
        if not payload:
            return "获取B站热门视频失败，请稍后重试。"
        items = _extract_hot_items(payload)
        if not items:
            return "当前未获取到热门视频数据。"
        title = f"B站全站热门视频（第 {normalized_page} 页）"
        return _format_result(title, items[:normalized_limit], source=HOT_SORT)
