"""Link summarizer — extract URLs and summarize via Gemini."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import requests
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
try:
    from xiaoning_runtime import chat_response_content, defer_stop_event
except ImportError:
    from data.plugins.xiaoning_runtime import chat_response_content, defer_stop_event

try:
    from draw_command.pro_access import is_active_pro_group
except ImportError:
    from data.plugins.draw_command.pro_access import is_active_pro_group

URL_RE = re.compile(r"https?://[^\s一-鿿，。；！？、]+")
PROXY = "http://127.0.0.1:3000/v1/chat/completions"


class LinkSummary(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}
        project_root = Path(__file__).resolve().parents[4]
        self._pro_db = (
            project_root / "astrbot" / "data" / "plugin_data"
            / "xiaoning_pro" / "pro_members.db"
        )

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=970)
    @defer_stop_event
    async def on_message(self, event: AstrMessageEvent):
        text = self._msg(event)

        # Group chat: require @mention, /summary, or Pro group
        if not event.is_private_chat():
            is_at = getattr(event, "is_at_or_wake_command", False)
            has_cmd = text.startswith("/summary") or text.startswith("/摘要")
            group_id = str(getattr(event, "get_group_id", lambda: "")() or "")
            in_pro_group = bool(group_id) and is_active_pro_group(group_id, self._pro_db)
            if not is_at and not has_cmd and not in_pro_group:
                return

        urls = URL_RE.findall(text)
        if not urls:
            return
        event.stop_event()

        url = urls[0]
        yield event.plain_result(f"🔍 正在阅读链接…")

        try:
            resp = await asyncio.to_thread(
                requests.post,
                PROXY,
                json={
                    "model": "gemini-2.5-flash-search",
                    "google_search": True,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are a helpful assistant. The user gives you a URL. "
                                "If you can read the page content, summarize it in 200 "
                                "Chinese characters: key topic, main argument, and one "
                                "key takeaway. If you cannot access the URL, say so "
                                "honestly and suggest the user paste the relevant text."
                            ),
                        },
                        {
                            "role": "user",
                            "content": f"Please summarize this URL: {url}",
                        },
                    ],
                    "max_tokens": 600,
                },
                timeout=30,
            )
            result = chat_response_content(resp)
        except Exception as exc:
            yield event.plain_result(f"摘要服务暂时不可用：{str(exc) or type(exc).__name__}")
            return

        yield event.plain_result(f"📄 {result}\n\n🔗 {url}")

    @staticmethod
    def _msg(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_message_str", None)
        if callable(getter):
            return str(getter() or "")
        return str(getattr(event, "message_str", "") or "")
