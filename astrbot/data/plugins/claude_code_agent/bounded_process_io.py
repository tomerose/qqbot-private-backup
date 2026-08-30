"""Stream subprocess output with hard in-flight memory limits."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable


Terminate = Callable[[], Awaitable[object]]


@dataclass(frozen=True)
class ProcessCapture:
    stdout: bytes
    stderr: bytes
    returncode: int | None
    reason: str


async def _read_bounded(
    stream: asyncio.StreamReader | None,
    limit: int,
    stream_name: str,
    buffer: bytearray,
    limit_signal: asyncio.Future[str],
) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            return
        remaining = limit - len(buffer)
        if remaining > 0:
            buffer.extend(chunk[:remaining])
        if len(chunk) > remaining:
            if not limit_signal.done():
                limit_signal.set_result(stream_name)
            # ponytail: buffer full — set the signal once, then drain
            # silently until EOF or termination to keep the pipe alive
            # while the watchdog kills the subprocess.
            break
    # Drain loop after limit: read + discard until EOF or pipe closes.
    while True:
        try:
            chunk = await stream.read(64 * 1024)
        except (OSError, ValueError, asyncio.CancelledError):
            return
        if not chunk:
            return


async def _default_terminate(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except (asyncio.TimeoutError, ProcessLookupError):
        pass


async def capture_bounded_process(
    proc: asyncio.subprocess.Process,
    *,
    stdout_limit: int,
    stderr_limit: int,
    timeout: float,
    terminate: Terminate | None = None,
) -> ProcessCapture:
    stdout_cap = max(1, int(stdout_limit))
    stderr_cap = max(1, int(stderr_limit))
    stdout = bytearray()
    stderr = bytearray()
    limit_signal: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    readers = [
        asyncio.create_task(
            _read_bounded(proc.stdout, stdout_cap, "stdout", stdout, limit_signal)
        ),
        asyncio.create_task(
            _read_bounded(proc.stderr, stderr_cap, "stderr", stderr, limit_signal)
        ),
    ]
    readers_done = asyncio.gather(*readers)
    reason = "completed"
    try:
        done, _pending = await asyncio.wait(
            {readers_done, limit_signal},
            timeout=max(0.01, float(timeout)),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if limit_signal in done:
            reason = f"{limit_signal.result()}_limit"
            if terminate is None:
                await _default_terminate(proc)
            else:
                await terminate()
            await asyncio.wait_for(readers_done, timeout=5)
        elif readers_done in done:
            await readers_done
            await proc.wait()
        else:
            reason = "timeout"
            if terminate is None:
                await _default_terminate(proc)
            else:
                await terminate()
            await asyncio.wait_for(readers_done, timeout=5)
    finally:
        if not limit_signal.done():
            limit_signal.cancel()
        for reader in readers:
            if not reader.done():
                reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        if proc.returncode is None:
            await _default_terminate(proc)
    return ProcessCapture(bytes(stdout), bytes(stderr), proc.returncode, reason)
