from datetime import datetime
from typing import Any

from astrbot.api import FunctionTool
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from mcp.types import CallToolResult
from pydantic import Field
from pydantic.dataclasses import dataclass

from ..core.models import SubscriptionRecord
from ..core.utils import render_text_to_plain

DEFAULT_LIMIT = 3
MIN_LIMIT = 1
MAX_LIMIT = 10


def _normalize_limit(limit: int) -> int:
    if limit < MIN_LIMIT:
        return MIN_LIMIT
    if limit > MAX_LIMIT:
        return MAX_LIMIT
    return limit


def _extract_pub_time(item: dict[str, Any]) -> str:
    author = item.get("modules", {}).get("module_author", {})
    if not isinstance(author, dict):
        return "未知时间"
    pub_time = author.get("pub_time")
    if isinstance(pub_time, str) and pub_time.strip():
        return pub_time.strip()
    pub_ts = author.get("pub_ts")
    if isinstance(pub_ts, int) and pub_ts > 0:
        return datetime.fromtimestamp(pub_ts).strftime("%Y-%m-%d %H:%M:%S")
    return "未知时间"


def _payload_main_text(payload: Any) -> str:
    summary = (getattr(payload, "summary", "") or "").strip()
    if summary:
        return summary
    return render_text_to_plain(getattr(payload, "text", "") or "").strip()


def _format_dynamic_block(index: int, payload: Any, pub_time: str) -> str:
    lines = [
        f"{index}. 类型: {getattr(payload, 'type', '') or '未知类型'}",
        f"发布时间: {pub_time}",
    ]

    title = (getattr(payload, "title", "") or "").strip()
    if title:
        lines.append(f"标题: {title}")

    main_text = _payload_main_text(payload)
    if main_text:
        lines.append(f"正文: {main_text}")

    forward = getattr(payload, "forward", None)
    if forward:
        forward_text = _payload_main_text(forward)
        if forward_text:
            lines.append(f"转发原文: {forward_text}")

    url = (getattr(payload, "url", "") or "").strip()
    if url:
        lines.append(f"链接: {url}")

    return "\n".join(lines)


@dataclass
class BiliUserDynamicsTool(FunctionTool):
    name: str = "bili_get_user_dynamics"
    description: str = (
        "当用户想查询某个 Bilibili UP 主最近动态、让你总结近期发文内容、"
        "分析动态主题或语气时调用。输入 UID，返回最近几条动态的结构化文本。"
    )
    bili_client: Any = None
    parse_dynamics: Any = None
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "uid": {
                    "type": "integer",
                    "description": "要查询的 Bilibili 用户 UID。",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回动态条数，范围 1-10，默认 3。",
                    "minimum": MIN_LIMIT,
                    "maximum": MAX_LIMIT,
                },
            },
            "required": ["uid"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        uid: int,
        limit: int = DEFAULT_LIMIT,
    ) -> str | CallToolResult:
        _ = context
        if self.bili_client is None or self.parse_dynamics is None:
            raise RuntimeError("bilibili dynamics tool 未正确初始化")

        normalized_limit = _normalize_limit(limit)
        user_info, _ = await self.bili_client.get_user_info(int(uid))
        dynamics = await self.bili_client.get_latest_dynamics(int(uid))

        if not dynamics:
            return (
                "获取该 UP 主动态失败，可能是未登录、UID 无效，或 B 站接口暂时不可用。"
            )

        parsed_results = self.parse_dynamics(
            dynamics,
            SubscriptionRecord(uid=int(uid)),
        )

        payload_blocks: list[str] = []
        for result, item in zip(
            parsed_results, dynamics.get("items", []), strict=False
        ):
            if not result.has_payload():
                continue
            payload_blocks.append(
                _format_dynamic_block(
                    len(payload_blocks) + 1,
                    result.payload,
                    _extract_pub_time(item),
                )
            )
            if len(payload_blocks) >= normalized_limit:
                break

        if not payload_blocks:
            return "该 UP 主最近没有可解析的动态，或者动态都被过滤屏蔽了。"

        up_name = "未知UP"
        if isinstance(user_info, dict):
            up_name = str(user_info.get("name", "") or up_name)

        lines = [
            f"UP主: {up_name}",
            f"UID: {uid}",
            f"最近动态共返回 {len(payload_blocks)} 条。",
            "",
            *payload_blocks,
            "",
            "请基于以上动态内容回答，不要编造未提供的信息。",
        ]
        return "\n".join(lines)
