"""QQ drawing command with a shared weekly limit for ordinary and X users."""

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
from astrbot.api.star import Context, Star

from .draw_core import (
    DrawRateLimiter,
    DrawRequestError,
    parse_draw_command,
    parse_edit_command,
    parse_draw_options,
    is_dewatermark_request,
    _DEWATERMARK_PROMPT,
)
from .pro_access import get_tier, is_active_pro_group, Tier
from .pro_client import ProClient

try:
    from xiaoning_runtime import ArtifactDeliveryResult, deliver_local_artifact
except ImportError:
    try:
        from data.plugins.xiaoning_runtime import ArtifactDeliveryResult, deliver_local_artifact
    except ImportError:
        # AstrBot's isolated plugin loader may expose only this plugin package.
        import importlib.util
        import sys

        _runtime_path = Path(__file__).resolve().parents[1] / "xiaoning_runtime.py"
        _runtime_spec = importlib.util.spec_from_file_location(
            "xiaoning_shared_runtime", _runtime_path
        )
        if _runtime_spec is None or _runtime_spec.loader is None:
            raise
        _runtime = importlib.util.module_from_spec(_runtime_spec)
        sys.modules.setdefault(_runtime_spec.name, _runtime)
        _runtime_spec.loader.exec_module(_runtime)
        ArtifactDeliveryResult = _runtime.ArtifactDeliveryResult
        deliver_local_artifact = _runtime.deliver_local_artifact


