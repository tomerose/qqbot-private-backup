"""PDF analysis — extract text via pypdf, analyze via Gemini. Pro-gated."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import requests
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Plain
from astrbot.api.star import Context, Star
try:
    from xiaoning_runtime import defer_stop_event
except ImportError:
    from data.plugins.xiaoning_runtime import defer_stop_event

try:
    from draw_command.pro_access import get_tier, is_active_pro_group, Tier
except ImportError:
    from data.plugins.draw_command.pro_access import get_tier, is_active_pro_group, Tier

PROXY = "http://127.0.0.1:3000/v1/chat/completions"
MAX_PAGES = 50
MAX_CHARS = 40_000
COOLDOWN_SECONDS = 60
PRO_DAILY_LIMIT = 10
FREE_DAILY_LIMIT = 1
PRO_MSG = "PDF 分析次数已用完（今日 {used}/{limit}）。请联系管理员获取更多次数。"


class PdfAnalysis(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}
        project_root = Path(__file__).resolve().parents[4]
        self._pro_db = (
            project_root / "astrbot" / "data" / "plugin_data"
            / "xiaoning_pro" / "pro_members.db"
        )
        self._cooldowns: dict[str, float] = {}
        self._daily_usage: dict[str, int] = {}

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=960)
    @defer_stop_event
    async def on_message(self, event: AstrMessageEvent):
        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
        if not sender_id:
            return

        components = getattr(getattr(event, "message_obj", None), "message", None) or []
        files = [
            c for c in (components if isinstance(components, list) else [])
            if isinstance(c, File) or (isinstance(c, dict) and c.get("type") == "file")
        ]

        text = self._msg(event)
        if text.startswith("/analysis") or text.startswith("/分析"):
            prompt_override = text.split(maxsplit=1)[1] if " " in text else ""
        else:
            prompt_override = ""
        has_prompt = bool(prompt_override)

        if not files and not has_prompt:
            return
        event.stop_event()

        tier = get_tier(sender_id, self._pro_db)
        group_id = str(getattr(event, "get_group_id", lambda: "")() or "")
        in_pro_group = bool(group_id) and is_active_pro_group(group_id, self._pro_db)
        today = time.strftime("%Y%m%d")
        dk = f"{sender_id}:{today}"
        used = self._daily_usage.get(dk, 0)
        limit = PRO_DAILY_LIMIT if (tier >= Tier.GO or in_pro_group) else FREE_DAILY_LIMIT
        if used >= limit:
            yield event.plain_result(PRO_MSG.format(used=used, limit=limit))
            return
        if tier < Tier.GO:
            yield event.plain_result(
                f"PDF 分析每日免费 {FREE_DAILY_LIMIT} 次（{used}/{FREE_DAILY_LIMIT}）。"
                f"GO/Pro 每日 {PRO_DAILY_LIMIT} 次。发送 /pro status 查看资格。"
            )

        now = time.time()
        last = self._cooldowns.get(sender_id, 0)
        if now - last < COOLDOWN_SECONDS:
            remain = int(COOLDOWN_SECONDS - (now - last))
            yield event.plain_result(f"请 {remain} 秒后再试。")
            return
        self._cooldowns[sender_id] = now

        content = ""
        source_name = ""

        if files:
            file_obj = files[0]
            path_str = (
                getattr(file_obj, "file", "")
                or getattr(file_obj, "path", "")
                or getattr(file_obj, "url", "")
                or (file_obj.get("data", {}).get("file", "") if isinstance(file_obj, dict) else "")
                or (file_obj.get("data", {}).get("path", "") if isinstance(file_obj, dict) else "")
                or (file_obj.get("data", {}).get("url", "") if isinstance(file_obj, dict) else "")
            )
            file_path = Path(str(path_str))
            source_name = file_path.name

            if file_path.suffix.lower() == ".pdf" and file_path.is_file():
                try:
                    from pypdf import PdfReader
                    reader = PdfReader(str(file_path))
                    pages = []
                    total = 0
                    for page in reader.pages[:MAX_PAGES]:
                        pt = (page.extract_text() or "")[:3000]
                        pages.append(pt)
                        total += len(pt)
                        if total > MAX_CHARS:
                            break
                    content = "\n\n".join(pages)
                    source_name = f"{file_path.name} ({len(pages)} 页)"
                except Exception as exc:
                    yield event.plain_result(f"PDF 读取失败：{type(exc).__name__}")
                    return
            elif file_path.suffix.lower() in {".txt", ".md", ".py", ".json", ".csv"} and file_path.is_file():
                try:
                    content = file_path.read_text(encoding="utf-8", errors="replace")[:MAX_CHARS]
                except Exception:
                    yield event.plain_result("文件读取失败。")
                    return
            else:
                yield event.plain_result(
                    "请发送 PDF / TXT / MD 文件。PDF 需为可读取文本（非扫描件图片）。"
                )
                return

        if prompt_override:
            final_prompt = prompt_override
            if content:
                final_prompt = f"文档内容：\n{content[:MAX_CHARS]}\n\n用户要求：{prompt_override}"
        elif content:
            final_prompt = (
                f"请分析以下文档内容，用中文输出：\n"
                f"1. 主题和核心观点\n"
                f"2. 关键论据或数据\n"
                f"3. 局限性或未解决的问题（如有）\n\n"
                f"文档内容：\n{content[:MAX_CHARS]}"
            )
        else:
            yield event.plain_result("请发送一个文件或输入分析要求。")
            return

        yield event.plain_result(f"📄 分析中{f'（{source_name}）' if source_name else ''}…")

        try:
            resp = await asyncio.to_thread(
                requests.post,
                PROXY,
                json={
                    "model": "gemini-2.5-flash",
                    "messages": [
                        {
                            "role": "system",
                            "content": "你是一个专业的文档分析助手。输出结构化、可直接阅读的中文回答。",
                        },
                        {"role": "user", "content": final_prompt},
                    ],
                    "max_tokens": 2000,
                },
                timeout=90,
            )
            resp.raise_for_status()
            result = resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as exc:
            yield event.plain_result(f"分析失败：{type(exc).__name__}")
            return

        self._daily_usage[dk] = used + 1
        yield event.plain_result(result)

    @staticmethod
    def _msg(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_message_str", None)
        if callable(getter):
            return str(getter() or "")
        return str(getattr(event, "message_str", "") or "")
