from datetime import date
from typing import Any, Optional

from astrbot.api import FunctionTool
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from mcp.types import CallToolResult
from pydantic import Field
from pydantic.dataclasses import dataclass

from ..bgm_client import DEFAULT_BANGUMI_USER_AGENT, BangumiApiClient
from .bgm_subject_schema import (
    build_advanced_parameters_schema,
    build_recent_hot_parameters_schema,
)

DEFAULT_LIMIT = 5
MAX_LIMIT = 10
VALID_SORT = {"match", "heat", "rank", "score"}
VALID_SUBJECT_TYPE = {1, 2, 3, 4, 6}
DEFAULT_RECENT_MONTHS = 3
MIN_RECENT_MONTHS = 1

SUBJECT_TYPE_ALIAS = {
    "book": 1,
    "anime": 2,
    "music": 3,
    "game": 4,
    "real": 6,
    "书籍": 1,
    "动画": 2,
    "音乐": 3,
    "游戏": 4,
    "三次元": 6,
}


def _normalize_limit(limit: int) -> int:
    if limit <= 0:
        return DEFAULT_LIMIT
    if limit > MAX_LIMIT:
        return MAX_LIMIT
    return limit


def _normalize_offset(offset: int) -> int:
    return max(offset, 0)


def _normalize_recent_months(months: int) -> int:
    if months < MIN_RECENT_MONTHS:
        return MIN_RECENT_MONTHS
    return months


def _month_start_n_months_ago(months: int) -> date:
    today = date.today()
    total = today.year * 12 + (today.month - 1) - (months - 1)
    year = total // 12
    month = total % 12 + 1
    return date(year, month, 1)


