"""QQ drawing command with a shared weekly limit for ordinary and X users."""

from __future__ import annotations

import asyncio
import base64
import io
import re
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

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
)
from .pro_access import get_tier, is_active_pro_group, Tier
from .pro_client import ProClient
from .edit_sessions import ImageEditSessionStore
try:
    from xiaoning_core.ownership import route_allows
except ImportError:
    try:
        from data.plugins.xiaoning_core.ownership import route_allows
    except ImportError:
        def route_allows(_event, _owner):
            return True

try:
    from xiaoning_runtime import (
        ArtifactDeliveryResult,
        deliver_local_artifact,
        mirror_runtime_task_status,
    )
except ImportError:
    try:
        from data.plugins.xiaoning_runtime import (
            ArtifactDeliveryResult,
            deliver_local_artifact,
            mirror_runtime_task_status,
        )
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
        mirror_runtime_task_status = _runtime.mirror_runtime_task_status


DRAW_PROXY_URL = "http://127.0.0.1:3000/v1/images/generations"
EDIT_PROXY_URL = "http://127.0.0.1:3000/v1/images/edits"
EDIT_MODEL = "gemini-3.1-flash-image"  # edits use flash tier: faster, verified on Vertex
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_EDGE = 4096
QQ_IMAGE_HOSTS = frozenset(
    {
        "multimedia.nt.qq.com.cn",
        "gchat.qpic.cn",
        "c2cpicdw.qpic.cn",
    }
)
DRAW_DAILY = 3
DRAW_LIMIT_MSG = "作图次数已用完（今日 {used}/{limit}）。明天自动重置。"
# All users get the same best-quality model
DRAW_MODEL = "gemini-3-pro-image"
_EDIT_CONFIRM_RE = re.compile(
    r"^(?:需要|要|是|是的|对|对的|好|好的|可以|确认|确定|开始吧|处理吧|这张|就这张|"
    r"(?:现在)?(?:修|弄|处理|去|抹)好了?(?:吗|没|没有)?|"
    r"还没(?:修|弄|处理|去)好(?:吗|嘛)?|怎么还没好|"
    r"(?:你)?(?:还)?没(?:发|传)(?:出来|给我|成功)?(?:吗|呀|啊)?|"
    r"(?:重新|再)(?:发|传|处理|弄)(?:一下|一次|出来)?|"
    r"继续(?:处理|弄)?)[。！!？?呀啊嘛呢\s]*$",
    re.I,
)
_EDIT_CANCEL_RE = re.compile(r"^(?:取消|算了|不用了|别弄了|停止)[。！!\s]*$", re.I)


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
        session_root = (
            project_root / "astrbot" / "data" / "plugin_data"
            / "draw_command" / "edit_sessions"
        )
        self._edit_sessions = ImageEditSessionStore(
            session_root,
            max_image_bytes=MAX_IMAGE_BYTES,
            max_image_edge=MAX_IMAGE_EDGE,
        )
        self._edit_sessions.cleanup()

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

    @classmethod
    def _edit_scope(cls, event: AstrMessageEvent) -> str:
        origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
        sender = cls._safe_sender_id(event)
        if origin:
            return f"{origin}:sender:{sender}"
        group = str(getattr(event, "get_group_id", lambda: "")() or "").strip()
        return f"group:{group}:{sender}" if group else f"private:{sender}"

    def _draw_rate_limit(self, tier: Tier, sender_id: str, in_pro_group: bool) -> tuple[int, str, int]:
        """Return (limit, cache_key, current_usage). Unified for all users."""
        today = time.strftime("%Y%m%d")
        dk = f"{sender_id}:{today}"
        return DRAW_DAILY, dk, self._daily_usage.get(dk, 0)

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

    def _request_image(self, prompt: str, aspect: str = "1:1", model: str = "",
                       image_size: str = "2K") -> bytes:
        size_map = {"1:1": "1024x1024", "16:9": "1024x576", "9:16": "576x1024", "2:3": "1024x1536", "3:2": "1536x1024"}
        size = size_map.get(aspect, "1024x1024")
        chosen_model = model or DRAW_MODEL
        prompt = f"{prompt}, masterpiece, highly detailed, professional quality, sharp focus"
        response = requests.post(
            DRAW_PROXY_URL,
            json={"prompt": prompt, "model": chosen_model, "size": size, "image_size": image_size},
            timeout=(30, 240),
        )
        response.raise_for_status()
        return self._decode_proxy_image(response)

    def _request_image_raw(self, body: dict) -> bytes:
        """Direct proxy call with full request body (supports reference images, 4K, etc.)."""
        if "prompt" in body:
            body = dict(body)
            body["prompt"] = f"{body['prompt']}, masterpiece, highly detailed, professional quality, sharp focus"
        response = requests.post(DRAW_PROXY_URL, json=body, timeout=(30, 240))
        response.raise_for_status()
        return self._decode_proxy_image(response)

    def _request_edit(self, image_b64: str, prompt: str) -> bytes:
        response = requests.post(
            EDIT_PROXY_URL,
            json={"image": image_b64, "prompt": prompt, "model": EDIT_MODEL},
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
        self,
        event: AstrMessageEvent,
        path: Path,
        *,
        task_id: str = "",
        task_desc: str = "",
    ) -> ArtifactDeliveryResult:
        return await deliver_local_artifact(
            event,
            path,
            allowed_roots=[self._output_root],
            kind="image",
            task_id=task_id,
            task_desc=task_desc,
            task_owner="draw" if task_id else "",
        )

    @staticmethod
    def _get_referenced_image_base64(event: AstrMessageEvent) -> str | None:
        """Extract base64 image from the current message or its reply target.
        Handles base64://, local files, and trusted QQ CDN URLs."""
        import base64 as _b64

        trusted_temp = (Path(__file__).resolve().parents[2] / "temp").resolve()

        def _encode_local_image(value: str) -> str | None:
            try:
                path = Path(value).resolve(strict=True)
                path.relative_to(trusted_temp)
                if path.stat().st_size > MAX_IMAGE_BYTES:
                    return None
                payload = path.read_bytes()
                with PillowImage.open(io.BytesIO(payload)) as source:
                    if source.width < 1 or source.height < 1:
                        return None
                    if source.width > MAX_IMAGE_EDGE * 2 or source.height > MAX_IMAGE_EDGE * 2:
                        return None
                    source.verify()
                return _b64.b64encode(payload).decode("ascii")
            except (OSError, ValueError):
                return None

        def _download_qq_image(url: str) -> str | None:
            parsed = urlparse(str(url or "").strip())
            if parsed.scheme != "https" or (parsed.hostname or "").lower() not in QQ_IMAGE_HOSTS:
                return None
            try:
                response = requests.get(url, timeout=(10, 30))
                response.raise_for_status()
                payload = response.content
                if not payload or len(payload) > MAX_IMAGE_BYTES:
                    return None
                with PillowImage.open(io.BytesIO(payload)) as source:
                    if source.width < 1 or source.height < 1:
                        return None
                    if source.width > MAX_IMAGE_EDGE * 2 or source.height > MAX_IMAGE_EDGE * 2:
                        return None
                    source.verify()
                return _b64.b64encode(payload).decode("ascii")
            except Exception as exc:
                logger.warning("[ProDraw] QQ reference image download failed: %s", type(exc).__name__)
                return None

        def _extract_from_message(msg_obj) -> str | None:
            if isinstance(msg_obj, (list, tuple)):
                message = msg_obj
            elif isinstance(msg_obj, dict):
                raw = msg_obj.get("raw_message")
                message = msg_obj.get("message") or msg_obj.get("message_chain")
                if message is None and isinstance(raw, dict):
                    message = raw.get("message")
                message = message or []
            else:
                raw = getattr(msg_obj, "raw_message", None)
                message = getattr(msg_obj, "message", None) or getattr(msg_obj, "message_chain", None)
                if message is None and isinstance(raw, dict):
                    message = raw.get("message")
                message = message or []
            for seg in (message if isinstance(message, list) else [message]):
                seg_data = seg.get("data") if isinstance(seg, dict) else getattr(seg, "data", None)
                if not isinstance(seg_data, dict):
                    seg_data = {}
                seg_type = str((seg.get("type") if isinstance(seg, dict) else getattr(seg, "type", "")) or "")
                # Check Image component
                if "image" in seg_type.lower() or "Image" in str(type(seg).__name__):
                    # Try file attribute first (NapCat passes file:// paths)
                    file_path = str(
                        seg_data.get("file")
                        or (getattr(seg, "file", "") if not isinstance(seg, dict) else "")
                        or ""
                    )
                    if file_path:
                        cleaned = file_path
                        if cleaned.startswith("file:///"):
                            cleaned = unquote(cleaned[len("file://"):])
                            if cleaned.startswith("/") and len(cleaned) > 3 and cleaned[2] == ":":
                                cleaned = cleaned[1:]  # /D:/... → D:/...
                        elif cleaned.startswith("file://"):
                            cleaned = cleaned[len("file://"):]
                        p = Path(cleaned)
                        encoded = _encode_local_image(str(p))
                        if encoded:
                            return encoded
                    # Try url attribute
                    url = str(
                        seg_data.get("url")
                        or seg_data.get("file")
                        or (getattr(seg, "url", "") if not isinstance(seg, dict) else "")
                        or ""
                    )
                    if url.startswith("base64://"):
                        return url[len("base64://"):]
                    downloaded = _download_qq_image(url)
                    if downloaded:
                        return downloaded
                # Check segment data dictionaries
                for field in ("data", "image_url", "image"):
                    data = seg.get(field) if isinstance(seg, dict) else getattr(seg, field, None)
                    if isinstance(data, dict):
                        url = str(data.get("url", "") or data.get("file", "") or "")
                        if url.startswith("base64://"):
                            return url[len("base64://"):]
                        if url.startswith("file://"):
                            cleaned = unquote(url[len("file://"):])
                            if cleaned.startswith("/") and len(cleaned) > 3 and cleaned[2] == ":":
                                cleaned = cleaned[1:]
                            p = Path(cleaned)
                            encoded = _encode_local_image(str(p))
                            if encoded:
                                return encoded
                        downloaded = _download_qq_image(url)
                        if downloaded:
                            return downloaded
            return None

        # Check current message
        get_messages = getattr(event, "get_messages", None)
        if callable(get_messages):
            result = _extract_from_message(get_messages())
            if result:
                return result
        msg_chain = getattr(event, "message_chain", None)
        if msg_chain is not None:
            result = _extract_from_message(msg_chain)
            if result:
                return result
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

    # Image edits own their explicit intent before generic vision/search plugins.
    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=986)
    async def on_message(self, event: AstrMessageEvent):
        if not route_allows(event, "draw_command"):
            return
        text = self._message_text(event)
        sender_id = self._safe_sender_id(event)

        # ── Image Editing (image and intent may arrive in adjacent turns) ──
        if self._is_allowed_context(event):
            scope = self._edit_scope(event)
            if _EDIT_CANCEL_RE.fullmatch(text.strip()) and any(
                vars(self._edit_sessions.get(scope)).values()
            ):
                self._edit_sessions.clear(scope)
                yield event.plain_result("这次图片处理已取消，临时图片也已清除。")
                event.stop_event()
                return

            ref_img = self._get_referenced_image_base64(event)
            if ref_img:
                try:
                    self._edit_sessions.remember_image(scope, ref_img)
                except (OSError, ValueError):
                    ref_img = None

            session = self._edit_sessions.get(scope)
            edit_prompt = parse_edit_command(text)
            edit_kind = None
            if edit_prompt is not None:
                edit_kind = "edit"
            elif session.intent_kind == "edit" and _EDIT_CONFIRM_RE.fullmatch(text.strip()):
                # intent_kind whitelist: stale sessions from the removed
                # dewatermark feature (TTL window) must never reach
                # remember_intent, which raises on any non-"edit" kind
                edit_kind = session.intent_kind
                edit_prompt = session.intent_prompt
            elif (
                ref_img
                and session.intent_kind == "edit"
                and text.strip() in {"", "[图片]", "图片", "这张"}
            ):
                edit_kind = session.intent_kind
                edit_prompt = session.intent_prompt

            if edit_kind:
                self._edit_sessions.remember_intent(scope, edit_kind, edit_prompt or "")
                session = self._edit_sessions.get(scope)
                ref_img = ref_img or session.image_b64

        else:
            edit_kind = None
            edit_prompt = None
            ref_img = None
            scope = ""

        if edit_kind == "edit":
            if not ref_img:
                yield event.plain_result("已记住修改要求。请把原图发来，或回复那张图；收到图片后才会开始处理。")
                event.stop_event()
                return
            group_id = str(getattr(event, "get_group_id", lambda: "")() or "")
            in_pro_group = bool(group_id) and is_active_pro_group(group_id, self._pro_db_path)
            tier = get_tier(sender_id, self._pro_db_path)
            today = time.strftime("%Y%m%d")
            dk = f"{sender_id}:{today}"
            limit, key, used = self._draw_rate_limit(tier, sender_id, in_pro_group)
            is_weekly = False
            if used >= limit:
                yield event.plain_result(DRAW_LIMIT_MSG.format(used=used, limit=limit))
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
            task_id = uuid.uuid4().hex[:12]
            task_desc = f"编辑图片：{edit_prompt[:140]}"
            await mirror_runtime_task_status(
                sender_id, task_id, task_desc, "in_progress", "edit_started", owner="draw"
            )
            yield event.plain_result("正在编辑图片，预计 30–90 秒。")
            edit_ok = False
            try:
                async with self._generation_lock:
                    payload = await asyncio.to_thread(self._request_edit, ref_img, edit_prompt)
                    output_path = self._save_sanitized_image(payload)
                edit_ok = True
            except Exception as exc:
                logger.warning("[ProDraw] edit failed: %s", type(exc).__name__)
                await mirror_runtime_task_status(
                    sender_id, task_id, task_desc, "failed", type(exc).__name__, owner="draw"
                )
                yield event.plain_result("图片编辑没成功，换个方式帮你。")
            if edit_ok:
                delivery = await self._deliver_image(
                    event, output_path, task_id=task_id, task_desc=task_desc
                )
                if delivery.delivered:
                    await mirror_runtime_task_status(
                        sender_id, task_id, task_desc, "done", f"qq:{delivery.channel}", owner="draw"
                    )
                    self._edit_sessions.clear(scope)
                    self._daily_usage[key] = used + 1
                    self._save_usage()
                    event.set_extra("_pro_draw_output_paths", [str(output_path)])
                    yield event.plain_result(f"图片编辑任务已完成，文件已交付：{output_path.name}")
                else:
                    await mirror_runtime_task_status(
                        sender_id, task_id, task_desc, "delivery_pending", delivery.channel, owner="draw"
                    )
                    self._edit_sessions.clear(scope)
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
        # Unified drawing limits for all users
        limit, key, used = self._draw_rate_limit(tier, sender_id, in_pro_group)
        if used >= limit:
            yield event.plain_result(DRAW_LIMIT_MSG.format(used=used, limit=limit))
            event.stop_event()
            return
        retry_after = self._rate_limiter.try_acquire(sender_id)
        if retry_after:
            yield event.plain_result(f"作图冷却中，{retry_after} 秒后再试。")
            event.stop_event()
            return

        # All users get best quality model
        draw_model = DRAW_MODEL

        prompt, aspect, n_images, is_4k, style_prefix = parse_draw_options(prompt)

        # 4K available to all users, costs 2× quota; check BEFORE proxy so a
        # generated 4K image is never discarded for insufficient quota
        draw_cost = 2 if is_4k else 1
        if used + draw_cost > limit:
            event.stop_event()
            yield event.plain_result(
                f"今日额度只剩 {limit - used} 次，画 4K 需要 {draw_cost} 次，先画 2K 或明天再试。"
            )
            return
        image_size = "2K"
        if is_4k:
            image_size = "4K"
            n_images = 1  # 4K only single image

        # P2: style preset
        if style_prefix:
            prompt = style_prefix + prompt

        # P1: reference image — detect if this is a reply to an image
        has_ref = False
        ref_image_b64 = None
        attachment_url = getattr(event, "get_attachment_url", lambda: None) or None
        if callable(attachment_url) and attachment_url():
            has_ref = True
            try:
                ref_resp = requests.get(attachment_url(), timeout=15)
                ref_resp.raise_for_status()
                ref_image_b64 = base64.b64encode(ref_resp.content).decode("ascii")
            except Exception:
                pass  # proceed without reference if download fails

        # P2: parallel batch generation (no global lock)
        task_id = uuid.uuid4().hex[:12]
        task_desc = f"生成图片：{prompt[:140]}"
        await mirror_runtime_task_status(
            sender_id, task_id, task_desc, "in_progress", "draw_started", owner="draw"
        )
        n_label = f" x{n_images}" if n_images > 1 else ""
        k4_label = " 4K" if is_4k else ""
        ref_label = " (参考图)" if has_ref else ""
        style_label = f" ({style_prefix.split('—')[0].strip()})" if style_prefix else ""
        quality_tag = "（Imagen 3）"
        yield event.plain_result(f"我开始画了{n_label}{k4_label}{ref_label}{style_label}{quality_tag}，预计 30–120 秒。")

        try:
            # P2: parallel batch — generate all images concurrently
            async def _gen_one(prompt_text: str, aspect_ratio: str, model: str, isz: str):
                if not ref_image_b64:
                    return await asyncio.to_thread(
                        self._request_image, prompt_text, aspect_ratio, model, isz
                    )
                body = {
                    "prompt": prompt_text,
                    "model": model,
                    "size": f"{aspect_ratio.replace(':','x')}",
                    "image_size": isz,
                    "reference_image": ref_image_b64,
                }
                return await asyncio.to_thread(self._request_image_raw, body)

            tasks = [_gen_one(prompt, aspect, draw_model, image_size) for _ in range(n_images)]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            output_paths: list[Path] = []
            for r in results:
                if isinstance(r, Exception):
                    raise r
                output_paths.append(self._save_sanitized_image(r))
            output_path = output_paths[0]
        except Exception as exc:
            logger.warning("[ProDraw] generation failed: %s", type(exc).__name__)
            await mirror_runtime_task_status(
                sender_id, task_id, task_desc, "failed", type(exc).__name__, owner="draw"
            )
            yield event.plain_result("画图没成功，我换个方式帮你。")
            # ponytail: don't stop the event — let Agent pick it up as fallback
            return

        # 4K costs 2× normal quota; clamp the stored count so the limit
        # message never shows "4/3" (pre-check already rejected unaffordable 4K)
        self._daily_usage[key] = min(used + draw_cost, limit)
        self._save_usage()
        # Deliver all images
        delivered_count = 0
        last_ok_channel = ""
        delivered_paths: list[str] = []
        queued = False
        for idx, path in enumerate(output_paths):
            delivery = await self._deliver_image(
                event, path, task_id=task_id, task_desc=task_desc
            )
            if delivery.delivered:
                delivered_count += 1
                last_ok_channel = delivery.channel
                delivered_paths.append(str(path))
            elif delivery.channel == "queued":
                queued = True
        if delivered_count == n_images:
            await mirror_runtime_task_status(
                sender_id,
                task_id,
                task_desc,
                "done",
                f"qq_delivery_confirmed:{delivered_count}",
                owner="draw",
            )
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
            await mirror_runtime_task_status(
                sender_id,
                task_id,
                task_desc,
                "delivery_pending",
                f"qq_partial:{delivered_count}/{n_images}",
                owner="draw",
            )
            self._daily_usage[key] = used
            self._save_usage()
            retry_note = "已加入后台重试队列，稍后自动送达。" if queued else "文件已安全保留，请稍后重试。"
            yield event.plain_result(
                f"已交付 {delivered_count}/{n_images} 张图片，剩余文件尚未交付，任务未完成；{retry_note}本次额度不计。"
            )
        else:
            await mirror_runtime_task_status(
                sender_id,
                task_id,
                task_desc,
                "delivery_pending",
                "qq_delivery_unconfirmed",
                owner="draw",
            )
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
