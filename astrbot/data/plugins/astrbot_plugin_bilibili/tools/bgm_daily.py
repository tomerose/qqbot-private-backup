from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api import FunctionTool
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from mcp.types import CallToolResult
from pydantic import Field
from pydantic.dataclasses import dataclass

from ..bgm_client import (
    DEFAULT_BANGUMI_USER_AGENT,
    EP_PAGE_SIZE,
    BangumiApiClient,
)

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
MAX_LIMIT = 20
MIN_LIMIT = 1

DAY_ALIAS = {
    "today": 0,
    "monday": 1,
    "tuesday": 2,
    "wednesday": 3,
    "thursday": 4,
    "friday": 5,
    "saturday": 6,
    "sunday": 7,
    "周一": 1,
    "周二": 2,
    "周三": 3,
    "周四": 4,
    "周五": 5,
    "周六": 6,
    "周日": 7,
    "周天": 7,
    "星期一": 1,
    "星期二": 2,
    "星期三": 3,
    "星期四": 4,
    "星期五": 5,
    "星期六": 6,
    "星期日": 7,
    "星期天": 7,
}

WEEKDAY_NAME_CN = {
    1: "星期一",
    2: "星期二",
    3: "星期三",
    4: "星期四",
    5: "星期五",
    6: "星期六",
    7: "星期日",
}


def _today_weekday_id() -> int:
    return datetime.now(SHANGHAI_TZ).weekday() + 1


def _today_date() -> date:
    return datetime.now(SHANGHAI_TZ).date()


def _resolve_weekday_id(day: str) -> int:
    normalized = day.strip().lower()
    if not normalized:
        return _today_weekday_id()
    if normalized in DAY_ALIAS:
        weekday_id = DAY_ALIAS[normalized]
        return _today_weekday_id() if weekday_id == 0 else weekday_id
    if normalized.isdigit():
        weekday_id = int(normalized)
        if 1 <= weekday_id <= 7:
            return weekday_id
    raise ValueError("day 参数无效，可选 today / monday..sunday / 周一..周日 / 1..7")


def _validate_limit(limit: int) -> int:
    if MIN_LIMIT <= limit <= MAX_LIMIT:
        return limit
    raise ValueError(f"limit 参数必须在 {MIN_LIMIT} 到 {MAX_LIMIT} 之间")


def _pick_day_items(
    calendar_rows: list[dict[str, Any]], weekday_id: int
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in calendar_rows:
        weekday = row.get("weekday", {})
        if isinstance(weekday, dict) and weekday.get("id") == weekday_id:
            raw_items = row.get("items", [])
            if isinstance(raw_items, list):
                items = [item for item in raw_items if isinstance(item, dict)]
            break
    return items


def _parse_airdate(raw_value: Any) -> date | None:
    if not isinstance(raw_value, str):
        return None
    candidate = raw_value.strip()
    if not candidate:
        return None
    try:
        return datetime.strptime(candidate[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


async def _build_progress(
    client: BangumiApiClient, *, subject_id: int, reference_date: date
) -> tuple[int, int]:
    aired = 0
    total = 0
    fetched = 0
    offset = 0
    while True:
        page = await client.get_episodes_page(
            subject_id, ep_type=0, limit=EP_PAGE_SIZE, offset=offset
        )
        page_total = page.get("total")
        if isinstance(page_total, int) and page_total >= 0:
            total = page_total
        data = page.get("data", [])
        if not isinstance(data, list) or not data:
            break
        # 遍历标记已播出的集数
        for episode in data:
            if not isinstance(episode, dict):
                continue
            air_date = _parse_airdate(episode.get("airdate"))
            if air_date and air_date <= reference_date:
                aired += 1
        batch_size = len(data)
        fetched += batch_size
        offset += batch_size
        if total and fetched >= total:
            break
        if batch_size < EP_PAGE_SIZE:
            break
    if total == 0:
        total = fetched
    return aired, total


def _resolve_total_eps(item: dict[str, Any], total_from_episodes: int) -> int:
    for key in ("eps", "eps_count"):
        value = item.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return total_from_episodes


def _format_progress_text(aired: int, total: int) -> str:
    if total <= 0:
        return f"{aired}/?"
    return f"{aired}/{total}"


async def _format_items(
    client: BangumiApiClient,
    *,
    calendar_rows: list[dict[str, Any]],
    weekday_id: int,
    limit: int,
    reference_date: date,
) -> list[str]:
    items = _pick_day_items(calendar_rows, weekday_id)

    lines: list[str] = []
    for item in items:
        item_type = item.get("type")
        if item_type not in (None, 2):
            continue
        subject_id = item.get("id")
        if not isinstance(subject_id, int):
            continue
        aired, total_raw = await _build_progress(
            client, subject_id=subject_id, reference_date=reference_date
        )
        total = _resolve_total_eps(item, total_raw)
        progress_text = _format_progress_text(aired, total)
        title = item.get("name_cn") or item.get("name") or "未命名条目"
        rating = item.get("rating")
        score = rating.get("score") if isinstance(rating, dict) else None
        score_text = f"{score}" if isinstance(score, (int, float)) else "暂无评分"
        url = item.get("url", "")
        air_date = item.get("air_date", "未知日期")
        lines.append(
            f"{len(lines) + 1}. {title} | 进度 {progress_text} | "
            f"首播 {air_date} | 评分 {score_text} | {url}"
        )
        if len(lines) >= limit:
            break
    return lines


@dataclass
class BgmDailyTool(FunctionTool):
    name: str = "get_bgm_daily_schedule"
    description: str = (
        "当用户询问今天或某天更新什么动画时调用。"
        "查询 bgm.tv 每日放送接口并返回该天的动画列表。"
    )
    token: str = ""
    user_agent: str = DEFAULT_BANGUMI_USER_AGENT
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "day": {
                    "type": "string",
                    "description": (
                        "要查询的星期。可选: today, monday..sunday, "
                        "周一..周日, 1..7。默认 today。"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "返回条目数量上限，范围 1-20，默认 10。",
                    "minimum": MIN_LIMIT,
                    "maximum": MAX_LIMIT,
                },
            },
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        day: str = "today",
        limit: int = 10,
    ) -> str | CallToolResult:
        _ = context
        weekday_id = _resolve_weekday_id(day)
        validated_limit = _validate_limit(limit)
        client = BangumiApiClient(token=self.token, user_agent=self.user_agent)
        calendar_rows = await client.get_calendar()
        lines = await _format_items(
            client,
            calendar_rows=calendar_rows,
            weekday_id=weekday_id,
            limit=validated_limit,
            reference_date=_today_date(),
        )
        weekday_cn = WEEKDAY_NAME_CN.get(weekday_id, f"星期{weekday_id}")
        if not lines:
            return f"{weekday_cn} 暂无可展示的动画放送数据。"

        content = "\n".join(lines)
        return (
            f"{weekday_cn} 动画放送（最多 {validated_limit} 条）:\n{content}\n"
            "请用简洁自然语言回答，不要输出 markdown。"
        )
