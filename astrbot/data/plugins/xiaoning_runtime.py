"""Small runtime helpers shared by Xiaoning command plugins."""

from collections.abc import AsyncGenerator, Callable
from functools import wraps
from typing import Any


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
