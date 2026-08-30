"""Local OpenAI-compatible relay for a user's own model provider.

The relay keeps provider credentials on the user's machine and exposes only a
loopback HTTP endpoint to the bundled AstrBot plugins. Provider-specific
controls are removed before forwarding so unsupported features fail clearly.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse


class ConfigurationError(RuntimeError):
    """Raised when the user has not configured a provider."""


@dataclass(frozen=True)
class RelaySettings:
    api_base: str
    api_key: str
    model: str


_DROP_FIELDS = frozenset(
    {"google_search", "google_maps", "code_execution", "url_context", "model_role", "thinking"}
)
_MAX_REQUEST_BYTES = 24 * 1024 * 1024
_TIMEOUT_SECONDS = 120.0


def _is_loopback(hostname: str) -> bool:
    return hostname.casefold() in {"127.0.0.1", "localhost", "::1"}


def _validate_api_base(raw: str) -> str:
    value = str(raw or "").strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ConfigurationError("XIAONING_LLM_API_BASE must be an HTTPS URL")
    if parsed.scheme != "https" and not _is_loopback(parsed.hostname):
        raise ConfigurationError("non-HTTPS providers are allowed only on loopback")
    if parsed.username or parsed.password:
        raise ConfigurationError("API base must not contain embedded credentials")
    return value


def read_settings(environ: Mapping[str, str] | None = None) -> RelaySettings:
    source = environ if environ is not None else os.environ
    api_base = _validate_api_base(source.get("XIAONING_LLM_API_BASE", ""))
    api_key = str(source.get("XIAONING_LLM_API_KEY", "") or "").strip()
    model = str(source.get("XIAONING_LLM_MODEL", "") or "").strip()
    if not api_key or not model:
        raise ConfigurationError(
            "Set XIAONING_LLM_API_BASE, XIAONING_LLM_API_KEY and "
            "XIAONING_LLM_MODEL in the local configuration."
        )
    return RelaySettings(api_base, api_key, model)


def build_chat_payload(body: object, model: str) -> dict:
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        raise HTTPException(status_code=400, detail="messages must be a list")
    return {
        key: value for key, value in body.items() if key not in _DROP_FIELDS
    } | {"model": model}


def _configuration_error(exc: ConfigurationError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"error": {"message": str(exc), "type": "configuration_error"}},
    )


async def _stream(response: httpx.Response, client: httpx.AsyncClient) -> AsyncIterator[bytes]:
    try:
        async for chunk in response.aiter_raw():
            yield chunk
    finally:
        await response.aclose()
        await client.aclose()


app = FastAPI()


@app.get("/healthz")
async def healthz():
    try:
        settings = read_settings()
    except ConfigurationError as exc:
        return _configuration_error(exc)
    return {"ok": True, "service": "xiaoning-llm-relay", "model": settings.model}


@app.get("/v1/models")
async def list_models():
    try:
        settings = read_settings()
    except ConfigurationError as exc:
        return _configuration_error(exc)
    return {"object": "list", "data": [{"id": settings.model, "object": "model"}]}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    try:
        settings = read_settings()
    except ConfigurationError as exc:
        return _configuration_error(exc)

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            too_large = int(content_length) > _MAX_REQUEST_BYTES
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid content length") from exc
        if too_large:
            raise HTTPException(status_code=413, detail="request body too large")
    try:
        payload = build_chat_payload(await request.json(), settings.model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON request") from exc

    client = httpx.AsyncClient(timeout=_TIMEOUT_SECONDS)
    try:
        upstream = await client.send(
            client.build_request(
                "POST",
                f"{settings.api_base}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {settings.api_key}"},
            ),
            stream=bool(payload.get("stream")),
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "message": "model provider unavailable",
                    "type": "api_error",
                    "reason": type(exc).__name__,
                }
            },
        )

    if payload.get("stream"):
        return StreamingResponse(
            _stream(upstream, client),
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "text/event-stream"),
        )
    content = await upstream.aread()
    await upstream.aclose()
    await client.aclose()
    return Response(
        content=content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


@app.api_route("/v1/{path:path}", methods=["GET", "POST"])
async def unsupported(path: str):
    return JSONResponse(
        status_code=501,
        content={
            "error": {
                "message": f"/{path} is not available with the configured text relay",
                "type": "unsupported_feature",
            }
        },
    )


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=3000)