def _parse_subject_type(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        if value in VALID_SUBJECT_TYPE:
            return value
        return None
    raw = str(value).strip().lower()
    return SUBJECT_TYPE_ALIAS.get(raw)


def _coerce_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_items = [str(item).strip() for item in value]
        return [item for item in raw_items if item]
    if isinstance(value, str):
        raw_items = [item.strip() for item in value.split(",")]
        return [item for item in raw_items if item]
    return []


def _build_search_filter(filters: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    subject_type = _parse_subject_type(filters.get("subject_type"))
    if subject_type is not None:
        result["type"] = [subject_type]
    tags = _coerce_str_list(filters.get("tags"))
    exclude_tags = _coerce_str_list(filters.get("exclude_tags"))
    if tags:
        result["tag"] = tags
    if exclude_tags:
        result["meta_tags"] = [f"-{tag}" for tag in exclude_tags]
    _append_air_date_filter(result, filters)
    _append_rating_filter(result, filters)
    _append_rank_filter(result, filters)
    nsfw = filters.get("nsfw")
    if isinstance(nsfw, bool):
        result["nsfw"] = nsfw
    return result


def _append_air_date_filter(result: dict[str, Any], filters: dict[str, Any]) -> None:
    year_from = filters.get("year_from")
    year_to = filters.get("year_to")
    if not isinstance(year_from, int) and not isinstance(year_to, int):
        return
    air_date: list[str] = []
    if isinstance(year_from, int):
        air_date.append(f">={year_from}-01-01")
    if isinstance(year_to, int):
        air_date.append(f"<={year_to}-12-31")
    result["air_date"] = air_date


def _append_rating_filter(result: dict[str, Any], filters: dict[str, Any]) -> None:
    rating_min = filters.get("rating_min")
    rating_max = filters.get("rating_max")
    values: list[str] = []
    if rating_min is not None:
        values.append(f">={rating_min}")
    if rating_max is not None:
        values.append(f"<={rating_max}")
    if values:
        result["rating"] = values


def _append_rank_filter(result: dict[str, Any], filters: dict[str, Any]) -> None:
    rank_max = filters.get("rank_max")
    if isinstance(rank_max, int) and rank_max > 0:
        result["rank"] = [f"<={rank_max}"]


def _extract_score(subject: dict[str, Any]) -> Optional[float]:
    rating = subject.get("rating")
    if not isinstance(rating, dict):
        return None
    score = rating.get("score")
    if isinstance(score, (int, float)):
        return float(score)
    return None


def _extract_rank(subject: dict[str, Any]) -> Optional[int]:
    rating = subject.get("rating")
    if not isinstance(rating, dict):
        return None
    rank = rating.get("rank")
    if isinstance(rank, int) and rank > 0:
        return rank
    return None


def _format_subject_line(index: int, subject: dict[str, Any]) -> str:
    title = subject.get("name_cn") or subject.get("name") or "未知条目"
    sid = subject.get("id", "未知ID")
    relation = subject.get("_relation")
    score = _extract_score(subject)
    rank = _extract_rank(subject)
    date_value = subject.get("date", "未知")
    lines = [f"{index}. {title} (ID: {sid})"]
    if isinstance(relation, str) and relation:
        lines.append(f"关联类型: {relation}")
    lines.append(f"评分: {score if score is not None else '暂无'}")
    lines.append(f"排名: {rank if rank is not None else '暂无'}")
    lines.append(f"日期: {date_value}")
    lines.append(f"链接: https://bgm.tv/subject/{sid}")
    return "\n".join(lines)


def _format_subject_list(title: str, subjects: list[dict[str, Any]]) -> str:
    lines = [f"{title}:"]
    for idx, subject in enumerate(subjects, start=1):
        lines.append(_format_subject_line(idx, subject))
        lines.append("")
    lines.append("请分点，贴心地回答。不要输出 markdown 格式。")
    return "\n".join(lines)


def _has_non_empty_tags(filter_payload: dict[str, Any]) -> bool:
    tag_values = filter_payload.get("tag")
    if not isinstance(tag_values, list):
        return False
    return any(isinstance(item, str) and item.strip() for item in tag_values)


async def _search_subjects_with_fallback(
    client: BangumiApiClient,
    *,
    keyword: str,
    filter_payload: dict[str, Any],
    sort: str,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    if not keyword or not _has_non_empty_tags(filter_payload):
        return []
    fallback_sort = "heat" if sort == "match" else sort
    return await client.search_subjects(
        keyword="",
        filter_payload=filter_payload,
        sort=fallback_sort,
        limit=limit,
        offset=offset,
    )


@dataclass
class BgmAdvancedSubjectSearchTool(FunctionTool):
    name: str = "bgm_search_subjects_advanced"
    description: str = (
        "当用户要在 ACG 领域中按关键词+筛选条件检索时调用。"
        "适用于按年份、标签、评分、排名、类型等条件找作品。"
    )
    token: str = ""
    user_agent: str = DEFAULT_BANGUMI_USER_AGENT
    parameters: dict = Field(default_factory=build_advanced_parameters_schema)

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        keyword: str,
        filters: Optional[dict[str, Any]] = None,
        sort: str = "match",
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> str | CallToolResult:
        _ = context
        client = BangumiApiClient(token=self.token, user_agent=self.user_agent)
        search_filters = filters if isinstance(filters, dict) else {}
        normalized_keyword = keyword.strip()
        normalized_sort = sort if sort in VALID_SORT else "match"
        normalized_limit = _normalize_limit(limit)
        normalized_offset = _normalize_offset(offset)
        filter_payload = _build_search_filter(search_filters)
        subjects = await client.search_subjects(
            keyword=normalized_keyword,
            filter_payload=filter_payload,
            sort=normalized_sort,
            limit=normalized_limit,
            offset=normalized_offset,
        )
        if not subjects:
            subjects = await _search_subjects_with_fallback(
                client,
                keyword=normalized_keyword,
                filter_payload=filter_payload,
                sort=normalized_sort,
                limit=normalized_limit,
                offset=normalized_offset,
            )
        if not subjects:
            return "未检索到符合条件的条目。请调整关键词或筛选条件后重试。"
        return _format_subject_list("高级条目搜索结果", subjects)


@dataclass
class BgmRecommendHotSubjectsTool(FunctionTool):
    name: str = "bgm_recommend_hot_subjects"
    description: str = (
        "当用户想看近期热门/热度榜/最近值得看的 ACG 条目时调用。"
        "仅用于热门推荐，不用于关键词精确检索。"
    )
    token: str = ""
    user_agent: str = DEFAULT_BANGUMI_USER_AGENT
    parameters: dict = Field(
        default_factory=lambda: build_recent_hot_parameters_schema(
            min_recent_months=MIN_RECENT_MONTHS
        )
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        months: int = DEFAULT_RECENT_MONTHS,
        filters: Optional[dict[str, Any]] = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> str | CallToolResult:
        _ = context
        client = BangumiApiClient(token=self.token, user_agent=self.user_agent)
        normalized_months = _normalize_recent_months(months)
        start_date = _month_start_n_months_ago(normalized_months).isoformat()
        raw_filters = dict(filters) if isinstance(filters, dict) else {}
        raw_filters.setdefault("subject_type", 2)
        search_filter = _build_search_filter(raw_filters)
        if "air_date" not in search_filter:
            search_filter["air_date"] = [f">={start_date}"]
        normalized_limit = _normalize_limit(limit)
        normalized_offset = _normalize_offset(offset)
        subjects = await client.search_subjects(
            keyword="",
            filter_payload=search_filter,
            sort="heat",
            limit=normalized_limit,
            offset=normalized_offset,
        )
        if not subjects:
            return "未找到符合条件的近期热门条目。可放宽筛选或扩大 months。"
        return _format_subject_list(
            f"近期热门条目（近 {normalized_months} 个月，按热度）", subjects
        )
