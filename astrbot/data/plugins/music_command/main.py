"""NetEase music cards and Pro-only original songs powered by Google Lyria."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Music
from astrbot.api.star import Context, Star, StarTools

from ..claude_code_agent.agent_core import upload_aiocqhttp_group_file
from ..draw_command.pro_access import Tier, get_tier

MUSIC_PROXY_URL = "http://127.0.0.1:3000/v1/music/generations"
MAX_SONG_BYTES = 20 * 1024 * 1024
SONG_DAILY_LIMIT = 1
PRO_SONG_MESSAGE = "原创歌曲生成是 Pro 专属功能。发送 /pro status 查看资格。"
_MUSIC_COMMAND = re.compile(r"^\s*(?:/music|/网易云|网易云音乐)\s+(.+?)\s*$", re.I)
_SING_COMMAND = re.compile(r"^\s*/sing\s+(.+?)\s*$", re.I)
MUSIC_MEMORY = (
    "\u3010\u97f3\u4e50\u80fd\u529b\u3011\u4ec5\u5728\u7528\u6237\u660e\u786e\u8bf7\u6c42\u65f6\u8bf4\u660e\u6216\u5f15\u5bfc\u3002"
    "\u7528\u6237\u8bf4\u201c\u53d1\u9001/\u5206\u4eab\u7f51\u6613\u4e91\u97f3\u4e50 + \u6b4c\u66f2 ID \u6216\u5206\u4eab\u94fe\u63a5\u201d\u65f6\uff0c\u53d1\u9001\u7f51\u6613\u4e91\u97f3\u4e50\u5361\uff0c\u4e0d\u4e0b\u8f7d\u6216\u8f6c\u53d1\u7248\u6743\u97f3\u9891\u3002"
    "\u7528\u6237\u660e\u786e\u8bf4\u201c\u5531/\u5199/\u521b\u4f5c/\u751f\u6210\u4e00\u9996\u539f\u521b\u6b4c\u201d\u65f6\uff0c\u624d\u89e6\u53d1 Pro \u539f\u521b\u6b4c\u66f2\uff0c\u6bcf\u65e5 1 \u9996\u3002"
    "\u201c\u627e\u6b4c\u3001\u63a8\u8350\u3001\u64ad\u653e\u67d0\u9996\u6b4c\u3001\u5531\u67d0\u4e2a\u6b4c\u624b\u6216\u5df2\u6709\u6b4c\u66f2\u201d\u4e0d\u89e6\u53d1\u6b4c\u66f2\u751f\u6210\uff1b\u9700\u6e05\u695a\u544a\u77e5\u4ec5\u652f\u6301\u7f51\u6613\u4e91\u97f3\u4e50\u5361\uff08\u9700 ID/\u94fe\u63a5\uff09\u6216\u539f\u521b\u6b4c\u66f2\u3002"
)
_NATURAL_NETEASE_COMMAND = re.compile(
    r"^\s*(?:\u5c0f\u67e0[\uff0c,\uff1a:\s]*)?(?:(?:\u5e2e\u6211|\u7ed9\u6211|\u8bf7)[\uff0c,\uff1a:\s]*)?"
    r"(?:\u53d1\u9001|\u53d1|\u5206\u4eab)(?:\u4e00\u9996)?\u7f51\u6613\u4e91(?:\u97f3\u4e50|\u6b4c\u66f2)?\s+(.+?)\s*$",
    re.I,
)
_NATURAL_ORIGINAL_SONG = re.compile(
    r"^\s*(?:\u5c0f\u67e0[\uff0c,\uff1a:\s]*)?(?:(?:\u5e2e\u6211|\u7ed9\u6211|\u8bf7)[\uff0c,\uff1a:\s]*)?"
    r"(?:\u5531|\u5199|\u521b\u4f5c|\u751f\u6210)(?:\u4e00\u9996|\u9996|\u4e00\u6bb5|\u4e2a)?\s*"
    r"(?P<prompt>(?=[^\n]*\u539f\u521b)(?=[^\n]*(?:\u6b4c\u66f2|\u97f3\u4e50|\u6b4c)).+?)\s*$",
    re.I,
)


def parse_netease_song_id(text: str) -> str | None:
    value_text = str(text or "")
    match = _MUSIC_COMMAND.match(value_text) or _NATURAL_NETEASE_COMMAND.match(value_text)
    if not match:
        return None
    value = match.group(1).strip()
    if value.isdigit():
        return value
    try:
        parsed = urlparse(value)
        query = parsed.query
        if parsed.fragment and "?" in parsed.fragment:
            query = f"{query}&{parsed.fragment.split('?', 1)[1]}"
        song_id = (parse_qs(query).get("id") or [""])[0]
    except ValueError:
        return ""
    return song_id if str(song_id).isdigit() else ""


def parse_original_song_prompt(text: str) -> str | None:
    """Accept the explicit command or a narrowly-scoped natural-language request."""
    value_text = str(text or "")
    command_match = _SING_COMMAND.match(value_text)
    if command_match:
        return command_match.group(1).strip()
    natural_match = _NATURAL_ORIGINAL_SONG.match(value_text)
    return natural_match.group("prompt").strip() if natural_match else None


def netease_music_card(song_id: str) -> Music:
    """Preserve OneBot's platform type despite Music's private-field limitation."""
    card = Music(id=int(song_id))
    object.__setattr__(card, "_type", "163")
    return card


class MusicCommand(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        data_dir = Path(StarTools.get_data_dir("music_command"))
        data_dir.mkdir(parents=True, exist_ok=True)
        self._usage_file = data_dir / "usage.json"
        self._daily_usage = self._load_usage()
        self._output_root = Path(__file__).resolve().parents[4] / "claude_workspace" / "pro_music"

    @staticmethod
    def _sender(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_sender_id", None)
        return str(getter() if callable(getter) else "").strip()

    @staticmethod
    def _pro_db_path() -> Path:
        return Path(__file__).resolve().parents[2] / "plugin_data" / "xiaoning_pro" / "pro_members.db"

    def _load_usage(self) -> dict[str, int]:
        try:
            raw = json.loads(self._usage_file.read_text(encoding="utf-8"))
            today = time.strftime("%Y%m%d")
            return {str(key): int(value) for key, value in raw.items() if str(key).endswith(f":{today}")}
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}

    def _save_usage(self) -> None:
        temporary = self._usage_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._daily_usage), encoding="utf-8")
        temporary.replace(self._usage_file)

    @staticmethod
    def _request_song(prompt: str) -> tuple[bytes, str]:
        response = requests.post(MUSIC_PROXY_URL, json={"prompt": prompt}, timeout=(30, 300))
        response.raise_for_status()
        data = response.json().get("data", [])
        if not data or not isinstance(data[0].get("b64_json"), str):
            raise ValueError("missing music response")
        payload = base64.b64decode(data[0]["b64_json"], validate=True)
        if not payload or len(payload) > MAX_SONG_BYTES:
            raise ValueError("invalid song size")
        return payload, str(data[0].get("mime_type") or "audio/mpeg")

    def _save_song(self, payload: bytes, mime: str) -> Path:
        self._output_root.mkdir(parents=True, exist_ok=True)
        ext = ".wav" if "wav" in mime.lower() else ".mp3"
        path = self._output_root / f"song-{uuid.uuid4().hex}{ext}"
        path.write_bytes(payload)
        return path

    async def _deliver_song(self, event: AstrMessageEvent, path: Path):
        get_group_id = getattr(event, "get_group_id", None)
        group_id = str(get_group_id() if callable(get_group_id) else "").strip()
        if group_id and hasattr(event, "bot"):
            try:
                await upload_aiocqhttp_group_file(event.bot, group_id, path)
                return event.plain_result(f"原创歌曲已上传到群文件：{path.name}")
            except Exception as exc:
                logger.error("[MusicCmd] group song delivery failed: %s", type(exc).__name__)
                return event.plain_result("歌曲已生成，但上传到群文件失败，请稍后重试。")
        return event.chain_result([File(name=path.name, file=str(path))])

    @filter.on_llm_request(priority=-20)
    async def inject_music_memory(self, event: AstrMessageEvent, req) -> None:
        """Keep the persona's music guidance aligned with the explicit command router."""
        system_prompt = str(getattr(req, "system_prompt", "") or "")
        if "\u3010\u97f3\u4e50\u80fd\u529b\u3011" not in system_prompt:
            req.system_prompt = f"{system_prompt}\n\n{MUSIC_MEMORY}".strip()

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=936)
    async def on_message(self, event: AstrMessageEvent):
        text = str(getattr(event, "get_message_str", lambda: "")() or "")
        song_id = parse_netease_song_id(text)
        song_prompt = parse_original_song_prompt(text)
        if song_id is None and song_prompt is None:
            return
        if not (event.is_private_chat() or event.is_at_or_wake_command):
            return
        if song_id is not None:
            if not song_id:
                yield event.plain_result("请发送网易云歌曲 ID 或歌曲分享链接，例如：/music 123456")
            else:
                yield event.chain_result([netease_music_card(song_id)])
            event.stop_event()
            return

        sender_id = self._sender(event)
        if not sender_id.isdigit() or get_tier(sender_id, self._pro_db_path()) < Tier.PRO:
            yield event.plain_result(PRO_SONG_MESSAGE)
            event.stop_event()
            return
        prompt = song_prompt
        if not prompt:
            yield event.plain_result("用法：/sing <原创歌曲描述>。请勿要求模仿歌手或复刻已有歌曲。")
            event.stop_event()
            return
        usage_key = f"{sender_id}:{time.strftime('%Y%m%d')}"
        if self._daily_usage.get(usage_key, 0) >= SONG_DAILY_LIMIT:
            yield event.plain_result("今日原创歌曲生成次数已用完（1/1）。")
            event.stop_event()
            return
        yield event.plain_result("原创歌曲生成中，约需 1-3 分钟，请稍候。")
        try:
            payload, mime = await asyncio.to_thread(self._request_song, prompt)
            path = self._save_song(payload, mime)
        except Exception as exc:
            logger.warning("[MusicCmd] song generation failed: %s", type(exc).__name__)
            yield event.plain_result("原创歌曲生成失败，请稍后重试。")
            event.stop_event()
            return
        self._daily_usage[usage_key] = self._daily_usage.get(usage_key, 0) + 1
        try:
            self._save_usage()
        except OSError:
            logger.warning("[MusicCmd] usage persistence failed")
        event.set_extra("_pro_music_output_paths", [str(path)])
        yield await self._deliver_song(event, path)
        event.stop_event()

    @filter.after_message_sent(priority=-1000)
    async def cleanup_sent_songs(self, event: AstrMessageEvent) -> None:
        paths = event.get_extra("_pro_music_output_paths", []) or []
        event.set_extra("_pro_music_output_paths", [])

        async def _delayed_cleanup():
            await asyncio.sleep(60)
            root = self._output_root.resolve(strict=False)
            for raw in paths:
                try:
                    candidate = Path(str(raw)).resolve(strict=True)
                except OSError:
                    continue
                if root in candidate.parents and candidate.suffix.lower() in {".mp3", ".wav"}:
                    candidate.unlink(missing_ok=True)

        asyncio.ensure_future(_delayed_cleanup())
