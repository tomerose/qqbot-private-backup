"""Small runtime helpers shared by Xiaoning command plugins."""

from collections.abc import AsyncGenerator, Callable
from functools import wraps
from typing import Any


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
