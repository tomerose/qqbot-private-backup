"""Small runtime helpers shared by Xiaoning command plugins."""

import asyncio
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.message_components import Image, Plain
from astrbot.core.message.message_event_result import MessageChain


@dataclass(frozen=True)
class ArtifactDeliveryResult:
    """Observable result of delivering one already-generated local artifact."""

    delivered: bool
    channel: str
    path: Path | None = None
    error: str = ""


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
) -> ArtifactDeliveryResult:
    """Deliver a verified file to QQ using NapCat native APIs with retries.

    KEY DESIGN: this account is under QQ media risk-control (风控) — sending
    media *inline* via send_group_msg / send_private_msg fails with
    "EventChecker Failed / retcode 1200". The file-transfer actions
    (upload_group_file / upload_private_file) use a DIFFERENT protocol that
    bypasses the message risk-control, so they are always tried FIRST.

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
        logger.warning("[Delivery] validation failed path=%r: %s", str(path), type(exc).__name__)
        return ArtifactDeliveryResult(False, "retained", error=type(exc).__name__)

    group_id = str(getattr(event, "get_group_id", lambda: "")() or "").strip()
    sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "").strip()
    bot = getattr(event, "bot", None)
    call_action = getattr(bot, "call_action", None)
    send = getattr(event, "send", None)
    is_image = kind == "image"
    fname = resolved.name
    fpath = str(resolved)
    upload_path = _file_upload_path(fpath)
    in_group = bool(group_id and group_id.isdigit())

    logger.info(
        "[Delivery] START fname=%r group=%s sender=%s bot_ok=%s has_call=%s kind=%s",
        fname, group_id, sender_id, bot is not None, callable(call_action), kind
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
                logger.info(f"[ArtifactDelivery] group_upload OK: {fname}")
                return ArtifactDeliveryResult(True, "group_upload", resolved)
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
                logger.info(f"[ArtifactDelivery] {channel} (upload_private_file) OK: {fname}")
                return ArtifactDeliveryResult(True, channel, resolved)
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
            logger.info(f"[ArtifactDelivery] group_image OK: {fname}")
            return ArtifactDeliveryResult(True, "group_image", resolved)
        except Exception as exc:
            logger.warning(
                "[ArtifactDelivery] group_image failed: %s", type(exc).__name__
            )

    # ── Channel 4: File component via send (private, LAST RESORT) ───
    # ── All channels exhausted → enqueue for Firestore-backed retry ──
    # A caller must be able to distinguish a persistent retry from a file that
    # merely remains on disk.  Never tell a QQ user that a retry is scheduled
    # until Firestore has actually accepted the entry.
    logger.warning(f"[ArtifactDelivery] ALL CHANNELS FAILED for {fname} → enqueuing")
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
            )
        )
        done, _pending = await asyncio.wait({enqueue_task}, timeout=1)
        if enqueue_task in done and enqueue_task.result():
            logger.info("[ArtifactDelivery] ENQUEUED for retry: %s", fname)
            return ArtifactDeliveryResult(False, "queued", resolved, "QueuedForRetry")
        if enqueue_task not in done:
            # Do not hold the QQ response hostage to Firestore/ADC latency.
            # The task continues in the background and may still create a
            # durable retry entry; until then callers truthfully report only
            # that the artifact was retained.
            def _log_delayed_queue_result(task: asyncio.Task) -> None:
                if task.cancelled():
                    logger.info("[ArtifactDelivery] delayed queue cancelled: %s", fname)
                    return
                try:
                    queued = bool(task.result())
                except Exception as exc:
                    logger.warning(
                        "[ArtifactDelivery] delayed queue failed for %s: %s",
                        fname, type(exc).__name__,
                    )
                    return
                logger.info("[ArtifactDelivery] delayed queue result for %s: %s", fname, queued)

            enqueue_task.add_done_callback(_log_delayed_queue_result)
            return ArtifactDeliveryResult(False, "retained", resolved, "QueuePending")
    except Exception as exc:
        logger.warning("[ArtifactDelivery] enqueue failed: %s", exc)
    return ArtifactDeliveryResult(False, "retained", resolved, "AllChannelsExhausted")


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
