"""QQ drawing command with a shared weekly limit for ordinary and GO users."""

from __future__ import annotations

import asyncio
import base64
import io
import time
import uuid
from pathlib import Path

import requests
from PIL import Image as PillowImage
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star

from .draw_core import (
    DrawRateLimiter,
    DrawRequestError,
    parse_draw_command,
)
from .pro_access import get_tier, is_active_pro_group, Tier
from .pro_client import ProClient


DRAW_PROXY_URL = "http://127.0.0.1:3000/v1/images/generations"
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_EDGE = 4096
DRAW_PRO_DAILY = 10
DRAW_GO_WEEKLY = 6
DRAW_LIMIT_MSG = "作图次数已用完（今日 {used}/{limit}）。明天自动重置。"
DRAW_GO_LIMIT_MSG = "本周作图已用 {used}/{limit} 次。下周自动重置。"
DRAW_MEMORY = (
    "【作图能力】普通用户和 GO 都可使用 /draw 或自然语言作图，每周 6 次；"
    "Pro 每天 10 次。只在用户明确要求画图时触发，不承诺生成失败的结果。"
)


class DrawCommand(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}
        self._rate_limiter = DrawRateLimiter(
            cooldown_seconds=int(self.config.get("cooldown_seconds", 75))
        )
        self._generation_lock = asyncio.Lock()
        self._daily_usage: dict[str, int] = {}
        project_root = Path(__file__).resolve().parents[4]
        self._output_root = project_root / "claude_workspace" / "pro_draw"
        self._pro_db_path = project_root / "astrbot" / "data" / "plugin_data" / "xiaoning_pro" / "pro_members.db"
        self._pro_client = ProClient(self._pro_db_path)

    def _is_pro(self, sender_id: str) -> bool:
        """Pro access is granted ONLY via HMAC-signed DB membership.

        There is no config-file bypass. The owner's permanent membership
        is maintained by ProStore.ensure_owner_membership() on startup.
        """
        return self._pro_client.is_active(sender_id)

    @staticmethod
    def _message_text(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_message_str", None)
        if callable(getter):
            return str(getter() or "")
        return str(getattr(event, "message_str", "") or "")

    @staticmethod
    def _is_allowed_context(event: AstrMessageEvent) -> bool:
        return bool(event.is_private_chat() or event.is_at_or_wake_command)

    @staticmethod
    def _safe_sender_id(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_sender_id", None)
        return str(getter() if callable(getter) else "").strip()

    @staticmethod
    def _decode_proxy_image(response: requests.Response) -> bytes:
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        encoded = data[0].get("b64_json") if isinstance(data, list) and data else None
        if not isinstance(encoded, str):
            raise ValueError("missing image response")
        image_bytes = base64.b64decode(encoded, validate=True)
        if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
            raise ValueError("invalid image size")
        return image_bytes

    def _request_image(self, prompt: str) -> bytes:
        response = requests.post(
            DRAW_PROXY_URL,
            json={"prompt": prompt, "model": "gemini-2.5-flash-image", "size": "1024x1024"},
            timeout=(30, 180),
        )
        response.raise_for_status()
        return self._decode_proxy_image(response)

    def _save_sanitized_image(self, payload: bytes) -> Path:
        self._output_root.mkdir(parents=True, exist_ok=True)
        target = self._output_root / f"draw-{uuid.uuid4().hex}.png"
        with PillowImage.open(io.BytesIO(payload)) as source:
            source.load()
            if source.width < 1 or source.height < 1:
                raise ValueError("invalid image dimensions")
            if source.width > MAX_IMAGE_EDGE or source.height > MAX_IMAGE_EDGE:
                raise ValueError("image dimensions too large")
            mode = "RGBA" if "transparency" in source.info else "RGB"
            source.convert(mode).save(target, format="PNG", optimize=True)
        return target

    async def _deliver_image(self, event: AstrMessageEvent, path: Path):
        group_id = str(getattr(event, "get_group_id", lambda: "")() or "").strip()
        if group_id and hasattr(event, "bot"):
            try:
                await event.bot.call_action(
                    "upload_group_file",
                    group_id=int(group_id),
                    file=str(path.resolve(strict=True)),
                    name=path.name,
                )
                return event.plain_result(f"图片已上传到群文件：{path.name}")
            except Exception as exc:
                logger.error("[ProDraw] group image delivery failed: %s", type(exc).__name__)
                return event.plain_result("图片已生成，但上传到群文件失败，请稍后重试。")
        return event.chain_result([Image.fromFileSystem(str(path))])

    @filter.on_llm_request(priority=-19)
    async def inject_draw_memory(self, event: AstrMessageEvent, req) -> None:
        system_prompt = str(getattr(req, "system_prompt", "") or "")
        if "【作图能力】" not in system_prompt:
            req.system_prompt = f"{system_prompt}\n\n{DRAW_MEMORY}".strip()

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=940)
    async def on_message(self, event: AstrMessageEvent):
        try:
            prompt = parse_draw_command(self._message_text(event))
        except DrawRequestError as exc:
            yield event.plain_result(str(exc))
            event.stop_event()
            return
        if prompt is None or not self._is_allowed_context(event):
            return

        sender_id = self._safe_sender_id(event)
        group_id = str(getattr(event, "get_group_id", lambda: "")() or "")
        in_pro_group = bool(group_id) and is_active_pro_group(group_id, self._pro_db_path)
        tier = get_tier(sender_id, self._pro_db_path)
        # Ordinary and GO share the same weekly drawing allowance.
        # Pro members and activated Pro groups keep the higher daily allowance.
        today = time.strftime("%Y%m%d")
        dk = f"{sender_id}:{today}"
        year, week_num = time.strftime("%Y"), time.strftime("%W")
        wk = f"{sender_id}:{year}:{week_num}"
        go_used = self._daily_usage.get(wk, 0)
        used = self._daily_usage.get(dk, 0)
        if tier < Tier.PRO and not in_pro_group:
            if go_used >= DRAW_GO_WEEKLY:
                yield event.plain_result(DRAW_GO_LIMIT_MSG.format(used=go_used, limit=DRAW_GO_WEEKLY))
                event.stop_event()
                return
        else:
            limit = DRAW_PRO_DAILY if (tier >= Tier.PRO or in_pro_group) else DRAW_FREE_DAILY
            if used >= limit:
                yield event.plain_result(DRAW_LIMIT_MSG.format(used=used, limit=limit))
                event.stop_event()
                return
        if self._generation_lock.locked():
            yield event.plain_result("我正在画一张图，等这张发出后再试。")
            event.stop_event()
            return
        retry_after = self._rate_limiter.try_acquire(sender_id)
        if retry_after:
            yield event.plain_result(f"作图冷却中，{retry_after} 秒后再试。")
            event.stop_event()
            return

        yield event.plain_result("我开始画了，预计 30–90 秒。")
        try:
            async with self._generation_lock:
                payload = await asyncio.to_thread(self._request_image, prompt)
                output_path = self._save_sanitized_image(payload)
        except Exception as exc:
            logger.warning("[ProDraw] generation failed: %s", type(exc).__name__)
            yield event.plain_result("这次没能画出来，稍后再试。")
            event.stop_event()
            return

        if tier < Tier.PRO and not in_pro_group:
            self._daily_usage[wk] = go_used + 1
        else:
            self._daily_usage[dk] = used + 1
        event.set_extra("_pro_draw_output_paths", [str(output_path)])
        yield await self._deliver_image(event, output_path)
        event.stop_event()

    @filter.after_message_sent(priority=-1000)
    async def cleanup_sent_images(self, event: AstrMessageEvent) -> None:
        paths = event.get_extra("_pro_draw_output_paths", []) or []
        event.set_extra("_pro_draw_output_paths", [])
        # ponytail: NapCat uploads files asynchronously after after_message_sent
        # fires. Schedule deletion with a delay so QQ has time to upload.
        async def _delayed_cleanup():
            await asyncio.sleep(45)
            root = self._output_root.resolve(strict=False)
            for raw_path in paths:
                candidate = Path(str(raw_path or ""))
                if candidate.is_symlink():
                    continue
                try:
                    resolved = candidate.resolve(strict=True)
                except OSError:
                    continue
                if root not in resolved.parents or resolved.suffix.lower() != ".png":
                    continue
                try:
                    resolved.unlink()
                except OSError:
                    continue
        asyncio.ensure_future(_delayed_cleanup())
        # Also clean up stale images older than 10 minutes
        try:
            now = time.time()
            root = self._output_root.resolve(strict=False)
            if root.is_dir():
                for png in root.glob("draw-*.png"):
                    try:
                        if now - png.stat().st_mtime > 600:
                            png.unlink()
                    except OSError:
                        continue
        except Exception:
            pass