DRAW_PROXY_URL = "http://127.0.0.1:3000/v1/images/generations"
EDIT_PROXY_URL = "http://127.0.0.1:3000/v1/images/edits"
MAX_IMAGE_BYTES = 20 * 1024 * 1024  # Imagen 3 output can be larger
MAX_IMAGE_EDGE = 4096
DRAW_PRO_DAILY = 10
DRAW_X_WEEKLY = 6
DRAW_ORDINARY_DAILY = 1
DRAW_LIMIT_MSG = "作图次数已用完（今日 {used}/{limit}）。明天自动重置。"
DRAW_WEEKLY_LIMIT_MSG = "本周作图已用 {used}/{limit} 次。下周自动重置。"
DRAW_ORDINARY_LIMIT_MSG = "作图次数已用完（今日 {used}/{limit}）。添加小柠为QQ好友获得X资格可享每周6次。"
# PRO tier: Vertex Imagen 3 — dedicated high-quality image model
PRO_IMAGE_MODEL = "gemini-3-pro-image"       # X/Pro — Gemini 3 Pro Image (best available)
X_IMAGE_MODEL  = "gemini-3.1-flash-image"    # 普通用户 — fast, good quality
DRAW_MEMORY = (
    "【作图能力】所有用户都可使用 /draw 或自然语言作图。普通用户每天 1 次；X资格每周 6 次；Pro 每天 10 次+定制图 1次/天。"
    "支持画幅控制：在描述后加 --9:16（竖屏）--16:9（横屏）--1:1（方形）。"
    "多图生成：描述后加 --2 或 --3 一次出多张。"
    "图片编辑：回复图片说「把这张图改成xxx」或使用 /edit 命令。"
    "去水印：回复图片说「去水印」或「把右下角的@画师小尾巴抹掉」，可去除水印、字幕、Logo。"
    "只在用户明确要求画图或编辑图片时触发，不承诺生成失败的结果。"
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
        self._usage_file = self._output_root.parent / "state" / "draw_usage.json"
        self._daily_usage = self._load_usage()

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

    def _draw_rate_limit(self, tier: Tier, sender_id: str, in_pro_group: bool) -> tuple[int, str, int]:
        """Return (limit, cache_key, current_usage) for the given tier."""
        today = time.strftime("%Y%m%d")
        dk = f"{sender_id}:{today}"
        if tier >= Tier.PRO or in_pro_group:
            return DRAW_PRO_DAILY, dk, self._daily_usage.get(dk, 0)
        if tier >= Tier.X:
            year, week_num = time.strftime("%Y"), time.strftime("%W")
            wk = f"{sender_id}:{year}:{week_num}"
            return DRAW_X_WEEKLY, wk, self._daily_usage.get(wk, 0)
        return DRAW_ORDINARY_DAILY, dk, self._daily_usage.get(dk, 0)

    def _load_usage(self) -> dict[str, int]:
        import json
        try:
            raw = json.loads(self._usage_file.read_text(encoding="utf-8"))
            today = time.strftime("%Y%m%d")
            year, week = time.strftime("%Y"), time.strftime("%W")
            # Keep today's daily + this week's weekly keys
            return {
                str(k): int(v) for k, v in raw.items()
                if (str(k).endswith(f":{today}") or str(k).endswith(f":{year}:{week}")) and int(v) >= 0
            }
        except Exception:
            return {}

    def _save_usage(self) -> None:
        import json
        try:
            self._usage_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._usage_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._daily_usage, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._usage_file)
        except OSError:
            pass

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

    def _request_image(self, prompt: str, aspect: str = "1:1", model: str = "") -> bytes:
        size_map = {"1:1": "1024x1024", "16:9": "1024x576", "9:16": "576x1024", "2:3": "1024x1536", "3:2": "1536x1024"}
        size = size_map.get(aspect, "1024x1024")
        chosen_model = model or X_IMAGE_MODEL
        # Gemini image models benefit from quality guidance
        prompt = f"{prompt}, masterpiece, highly detailed, professional quality, sharp focus"
        response = requests.post(
            DRAW_PROXY_URL,
            json={"prompt": prompt, "model": chosen_model, "size": size},
            timeout=(30, 240),
        )
        response.raise_for_status()
        return self._decode_proxy_image(response)

    def _request_edit(self, image_b64: str, prompt: str) -> bytes:
        response = requests.post(
            EDIT_PROXY_URL,
            json={"image": image_b64, "prompt": prompt, "model": "gemini-3.1-flash-image"},
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

    async def _deliver_image(
        self, event: AstrMessageEvent, path: Path
    ) -> ArtifactDeliveryResult:
        return await deliver_local_artifact(
            event, path, allowed_roots=[self._output_root], kind="image"
        )

    @staticmethod
    def _get_referenced_image_base64(event: AstrMessageEvent) -> str | None:
        """Extract base64 image from the current message or its reply target.
        Handles base64://, file://, and raw file paths by converting to base64."""
        import base64 as _b64

        def _extract_from_message(msg_obj) -> str | None:
            message = getattr(msg_obj, "message", None) or []
            for seg in (message if isinstance(message, list) else [message]):
                seg_type = str(getattr(seg, "type", "") or "")
                # Check Image component
                if "image" in seg_type.lower() or "Image" in str(type(seg).__name__):
                    # Try file attribute first (NapCat passes file:// paths)
                    file_path = getattr(seg, "file", "") or ""
                    if file_path:
                        cleaned = file_path
                        if cleaned.startswith("file:///"):
                            from urllib.parse import unquote
                            cleaned = unquote(cleaned[len("file://"):])
                            if cleaned.startswith("/") and len(cleaned) > 3 and cleaned[2] == ":":
                                cleaned = cleaned[1:]  # /D:/... → D:/...
                        elif cleaned.startswith("file://"):
                            cleaned = cleaned[len("file://"):]
                        p = Path(cleaned)
                        if p.is_file():
                            return _b64.b64encode(p.read_bytes()).decode("ascii")
                    # Try url attribute
                    url = getattr(seg, "url", "") or ""
                    if url.startswith("base64://"):
                        return url[len("base64://"):]
                # Check segment data dictionaries
                for field in ("data", "image_url", "image"):
                    data = getattr(seg, field, None)
                    if isinstance(data, dict):
                        url = str(data.get("url", "") or data.get("file", "") or "")
                        if url.startswith("base64://"):
                            return url[len("base64://"):]
                        if url.startswith("file://"):
                            from urllib.parse import unquote
                            cleaned = unquote(url[len("file://"):])
                            if cleaned.startswith("/") and len(cleaned) > 3 and cleaned[2] == ":":
                                cleaned = cleaned[1:]
                            p = Path(cleaned)
                            if p.is_file():
                                return _b64.b64encode(p.read_bytes()).decode("ascii")
            return None

        # Check current message
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj is not None:
            result = _extract_from_message(msg_obj)
            if result:
                return result

        # Check replied message
        reply_obj = getattr(event, "get_reply_obj", None)
        if not callable(reply_obj):
            return None
        try:
            reply = reply_obj()
        except Exception:
            return None
        if reply is None:
            return None
        return _extract_from_message(reply)

    @filter.on_llm_request(priority=-19)
    async def inject_draw_memory(self, event: AstrMessageEvent, req) -> None:
        system_prompt = str(getattr(req, "system_prompt", "") or "")
        if "【作图能力】" not in system_prompt:
            req.system_prompt = f"{system_prompt}\n\n{DRAW_MEMORY}".strip()

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=940)
    async def on_message(self, event: AstrMessageEvent):
        text = self._message_text(event)
        sender_id = self._safe_sender_id(event)

        # ── Image Editing ──────────────────────────────────────────
        edit_prompt = parse_edit_command(text)
        if edit_prompt is not None and self._is_allowed_context(event):
            ref_img = self._get_referenced_image_base64(event)
            if not ref_img:
                return  # pass through to normal chat — no image to edit
            group_id = str(getattr(event, "get_group_id", lambda: "")() or "")
            in_pro_group = bool(group_id) and is_active_pro_group(group_id, self._pro_db_path)
            tier = get_tier(sender_id, self._pro_db_path)
            today = time.strftime("%Y%m%d")
            dk = f"{sender_id}:{today}"
            limit, key, used = self._draw_rate_limit(tier, sender_id, in_pro_group)
            is_weekly = (tier >= Tier.X and tier < Tier.PRO and not in_pro_group)
            if used >= limit:
                msg = DRAW_WEEKLY_LIMIT_MSG if is_weekly else (
                    DRAW_ORDINARY_LIMIT_MSG if tier < Tier.X else DRAW_LIMIT_MSG
                )
                yield event.plain_result(msg.format(used=used, limit=limit))
                event.stop_event()
                return
            if self._generation_lock.locked():
                yield event.plain_result("我正在处理一张图，等这张发出后再试。")
                event.stop_event()
                return
            retry_after = self._rate_limiter.try_acquire(sender_id)
            if retry_after:
                yield event.plain_result(f"作图冷却中，{retry_after} 秒后再试。")
                event.stop_event()
                return
            yield event.plain_result("正在编辑图片，预计 30–90 秒。")
            try:
                async with self._generation_lock:
                    payload = await asyncio.to_thread(self._request_edit, ref_img, edit_prompt)
                    output_path = self._save_sanitized_image(payload)
            except Exception as exc:
                logger.warning("[ProDraw] edit failed: %s", type(exc).__name__)
                yield event.plain_result("图片编辑失败，请尝试换一种描述或更清晰的图片。")
                event.stop_event()
                return
            self._daily_usage[key] = used + 1
            delivery = await self._deliver_image(event, output_path)
            if delivery.delivered:
                event.set_extra("_pro_draw_output_paths", [str(output_path)])
                yield event.plain_result(f"图片编辑任务已完成，文件已交付：{output_path.name}")
            else:
                self._daily_usage[key] = used
                self._save_usage()
                retry_note = (
                    "已加入后台重试队列，稍后自动送达。"
                    if delivery.channel == "queued"
                    else "文件已安全保留，请稍后重试。"
                )
                yield event.plain_result(
                    f"图片已处理，但 QQ 文件尚未交付，任务未完成；{retry_note}本次额度不计。"
                )
            event.stop_event()
            return

        # ── Watermark Removal ──────────────────────────────────────
        if is_dewatermark_request(text):
            if not self._is_allowed_context(event):
                return
            ref_img = self._get_referenced_image_base64(event)
            if not ref_img:
                yield event.plain_result("需要原图才能处理：请回复那张图片，再说「去水印」或描述要抹掉的位置。")
                event.stop_event()
                return
            if self._generation_lock.locked():
                yield event.plain_result("我正在处理一张图，等这张发出后再试。")
                event.stop_event()
                return
            retry_after = self._rate_limiter.try_acquire(sender_id)
            if retry_after:
                yield event.plain_result(f"冷却中，{retry_after} 秒后再试。")
                event.stop_event()
                return
            yield event.plain_result("去水印任务已开始，预计 30–90 秒；QQ 文件成功交付后才会标记完成。")
            try:
                async with self._generation_lock:
                    payload = await asyncio.to_thread(self._request_edit, ref_img, _DEWATERMARK_PROMPT)
                    output_path = self._save_sanitized_image(payload)
            except Exception:
                logger.warning("[Draw] dewatermark failed")
                yield event.plain_result("去水印失败，请尝试换一张更清晰的图片。")
                event.stop_event()
                return
            delivery = await self._deliver_image(event, output_path)
            if delivery.delivered:
                yield event.plain_result(f"去水印任务已完成，文件已交付：{output_path.name}")
            else:
                retry_note = (
                    "已加入后台重试队列，稍后自动送达。"
                    if delivery.channel == "queued"
                    else "文件已安全保留，请稍后重试。"
                )
                yield event.plain_result(f"图片已处理，但 QQ 文件尚未交付，任务未完成；{retry_note}")
            event.stop_event()
            return

        # ── Image Generation ───────────────────────────────────────
        try:
            prompt = parse_draw_command(text)
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
        # Tiered drawing limits: ORDINARY=1/day, X=6/week, PRO=10/day
        limit, key, used = self._draw_rate_limit(tier, sender_id, in_pro_group)
        is_weekly = (tier >= Tier.X and tier < Tier.PRO and not in_pro_group)
        if used >= limit:
            msg = DRAW_WEEKLY_LIMIT_MSG if is_weekly else (
                DRAW_ORDINARY_LIMIT_MSG if tier < Tier.X else DRAW_LIMIT_MSG
            )
            yield event.plain_result(msg.format(used=used, limit=limit))
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

        # X/PRO→Gemini 3 Pro Image (high-quality); ordinary→Gemini Flash
        is_pro_quality = tier >= Tier.X or in_pro_group
        draw_model = PRO_IMAGE_MODEL if is_pro_quality else X_IMAGE_MODEL

        prompt, aspect, n_images = parse_draw_options(prompt)
        n_label = f" x{n_images}" if n_images > 1 else ""
        quality_tag = "（Imagen 3）" if is_pro_quality else ""
        yield event.plain_result(f"我开始画了{n_label}{quality_tag}，预计 30–120 秒。")
        try:
            async with self._generation_lock:
                output_paths: list[Path] = []
                for i in range(n_images):
                    payload = await asyncio.to_thread(
                        self._request_image, prompt, aspect, draw_model
                    )
                    path = self._save_sanitized_image(payload)
                    output_paths.append(path)
                output_path = output_paths[0]
        except Exception as exc:
            logger.warning("[ProDraw] generation failed: %s", type(exc).__name__)
            yield event.plain_result("这次没能画出来，稍后再试。")
            event.stop_event()
            return

        self._daily_usage[key] = used + 1
        self._save_usage()
        # Deliver all images
        delivered_count = 0
        last_ok_channel = ""
        delivered_paths: list[str] = []
        queued = False
        for idx, path in enumerate(output_paths):
            delivery = await self._deliver_image(event, path)
            if delivery.delivered:
                delivered_count += 1
                last_ok_channel = delivery.channel
                delivered_paths.append(str(path))
            elif delivery.channel == "queued":
                queued = True
        if delivered_count == n_images:
            # Only files proved delivered may be cleaned after AstrBot sends
            # the confirmation.  Queued/retained artifacts must remain for a
            # later QQ retry or recovery.
            event.set_extra("_pro_draw_output_paths", delivered_paths)
            suffix = {
                "group_upload": "已上传到群文件",
                "private_fallback": "群文件上传失败，已私聊发送给你",
                "group_component": "群文件和私聊投递失败，已改为在群内发送",
                "private": "已发送到当前私聊",
                "private_component": "已发送到当前私聊",
            }.get(last_ok_channel, "已发送")
            label = f"{delivered_count} 张图片已生成" if n_images > 1 else "图片已生成"
            yield event.plain_result(f"作图任务已完成，{label}，{suffix}")
        elif delivered_count > 0:
            self._daily_usage[key] = used
            self._save_usage()
            retry_note = "已加入后台重试队列，稍后自动送达。" if queued else "文件已安全保留，请稍后重试。"
            yield event.plain_result(
                f"已交付 {delivered_count}/{n_images} 张图片，剩余文件尚未交付，任务未完成；{retry_note}本次额度不计。"
            )
        else:
            self._daily_usage[key] = used
            self._save_usage()
            retry_note = "已加入后台重试队列，稍后自动送达。" if queued else "文件已安全保留，请稍后重试。"
            yield event.plain_result(
                f"图片已生成，但 QQ 文件尚未交付，任务未完成；{retry_note}本次额度不计。"
            )
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
