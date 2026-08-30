"""Strict loopback client for the isolated local TTS service."""

from __future__ import annotations

import asyncio
import json
import urllib.request
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlparse


Transport = Callable[[str, dict, dict, float], Awaitable[dict]]


class LocalTTSClient:
    def __init__(
        self,
        endpoint: str,
        token: str,
        audio_root: Path,
        *,
        transport: Transport | None = None,
        primary_timeout: float = 45.0,
        fallback_timeout: float = 15.0,
    ):
        parsed = urlparse(str(endpoint or ""))
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("本地 TTS 必须使用 127.0.0.1 环回地址")
        normalized_token = str(token or "").strip()
        if not normalized_token or len(normalized_token) > 200:
            raise ValueError("本地 TTS 令牌无效")
        self.endpoint = str(endpoint).rstrip("/") + "/synthesize"
        self.token = normalized_token
        self.audio_root = Path(audio_root).resolve(strict=False)
        self.transport = transport or self._default_transport
        self.primary_timeout = max(1.0, min(float(primary_timeout), 30.0))
        self.fallback_timeout = max(1.0, min(float(fallback_timeout), 30.0))

    @staticmethod
    async def _default_transport(
        endpoint: str, payload: dict, headers: dict, timeout: float
    ) -> dict:
        def request() -> dict:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                endpoint,
                data=body,
                headers={"Content-Type": "application/json", **headers},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read(16 * 1024)
            value = json.loads(raw.decode("utf-8"))
            return value if isinstance(value, dict) else {}

        return await asyncio.to_thread(request)

    def _validate_audio_path(self, raw: object) -> Path | None:
        value = str(raw or "").strip()
        if not value:
            return None
        candidate = Path(value)
        if candidate.is_symlink():
            return None
        try:
            resolved = candidate.resolve(strict=True)
            root = self.audio_root.resolve(strict=True)
        except OSError:
            return None
        if root not in resolved.parents or not resolved.is_file():
            return None
        if resolved.suffix.lower() not in {".wav", ".mp3", ".ogg"}:
            return None
        size = resolved.stat().st_size
        return resolved if 0 < size <= 20 * 1024 * 1024 else None

    async def synthesize(self, text: str) -> Path | None:
        value = str(text or "").strip()
        if not value:
            raise ValueError("待朗读文字不能为空")
        if len(value) > 600:
            raise ValueError("待朗读文字过长")
        headers = {"X-Local-TTS-Token": self.token}
        for engine, timeout in (
            ("gpt_sovits", self.primary_timeout),
            ("melo", self.fallback_timeout),
        ):
            try:
                response = await asyncio.wait_for(
                    self.transport(
                        self.endpoint,
                        {"text": value, "voice": "xiaoning", "engine": engine},
                        headers,
                        timeout,
                    ),
                    timeout=timeout + 1.0,
                )
            except Exception:
                continue
            path = self._validate_audio_path(response.get("path") if response else None)
            if path is not None:
                return path
        return None
