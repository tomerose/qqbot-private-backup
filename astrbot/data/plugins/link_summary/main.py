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
_NL_SUMMARY = re.compile(
    r"(?:帮我|请|给我|麻烦)?\s*(?:总结|概括|摘要|读一下|看看|分析|看一下)"
    r"(?:一下|这个|那个|这篇|这份)?\s*(?:链接|网址|url|网页)",
    re.I,
)
PROXY = "http://127.0.0.1:3000/v1/chat/completions"
_PUBLIC_READ_COMMANDS = ("/summary", "/摘要", "/browse", "/浏览")


def _is_public_read_command(text: str) -> bool:
    return str(text or "").lstrip().lower().startswith(_PUBLIC_READ_COMMANDS)


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

        # Group chat: require @mention, /summary, NL, or Pro group
        is_private = event.is_private_chat()
        has_nl = bool(_NL_SUMMARY.search(text))
        if not is_private:
            is_at = getattr(event, "is_at_or_wake_command", False)
            has_cmd = _is_public_read_command(text)
            group_id = str(getattr(event, "get_group_id", lambda: "")() or "")
            in_pro_group = bool(group_id) and is_active_pro_group(group_id, self._pro_db)
            if not is_at and not has_cmd and not has_nl and not in_pro_group:
                return

        urls = URL_RE.findall(text)
        if not urls:
            # NL trigger but no URL in this message — check reply for URL
            if has_nl or _is_public_read_command(text):
                reply = getattr(event, "get_reply_obj", None)
                if callable(reply):
                    reply = reply()
                if reply is not None:
                    reply_text = str(getattr(reply, "message", "") or "")
                    urls = URL_RE.findall(reply_text)
            if not urls:
                if has_nl or _is_public_read_command(text):
                    yield event.plain_result('把公开链接一起发来，或回复链接消息后说“总结一下”。/browse 只阅读公开网页，不登录、不填写表单。')
                    event.stop_event()
                return
        event.stop_event()

        url = urls[0]
        yield event.plain_result(f"🔍 正在阅读链接…")

        try:
            resp = await asyncio.to_thread(
                requests.post,
                PROXY,
                json={
                    "model": "gemini-3.5-flash",
                    "url_context": True,
                    "google_search": True,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "你是链接摘要助手。用户给你一个URL，你需要先尝试读取该网页内容，"
                                "然后用中文200字以内总结：主题、主要观点、一个关键要点。"
                                "如果无法访问该URL，诚实说明原因，建议用户粘贴原文。"
                                "不要编造内容，只总结实际读到的信息。"
                            ),
                        },
                        {
                            "role": "user",
                            "content": f"请阅读并总结这个链接的内容：{url}",
                        },
                    ],
                    "max_tokens": 800,
                },
                timeout=(15, 60),
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
