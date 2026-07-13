"""Smart translation via Gemini Vertex proxy — 100+ languages, zero config."""

from __future__ import annotations

import asyncio
import re
import requests
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
try:
    from xiaoning_runtime import defer_stop_event
except ImportError:
    from data.plugins.xiaoning_runtime import defer_stop_event

TRANSLATE_PROXY = "http://127.0.0.1:3000/v1/chat/completions"
LANG_NAMES: dict[str, str] = {
    "en": "English", "zh": "中文", "ja": "日本語", "ko": "한국어",
    "fr": "Français", "de": "Deutsch", "es": "Español", "ru": "Русский",
    "ar": "العربية", "pt": "Português", "it": "Italiano", "th": "ไทย",
    "vi": "Tiếng Việt", "id": "Bahasa Indonesia",
}


class SmartTranslate(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=980)
    @defer_stop_event
    async def on_message(self, event: AstrMessageEvent):
        text = self._msg(event)
        m = re.match(r"^/(?:translate|tr|翻译)\s+(\w+)\s+(.+)", text, re.S)
        if not m:
            return
        event.stop_event()
        target = m.group(1).strip().lower()
        content = m.group(2).strip()
        if len(content) < 1 or len(content) > 5000:
            yield event.plain_result("翻译内容需在 1-5000 字符之间。")
            return
        lang_label = LANG_NAMES.get(target, target.upper())

        yield event.plain_result(f"🔄 翻译中 → {lang_label}…")

        try:
            resp = await asyncio.to_thread(
                requests.post,
                TRANSLATE_PROXY,
                json={
                    "model": "gemini-2.5-flash",
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
            resp.raise_for_status()
            result = resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as exc:
            yield event.plain_result(f"翻译失败：{type(exc).__name__}")
            return

        yield event.plain_result(result)

    @staticmethod
    def _msg(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_message_str", None)
        if callable(getter):
            return str(getter() or "")
        return str(getattr(event, "message_str", "") or "")
