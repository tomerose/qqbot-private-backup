"""Smart translation through the authenticated local model gateway."""

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
    from draw_command.pro_access import get_tier, Tier
except ImportError:
    from data.plugins.draw_command.pro_access import get_tier, Tier

GEMINI_PROXY = "http://127.0.0.1:3000/v1/chat/completions"
LANG_NAMES: dict[str, str] = {
    "en": "English", "zh": "中文", "ja": "日本語", "ko": "한국어",
    "fr": "Français", "de": "Deutsch", "es": "Español", "ru": "Русский",
    "ar": "العربية", "pt": "Português", "it": "Italiano", "th": "ไทย",
    "vi": "Tiếng Việt", "id": "Bahasa Indonesia",
}
LANG_ALIASES = {
    "英文": "en", "英语": "en", "中文": "zh", "汉语": "zh",
    "日文": "ja", "日语": "ja", "韩文": "ko", "韩语": "ko",
    "法文": "fr", "法语": "fr", "德文": "de", "德语": "de",
    "西班牙文": "es", "西班牙语": "es", "俄文": "ru", "俄语": "ru",
    "阿拉伯文": "ar", "阿拉伯语": "ar", "葡萄牙文": "pt", "葡萄牙语": "pt",
    "意大利文": "it", "意大利语": "it", "泰文": "th", "泰语": "th",
    "越南文": "vi", "越南语": "vi", "印尼文": "id", "印尼语": "id",
}
_COMMAND = re.compile(r"^/(?:translate|tr|翻译)\s+(\S+)\s+(.+)$", re.S | re.I)
_NATURAL_CONTENT_FIRST = re.compile(
    r"^(?:小柠[，,：:\s]*)?(?:帮我|请)?把?\s*(?P<content>.+?)\s*"
    r"翻译(?:成|为)\s*(?P<target>[\w\u4e00-\u9fff-]{2,20})[。！!？?]?$",
    re.S | re.I,
)
_NATURAL_TARGET_FIRST = re.compile(
    r"^(?:小柠[，,：:\s]*)?(?:帮我|请)?翻译(?:成|为)\s*"
    r"(?P<target>[\w\u4e00-\u9fff-]{2,20})[：:，,\s]+(?P<content>.+)$",
    re.S | re.I,
)


def parse_translate_request(text: str) -> tuple[str, str] | None:
    value = str(text or "").strip()
    match = _COMMAND.match(value)
    if match:
        target, content = match.groups()
    else:
        match = _NATURAL_TARGET_FIRST.match(value) or _NATURAL_CONTENT_FIRST.match(value)
        if not match:
            return None
        target, content = match.group("target"), match.group("content")
    target = LANG_ALIASES.get(target.lower(), target.lower())
    content = content.strip()
    return (target, content) if content else None


class SmartTranslate(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}
        project_root = Path(__file__).resolve().parents[4]
        self._pro_db = (
            project_root / "astrbot" / "data" / "plugin_data"
            / "xiaoning_pro" / "pro_members.db"
        )

    def _translate_config(self, sender_id: str) -> tuple[str, str, str]:
        """Return (api_base, api_key, model) based on tier."""
        try:
            tier = get_tier(sender_id, self._pro_db)
            if tier >= Tier.X:
                return GEMINI_PROXY, "sk-gemini-vertex", "gemini-2.5-flash"
        except Exception:
            pass
        return GEMINI_PROXY, "local-proxy", "gemini-2.5-flash-lite"

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=980)
    @defer_stop_event
    async def on_message(self, event: AstrMessageEvent):
        text = self._msg(event)
        request = parse_translate_request(text)
        if request is None:
            return
        if not (event.is_private_chat() or event.is_at_or_wake_command):
            return
        event.stop_event()
        target, content = request
        if len(content) < 1 or len(content) > 5000:
            yield event.plain_result("翻译内容需在 1-5000 字符之间。")
            return
        lang_label = LANG_NAMES.get(target, target.upper())

        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
        api_base, api_key, model = self._translate_config(sender_id)
        yield event.plain_result(f"🔄 翻译中 → {lang_label}…")

        try:
            resp = await asyncio.to_thread(
                requests.post,
                api_base,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                f"You are a professional translator. "
                                f"Translate the user's text into {LANG_NAMES.get(target, target)}. "
                                f"Output ONLY the translation, no explanations, no notes, no quotes."
                            ),
                        },
                        {"role": "user", "content": content},
                    ],
                    "max_tokens": min(len(content) * 4, 4000),
                    "temperature": 0.1,
                },
                timeout=30,
            )
            result = chat_response_content(resp)
        except Exception as exc:
            yield event.plain_result(f"翻译服务暂时不可用：{str(exc) or type(exc).__name__}")
            return

        yield event.plain_result(result)

    @staticmethod
    def _msg(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_message_str", None)
        if callable(getter):
            return str(getter() or "")
        return str(getattr(event, "message_str", "") or "")
