"""Small runtime helpers shared by Xiaoning command plugins."""

import asyncio
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from functools import wraps
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any
import uuid
import zipfile

from astrbot.api import logger
from astrbot.api.message_components import File, Image
from astrbot.core.message.message_event_result import MessageChain


# Private data must never be keyed by a bare identifier from two transports.
# Keep existing QQ document ids untouched, and namespace only the native
# personal-WeChat adapter.  Unknown platforms deliberately get no durable
# identity until they receive an explicit compatibility review.
_QQ_PRIVATE_ID_RE = re.compile(r"^[1-9]\d{4,11}$")
_WEIXIN_PRIVATE_ID_RE = re.compile(r"^[^/\x00-\x1f]{1,128}$")
_WEIXIN_PRIVATE_PREFIX = "weixin_oc:"


def event_platform_name(event: Any) -> str:
    """Return the normalized AstrBot platform name without trusting a raw ID."""
    meta = getattr(event, "platform_meta", None)
    name = getattr(meta, "name", "")
    if not name and isinstance(meta, dict):
        name = meta.get("name", "")
    if not name:
        platform = getattr(event, "platform", None)
        getter = getattr(platform, "meta", None)
        if callable(getter):
            try:
                name = getattr(getter(), "name", "")
            except Exception:
                name = ""
    return str(name or "").strip().lower()


def is_weixin_private(event: Any) -> bool:
    """True only for the native personal-WeChat private-chat adapter."""
    if event_platform_name(event) != "weixin_oc":
        return False
    private_getter = getattr(event, "is_private_chat", None)
    try:
        return bool(private_getter()) if callable(private_getter) else False
    except Exception:
        return False


def private_user_key(event: Any) -> str:
    """Build the durable private-user key for QQ or native personal WeChat.

    The raw WeChat sender id remains available on the event for replying.  This
    key is only for memory, task, commitment and relationship storage.
    """
    sender_getter = getattr(event, "get_sender_id", None)
    try:
        sender = str(sender_getter() if callable(sender_getter) else "").strip()
    except Exception:
        return ""
    if is_weixin_private(event):
        return _WEIXIN_PRIVATE_PREFIX + sender if _WEIXIN_PRIVATE_ID_RE.fullmatch(sender) else ""
    return sender if _QQ_PRIVATE_ID_RE.fullmatch(sender) else ""


def is_private_user_key(value: object) -> bool:
    """Validate a storage key created by :func:`private_user_key`."""
    key = str(value or "").strip()
    if _QQ_PRIVATE_ID_RE.fullmatch(key):
        return True
    if not key.startswith(_WEIXIN_PRIVATE_PREFIX):
        return False
    return bool(_WEIXIN_PRIVATE_ID_RE.fullmatch(key[len(_WEIXIN_PRIVATE_PREFIX):]))


async def mirror_runtime_task_status(
    qq_id: str,
    task_id: str,
    task_desc: str,
    status: str,
    evidence: str = "",
    *,
    owner: str = "runtime",
) -> None:
    """Best-effort bridge from real plugin execution to cross-dialog tasks."""
    def sync_track() -> None:
        try:
            from astrbot_plugin_xiaoning_memory.main import track_runtime_task_status
        except ImportError:
            from data.plugins.astrbot_plugin_xiaoning_memory.main import track_runtime_task_status
        track_runtime_task_status(
            qq_id,
            task_id,
            task_desc,
            status,
            evidence,
            owner,
        )

    try:
        await asyncio.wait_for(asyncio.to_thread(sync_track), timeout=1.0)
    except Exception:
        logger.debug("[TaskMirror] status update unavailable")


@dataclass(frozen=True)
class ArtifactDeliveryResult:
    """Observable result of delivering one already-generated local artifact."""

    delivered: bool
    channel: str
    path: Path | None = None
    error: str = ""
    quality_code: str = "unchecked"
    manifest: Path | None = None


