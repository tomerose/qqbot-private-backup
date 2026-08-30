"""Cached, non-mutating availability probes for local Agent backends."""

from __future__ import annotations

import asyncio
import subprocess
import time
from collections.abc import Awaitable, Callable, Sequence

try:
    from .agent_core import (
        BACKEND_CLAUDE,
        BACKEND_CODEX,
        BACKEND_WORKBUDDY,
        CLAUDE_EXE,
        CODEX_CLI,
        NODE_EXE,
        WORKBUDDY_CLI,
    )
except ImportError:  # Direct module loading in unit tests.
    from agent_core import (
        BACKEND_CLAUDE,
        BACKEND_CODEX,
        BACKEND_WORKBUDDY,
        CLAUDE_EXE,
        CODEX_CLI,
        NODE_EXE,
        WORKBUDDY_CLI,
    )


ProbeRunner = Callable[[Sequence[str], float], Awaitable[int]]


def backend_probe_command(backend: str) -> list[str]:
    name = str(backend or "").strip().lower()
    if name == BACKEND_CLAUDE:
        return [str(CLAUDE_EXE), "--version"]
    if name == BACKEND_CODEX:
        return [str(NODE_EXE), str(CODEX_CLI), "--version"]
    if name == BACKEND_WORKBUDDY:
        return [str(NODE_EXE), str(WORKBUDDY_CLI), "--version"]
    raise ValueError("不支持的 Agent 后端")


async def _run_probe(command: Sequence[str], timeout: float) -> int:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        creationflags=creationflags,
    )
    try:
        return await asyncio.wait_for(proc.wait(), timeout=max(1.0, float(timeout)))
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124


class BackendHealthCache:
    def __init__(
        self,
        *,
        ttl_seconds: float = 60,
        timeout_seconds: float = 10,
        runner: ProbeRunner = _run_probe,
    ):
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.runner = runner
        self._checked_at = 0.0
        self._available: frozenset[str] = frozenset()
        self._lock = asyncio.Lock()

    async def available(self) -> frozenset[str]:
        now = time.monotonic()
        if now - self._checked_at < self.ttl_seconds:
            return self._available
        async with self._lock:
            now = time.monotonic()
            if now - self._checked_at < self.ttl_seconds:
                return self._available
            names = (BACKEND_CLAUDE, BACKEND_CODEX, BACKEND_WORKBUDDY)

            async def probe(name: str) -> tuple[str, bool]:
                try:
                    return name, await self.runner(
                        backend_probe_command(name), self.timeout_seconds
                    ) == 0
                except (OSError, ValueError, subprocess.SubprocessError):
                    return name, False

            results = await asyncio.gather(*(probe(name) for name in names))
            self._available = frozenset(name for name, ok in results if ok)
            self._checked_at = time.monotonic()
            return self._available
