"""Pro-only QQ drawing command backed by the local Vertex proxy."""

from __future__ import annotations

import asyncio
import base64
import io
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
    parse_pro_user_ids,
)


PRO_DRAW_MESSAGE = "作图是 Pro 功能。要开通或了解 Pro，可发邮件说明用途：portelamicheli636@gmail.com"
DRAW_PROXY_URL = "http://127.0.0.1:3000/v1/images/generations"
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_EDGE = 4096


class DrawCommand(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}
        self._pro_user_ids = frozenset(
            parse_pro_user_ids(self.config.get("pro_user_ids", "1211000567"))
        )
        self._rate_limiter = DrawRateLimiter(
            cooldown_seconds=int(self.config.get("cooldown_seconds", 75))
        )
        self._generation_lock = asyncio.Lock()
        project_root = Path(__file__).resolve().parents[4]
        self._output_root = project_root / "astrbot" / "data" / "temp" / "pro_draw"

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
            json={"prompt": prompt, "model": "gemini-3.1-flash-image", "size": "1024x1024"},
            timeout=(10, 95),
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

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=940)
    async def on_message(self, event: AstrMessageEvent):
        try:
            prompt = parse_draw_command(self._message_text(event))
        except DrawRequestError as exc:
            event.stop_event()
            yield event.plain_result(str(exc))
            return
        if prompt is None or not self._is_allowed_context(event):
            return

        event.stop_event()
        sender_id = self._safe_sender_id(event)
        if sender_id not in self._pro_user_ids:
            yield event.plain_result(PRO_DRAW_MESSAGE)
            return
        if self._generation_lock.locked():
            yield event.plain_result("我正在画一张图，等这张发出后再试。")
            return
        retry_after = self._rate_limiter.try_acquire(sender_id)
        if retry_after:
            yield event.plain_result(f"作图冷却中，{retry_after} 秒后再试。")
            return

        yield event.plain_result("我开始画了，预计 30–90 秒。")
        try:
            async with self._generation_lock:
                payload = await asyncio.to_thread(self._request_image, prompt)
                output_path = self._save_sanitized_image(payload)
        except Exception as exc:
            logger.warning("[ProDraw] generation failed: %s", type(exc).__name__)
            yield event.plain_result("这次没能画出来，稍后再试。")
            return

        event.set_extra("_pro_draw_output_paths", [str(output_path)])
        yield event.chain_result([Image.fromFileSystem(str(output_path))])

    @filter.after_message_sent(priority=-1000)
    async def cleanup_sent_images(self, event: AstrMessageEvent) -> None:
        paths = event.get_extra("_pro_draw_output_paths", []) or []
        event.set_extra("_pro_draw_output_paths", [])
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