@dataclass(frozen=True)
class ArtifactQualityResult:
    allowed: bool
    code: str
    size: int = 0
    sha256: str = ""


MAX_ARTIFACT_BYTES = 50 * 1024 * 1024
_ZIP_ARTIFACTS = {".docx", ".xlsx", ".pptx", ".zip"}
_QQ_TEXT_ARTIFACTS = {".txt", ".md", ".csv"}
_MAGIC = {
    ".pdf": (b"%PDF-",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".wav": (b"RIFF",),
    ".mp3": (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"),
}


def _normalize_qq_text_artifact(path: Path) -> bool:
    """Make text attachments readable by both QQ and legacy Windows viewers.

    QQ transfers bytes unchanged, while common Windows editors may still guess
    UTF-8 without a BOM as GBK.  Normalize only text formats whose byte-level
    encoding is unambiguous and useful outside a browser.  The rewrite is
    atomic so a failed conversion never leaves a partial artifact behind.
    """
    if path.suffix.lower() not in _QQ_TEXT_ARTIFACTS:
        return False
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw.decode("utf-8-sig")
        return False
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("gb18030")

    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            handle.write(text)
        temporary.replace(path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return True


def inspect_local_artifact(path: Path) -> ArtifactQualityResult:
    """Cheap deterministic gate before an artifact is allowed near QQ."""
    size = path.stat().st_size
    if size <= 0:
        return ArtifactQualityResult(False, "empty", size)
    if size > MAX_ARTIFACT_BYTES:
        return ArtifactQualityResult(False, "too_large", size)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        head = handle.read(32)
        digest.update(head)
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    sha256 = digest.hexdigest()
    suffix = path.suffix.lower()
    expected = _MAGIC.get(suffix)
    if expected and not any(head.startswith(prefix) for prefix in expected):
        return ArtifactQualityResult(False, "format_magic", size, sha256)
    if suffix == ".mp4" and b"ftyp" not in head[:16]:
        return ArtifactQualityResult(False, "format_magic", size, sha256)
    if suffix in _ZIP_ARTIFACTS:
        try:
            with zipfile.ZipFile(path) as archive:
                if archive.testzip() is not None or not archive.namelist():
                    raise zipfile.BadZipFile
        except (OSError, ValueError, zipfile.BadZipFile):
            return ArtifactQualityResult(False, "archive_invalid", size, sha256)
    if suffix in {".txt", ".md", ".csv", ".html", ".htm"}:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return ArtifactQualityResult(False, "text_encoding", size, sha256)
        if not text.strip():
            return ArtifactQualityResult(False, "empty", size, sha256)
        if suffix in {".html", ".htm"} and "<html" not in text.lower():
            return ArtifactQualityResult(False, "html_invalid", size, sha256)
    return ArtifactQualityResult(True, "valid", size, sha256)


def _record_delivery_manifest(
    path: Path,
    quality: ArtifactQualityResult,
    *,
    kind: str,
    channel: str,
    delivered: bool,
    target_scope: str,
    error: str = "",
    root: Path | None = None,
) -> Path | None:
    """Persist non-sensitive delivery evidence without QQ ids or host paths."""
    directory = root or (
        Path(__file__).resolve().parents[1]
        / "plugin_data"
        / "xiaoning_artifacts"
    )
    trace_id = uuid.uuid4().hex
    payload = {
        "trace_id": trace_id,
        "created_at": int(time.time()),
        "file_name": path.name,
        "kind": kind,
        "size": quality.size,
        "sha256": quality.sha256,
        "quality_code": quality.code,
        "delivered": delivered,
        "channel": channel,
        "target_scope": target_scope,
        "error": str(error or "")[:80],
    }
    try:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{trace_id}.json"
        temporary = directory / f".{trace_id}.tmp"
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(target)
        return target
    except OSError as exc:
        logger.warning("[ArtifactDelivery] manifest write failed: %s", type(exc).__name__)
        return None


def _validated_artifact(path: object, allowed_roots: list[Path]) -> Path:
    candidate = Path(path)
    if candidate.is_symlink():
        raise ValueError("待发送内容不能是链接")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("待发送内容不是普通文件")

    roots: list[Path] = []
    for root in allowed_roots:
        try:
            roots.append(Path(root).resolve(strict=True))
        except OSError:
            continue
    if not roots or not any(resolved == root or root in resolved.parents for root in roots):
        raise ValueError("待发送内容不在允许目录内")
    return resolved


def _check_action_result(result: object, action: str) -> None:
    """Raise if a OneBot call_action returned a non-zero retcode (transport OK but QQ rejected)."""
    if isinstance(result, dict) and result.get("retcode", 0) != 0:
        raise RuntimeError(
            f"{action}: retcode={result.get('retcode')} "
            + str(result.get("wording", result.get("msg", "")))[:120]
        )


def _file_upload_path(fpath: str) -> str:
    """Return a native absolute path for NapCat's file-upload actions.

    NapCat accepts local Windows paths directly.  Passing a percent-encoded
    ``file://`` URI can make files under non-ASCII directories arrive corrupt.
    """
    return str(Path(fpath).resolve(strict=True))


async def deliver_local_artifact(
    event: Any,
    path: object,
    *,
    allowed_roots: list[Path],
    kind: str = "file",
    task_id: str = "",
    task_desc: str = "",
    task_owner: str = "",
) -> ArtifactDeliveryResult:
    """Deliver a verified artifact through the event's active transport.

    QQ first uses its native file-transfer actions because its inline media is
    subject to risk control.  Personal WeChat has no compatible retry queue:
    it sends the AstrBot File/Image component in the current private session,
    and a failure stays visibly retained rather than being misreported as a
    QQ retry.

    Strategy (group chat):
      1. upload_group_file (retry 3x, file-transfer protocol)
      2. upload_private_file to sender (retry 2x, file-transfer protocol)
      3. For images only: inline Image via send (last resort, may hit 风控)
      4. Text fallback telling user the file couldn't be delivered

    Strategy (private chat):
      1. upload_private_file (retry 3x, file-transfer protocol)  ← the fix
      2. For images only: inline Image via send (last resort)
      3. Otherwise retain and queue the artifact for a real file upload retry.
    """

    try:
        resolved = _validated_artifact(path, allowed_roots)
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("[Delivery] validation failed: %s", type(exc).__name__)
        return ArtifactDeliveryResult(False, "retained", error=type(exc).__name__)

    group_id = str(getattr(event, "get_group_id", lambda: "")() or "").strip()
    sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "").strip()
    target_scope = "group" if group_id.isdigit() else "private"
    weixin_private = is_weixin_private(event)
    try:
        normalized = False if weixin_private else _normalize_qq_text_artifact(resolved)
    except (OSError, UnicodeError) as exc:
        logger.warning(
            "[ArtifactDelivery] text normalization failed: %s",
            type(exc).__name__,
        )
        quality = ArtifactQualityResult(
            False, "text_encoding", resolved.stat().st_size
        )
    else:
        quality = inspect_local_artifact(resolved)
        if normalized:
            logger.info("[ArtifactDelivery] normalized text to UTF-8 BOM for QQ")

    def finish(
        delivered: bool, channel: str, error: str = ""
    ) -> ArtifactDeliveryResult:
        manifest = _record_delivery_manifest(
            resolved,
            quality,
            kind=kind,
            channel=channel,
            delivered=delivered,
            target_scope=target_scope,
            error=error,
        )
        # Individual WeChat jobs cannot use the QQ-only completion mirror in
        # their owning plugins (those plugins receive a raw non-numeric id).
        # Mirror the observable delivery result here, where the platform is
        # still known, so /任务 stays truthful for the same private user.
        if weixin_private and task_id:
            owner = str(task_owner or "runtime")[:24]
            user_key = private_user_key(event)
            if user_key:
                asyncio.create_task(
                    mirror_runtime_task_status(
                        user_key,
                        str(task_id),
                        str(task_desc),
                        "done" if delivered else "delivery_pending",
                        f"weixin:{channel}" if delivered else "",
                        owner=owner,
                    )
                )
        return ArtifactDeliveryResult(
            delivered, channel, resolved, error, quality.code, manifest
        )

    if not quality.allowed:
        logger.warning(
            "[ArtifactDelivery] quality gate rejected artifact: %s",
            quality.code,
        )
        return finish(False, "rejected", quality.code)

    bot = getattr(event, "bot", None)
    call_action = getattr(bot, "call_action", None)
    send = getattr(event, "send", None)
    is_image = kind == "image"
    fname = resolved.name
    fpath = str(resolved)
    upload_path = _file_upload_path(fpath)
    in_group = bool(group_id and group_id.isdigit())

    logger.info(
        "[Delivery] START scope=%s bot_ok=%s has_call=%s kind=%s",
        target_scope, bot is not None, callable(call_action), kind,
    )

    # ── Channel 1: Group file upload (group chats) ──────────────────
    # File-transfer protocol; bypasses the inline-media risk-control.
    if callable(call_action) and in_group:
        for attempt in range(3):
            try:
                result = await call_action(
                    "upload_group_file",
                    group_id=int(group_id),
                    file=upload_path,
                    name=fname,
                )
                _check_action_result(result, "upload_group_file")
                logger.info("[ArtifactDelivery] group_upload OK")
                return finish(True, "group_upload")
            except Exception as exc:
                logger.warning(
                    "[ArtifactDelivery] group_upload attempt %d/3 failed: %s",
                    attempt + 1, type(exc).__name__,
                )
                if attempt < 2:
                    await asyncio.sleep(1 * (2 ** attempt))

    # ── Channel 2: Private file upload (THE FIX) ────────────────────
    # upload_private_file uses the file-transfer protocol, NOT send_private_msg,
    # so it is not blocked by the account's inline-media risk-control.
    if callable(call_action) and sender_id and sender_id.isdigit():
        for attempt in range(3):
            try:
                result = await call_action(
                    "upload_private_file",
                    user_id=int(sender_id),
                    file=upload_path,
                    name=fname,
                )
                _check_action_result(result, "upload_private_file")
                channel = "private_fallback" if in_group else "private"
                logger.info("[ArtifactDelivery] %s (upload_private_file) OK", channel)
                return finish(True, channel)
            except Exception as exc:
                logger.warning(
                    "[ArtifactDelivery] upload_private_file attempt %d/3 failed: %s",
                    attempt + 1, type(exc).__name__,
                )
                if attempt < 2:
                    await asyncio.sleep(1 * (2 ** attempt))

    # ── Channel 3: Inline image (images only, LAST RESORT) ──────────
    # May fail under media risk-control, but worth one attempt for UX.
    if is_image and callable(send):
        try:
            img = Image.fromFileSystem(fpath)
            result = await send(MessageChain([img]))
            _check_action_result(result, "send(group_image)")
            logger.info("[ArtifactDelivery] group_image OK")
            return finish(True, "group_image")
        except Exception as exc:
            logger.warning(
                "[ArtifactDelivery] group_image failed: %s", type(exc).__name__
            )

    # ── Channel 4: File component via send (private, LAST RESORT) ───
    # ── All channels exhausted → enqueue for Firestore-backed retry ──
    # A caller must be able to distinguish a persistent retry from a file that
    # merely remains on disk.  Never tell a QQ user that a retry is scheduled
    # until Firestore has actually accepted the entry.
    if callable(send):
        try:
            result = await send(MessageChain([File(name=fname, file=fpath)]))
            _check_action_result(result, "send(file_component)")
            channel = "group_component" if in_group else "private_component"
            logger.info("[ArtifactDelivery] %s OK", channel)
            return finish(True, channel)
        except Exception as exc:
            logger.warning(
                "[ArtifactDelivery] file_component failed: %s",
                type(exc).__name__,
            )

    if weixin_private:
        logger.warning("[ArtifactDelivery] personal-WeChat delivery failed; not queued for QQ retry")
        return finish(False, "retained", "WeixinDeliveryUnavailable")

    logger.warning("[ArtifactDelivery] native and component delivery failed; enqueuing")
    try:
        try:
            from data.plugins.friend_core.delivery_queue import get_queue
        except ImportError:
            # AstrBot's isolated plugin loader exposes sibling packages without
            # the ``data.plugins`` prefix.
            from friend_core.delivery_queue import get_queue
        queue = get_queue()
        enqueue_task = asyncio.create_task(
            asyncio.to_thread(
                queue.enqueue,
                local_path=fpath,
                file_name=fname,
                kind=kind,
                sender_id=sender_id,
                group_id=group_id,
                job_id=str(task_id or "")[:64],
                task_desc=str(task_desc or "")[:200],
                task_owner=str(task_owner or "")[:24],
            )
        )
        done, _pending = await asyncio.wait({enqueue_task}, timeout=3)
        if enqueue_task in done and enqueue_task.result():
            logger.info("[ArtifactDelivery] ENQUEUED for retry")
            return finish(False, "queued", "QueuedForRetry")
        if enqueue_task not in done:
            # Do not hold the QQ response hostage to Firestore/ADC latency.
            # The task continues in the background and may still create a
            # durable retry entry; until then callers truthfully report only
            # that the artifact was retained.
            def _log_delayed_queue_result(task: asyncio.Task) -> None:
                if task.cancelled():
                    logger.info("[ArtifactDelivery] delayed queue cancelled")
                    return
                try:
                    queued = bool(task.result())
                except Exception as exc:
                    logger.warning(
                        "[ArtifactDelivery] delayed queue failed: %s",
                        type(exc).__name__,
                    )
                    return
                logger.info("[ArtifactDelivery] delayed queue result: %s", queued)

            enqueue_task.add_done_callback(_log_delayed_queue_result)
            return finish(False, "retained", "QueuePending")
    except Exception as exc:
        logger.warning("[ArtifactDelivery] enqueue failed: %s", type(exc).__name__)
    return finish(False, "retained", "AllChannelsExhausted")


def chat_response_content(response: Any) -> str:
    """Return a chat-completion result or a safe upstream error message."""

    try:
        payload = response.json()
    except Exception as exc:
        raise RuntimeError("服务返回了无法识别的响应。") from exc

    error = payload.get("error") if isinstance(payload, dict) else None
    if not 200 <= int(getattr(response, "status_code", 200)) < 300:
        raise RuntimeError(_chat_error_message(error))

    try:
        content = payload["choices"][0]["message"]["content"]
    except (IndexError, KeyError, TypeError) as exc:
        raise RuntimeError(_chat_error_message(error)) from exc
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("服务没有返回可用内容，请稍后再试。")
    return content.strip()


def _chat_error_message(error: Any) -> str:
    if isinstance(error, dict):
        message = error.get("message")
    else:
        message = error
    message = str(message or "服务暂时不可用，请稍后再试。").strip()
    return message[:200]


def defer_stop_event(handler: Callable[..., AsyncGenerator[Any, None]]):
    """Stop a handled event only after an async handler has finished yielding."""

    @wraps(handler)
    async def wrapped(self: Any, event: Any, *args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:
        original_stop = event.stop_event
        stop_requested = False

        def request_stop() -> None:
            nonlocal stop_requested
            stop_requested = True

        event.stop_event = request_stop
        try:
            async for result in handler(self, event, *args, **kwargs):
                yield result
        finally:
            event.stop_event = original_stop
            if stop_requested:
                original_stop()

    return wrapped
