"""Small OpenAI-compatible proxy for Vertex AI Gemini."""

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import io
import json
import logging
import math
import os
import random
import re
import secrets
import socket
import sys
import time
import uuid
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from google import genai
from google.genai import types

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from image_proxy_core import (
    IMAGE_MODEL_FALLBACK,
    IMAGE_MODEL_PRIMARY,
    IMAGEN_MODELS,
    ImageRequestError,
    MAX_IMAGE_PROMPT_CHARS,
    extract_first_image_bytes,
    image_model_attempts,
    normalize_image_request,
)

app = FastAPI()
PROJECT = os.getenv("VERTEX_PROJECT", "solar-modem-496213-f5")
LOCATION = os.getenv("VERTEX_LOCATION", "global")
MODEL_IDS = {
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-3.1-pro-preview",
    "gemini-3.5-flash",
}
MODEL_ROLES = {
    "fast": "gemini-3.5-flash",
    "quality": "gemini-3.1-pro-preview",
}
MIN_MODEL_OUTPUT_TOKENS = {
    "gemini-2.5-flash": 256,
    "gemini-2.5-pro": 512,
    "gemini-3.1-pro-preview": 512,
    "gemini-3.5-flash": 1000,
}
RESEARCH_AGENT_ROLES = {
    "standard": "deep-research-preview-04-2026",
    "pro": "deep-research-pro-preview-12-2025",
    "max": "deep-research-max-preview-04-2026",
}
SEARCH_MODEL_ALIASES = {"gemini-2.5-flash-search", "gemini-3.5-flash-search"}
MUSIC_MODEL = "lyria-3-clip-preview"
MAX_MUSIC_BYTES = 20 * 1024 * 1024
MAX_CHAT_REQUEST_BYTES = 24 * 1024 * 1024
MAX_CHAT_INGRESS_BYTES = 64 * 1024 * 1024
CHAT_CONTEXT_BUDGET_BYTES = 20 * 1024 * 1024
MAX_CHAT_MESSAGES = 100
CHAT_UPSTREAM_ERROR = "chat service temporarily unavailable"
CHAT_RATE_LIMIT_ERROR = "model capacity is temporarily limited; please retry shortly"
UPSTREAM_RETRY_ATTEMPTS = 3
UPSTREAM_RETRY_BASE_DELAY = 0.75
UPSTREAM_RETRY_MAX_DELAY = 8.0
UPSTREAM_COOLDOWN_SECONDS = 15.0
_upstream_cooldown_until = 0.0
_research_agent_cooldown_until = 0.0
RESEARCH_AGENT_COOLDOWN_SECONDS = 300.0
logger = logging.getLogger("vertex-gemini-proxy")


@app.get("/healthz")
async def healthz(deep: bool = False):
    result = {
        "ok": True,
        "service": "vertex-gemini-proxy",
        "primary_model": "gemini-3.5-flash",
        "image_model": IMAGE_MODEL_PRIMARY,
    }
    if not deep:
        return result
    try:
        client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
        await asyncio.wait_for(
            asyncio.to_thread(
                client.models.generate_content,
                model="gemini-3.5-flash",
                contents="Reply OK",
                config=types.GenerateContentConfig(
                    max_output_tokens=8,
                    thinking_config=types.ThinkingConfig(thinking_level="minimal"),
                ),
            ),
            timeout=15,
        )
        result["upstream"] = "ok"
        return result
    except Exception as exc:
        logger.warning("Vertex health probe failed: %s", type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content={**result, "ok": False, "upstream": "unavailable"},
        )


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {"id": model_id, "object": "model", "capabilities": {"vision": True, "audio": True}}
            for model_id in sorted(MODEL_IDS)
        ]
        + [
            {"id": model_id, "object": "model", "capabilities": {"vision": True, "audio": True, "search": True}}
            for model_id in sorted(SEARCH_MODEL_ALIASES)
        ],
    }


def _resolve_model(model_id: object, model_role: object = None) -> str:
    """Resolve new semantic roles while preserving every existing model id."""
    if isinstance(model_role, str) and model_role in MODEL_ROLES:
        return MODEL_ROLES[model_role]
    candidate = str(model_id or "gemini-3.5-flash")
    return candidate if candidate in MODEL_IDS else "gemini-3.5-flash"


def _interaction_json(value: object) -> dict:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return value
    raise TypeError("invalid interaction response")


def _to_contents(messages: list[dict]) -> tuple[str | None, list[types.Content]]:
    system_prompt = None
    contents = []
    for message in messages:
        role = message.get("role", "user")
        content = message.get("content", "")
        if role == "system":
            system_prompt = str(content)
            continue

        parts = []
        if isinstance(content, list):
            for item in content:
                item_type = item.get("type")
                if item_type in {"image_url", "audio_url"}:
                    media_key = "image_url" if item_type == "image_url" else "audio_url"
                    media = item.get(media_key, {}) or {}
                    url = media.get("url", "")
                    if url.startswith("data:"):
                        header, encoded = url.split(",", 1)
                        mime_type = header.split(":", 1)[1].split(";", 1)[0]
                        parts.append(types.Part.from_bytes(data=base64.b64decode(encoded), mime_type=mime_type))
                elif item_type == "text":
                    parts.append(types.Part.from_text(text=str(item.get("text", ""))))
                else:
                    parts.append(types.Part.from_text(text=str(item.get("text", ""))))
        else:
            parts.append(types.Part.from_text(text=str(content)))
        contents.append(types.Content(role="model" if role == "assistant" else "user", parts=parts))
    return system_prompt, contents


class RequestPayloadError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class UpstreamCapacityError(RuntimeError):
    def __init__(self, retry_after: float):
        super().__init__(CHAT_RATE_LIMIT_ERROR)
        self.retry_after = retry_after


def _upstream_status_code(exc: BaseException) -> int | None:
    """Best-effort status extraction across google-genai transport errors."""
    for value in (
        getattr(exc, "status_code", None),
        getattr(exc, "code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ):
        try:
            status = int(value)
        except (TypeError, ValueError):
            continue
        if 100 <= status <= 599:
            return status
    return None


def _is_retryable_upstream_error(exc: BaseException) -> bool:
    status = _upstream_status_code(exc)
    return status == 429 or (status is not None and 500 <= status < 600) or isinstance(
        exc, (TimeoutError, ConnectionError, OSError)
    )


def _upstream_retry_delay(attempt: int) -> float:
    capped_delay = min(UPSTREAM_RETRY_BASE_DELAY * (2 ** attempt), UPSTREAM_RETRY_MAX_DELAY)
    return capped_delay * random.uniform(0.75, 1.25)


def _upstream_retry_after() -> float:
    return max(0.0, _upstream_cooldown_until - time.monotonic())


def _open_upstream_cooldown() -> float:
    global _upstream_cooldown_until
    _upstream_cooldown_until = max(
        _upstream_cooldown_until,
        time.monotonic() + UPSTREAM_COOLDOWN_SECONDS,
    )
    return _upstream_retry_after()


async def _read_json_request(
    request: Request, max_bytes: int = MAX_CHAT_REQUEST_BYTES
) -> dict:
    content_length = request.headers.get("content-length", "").strip()
    if content_length.isdigit() and int(content_length) > max_bytes:
        raise RequestPayloadError("request body is too large", 413)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            raise RequestPayloadError("request body is too large", 413)
        chunks.append(chunk)
    try:
        body = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequestPayloadError("request body must be valid UTF-8 JSON") from exc
    if not isinstance(body, dict):
        raise RequestPayloadError("request body must be an object")
    return body


def _validate_chat_messages(messages: object) -> list[dict]:
    if not isinstance(messages, list) or not messages:
        raise RequestPayloadError("messages must be a non-empty array")
    if any(not isinstance(message, dict) for message in messages):
        raise RequestPayloadError("each message must be an object")
    if len(messages) <= MAX_CHAT_MESSAGES:
        return messages

    # AstrBot can retain very long conversations. Gemini only needs the latest
    # working window, while the last system message must survive because it is
    # the effective persona/instruction used by _to_contents().
    system = next(
        (message for message in reversed(messages) if message.get("role") == "system"),
        None,
    )
    recent = [message for message in messages if message.get("role") != "system"]
    keep = MAX_CHAT_MESSAGES - (1 if system else 0)
    trimmed = recent[-keep:]
    if system:
        trimmed.insert(0, system)
    logger.info("Trimmed chat history from %d to %d messages", len(messages), len(trimmed))
    return trimmed


def _json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _omit_inline_media(message: dict) -> int:
    content = message.get("content")
    if not isinstance(content, list):
        return 0
    omitted = 0
    for item in content:
        if not isinstance(item, dict) or item.get("type") not in {"image_url", "audio_url"}:
            continue
        media_key = item["type"]
        media = item.get(media_key)
        url = str(media.get("url", "") if isinstance(media, dict) else "")
        if url.lower().startswith(("data:", "base64://")):
            item.clear()
            item.update({"type": "text", "text": "[Earlier inline media omitted]"})
            omitted += 1
    return omitted


def _compact_chat_messages(messages: list[dict]) -> list[dict]:
    """Keep oversized persisted media from turning an ordinary chat into HTTP 413."""
    if _json_size(messages) <= CHAT_CONTEXT_BUDGET_BYTES:
        return messages

    compacted = json.loads(json.dumps(messages, ensure_ascii=False))
    latest_user = next(
        (index for index in range(len(compacted) - 1, -1, -1) if compacted[index].get("role") == "user"),
        -1,
    )
    omitted = sum(
        _omit_inline_media(message)
        for index, message in enumerate(compacted)
        if index != latest_user
    )
    if _json_size(compacted) > CHAT_CONTEXT_BUDGET_BYTES and latest_user >= 0:
        omitted += _omit_inline_media(compacted[latest_user])

    removed = 0
    while _json_size(compacted) > CHAT_CONTEXT_BUDGET_BYTES:
        latest_system = next(
            (index for index in range(len(compacted) - 1, -1, -1) if compacted[index].get("role") == "system"),
            -1,
        )
        protected = {latest_user, latest_system}
        oldest = next((index for index in range(len(compacted)) if index not in protected), None)
        if oldest is None:
            raise RequestPayloadError("chat context remains too large after media trimming")
        compacted.pop(oldest)
        if oldest < latest_user:
            latest_user -= 1
        removed += 1
    if omitted or removed:
        logger.warning(
            "Compacted oversized chat context: omitted_media=%d removed_messages=%d bytes=%d",
            omitted,
            removed,
            _json_size(compacted),
        )
    return compacted


def _sse(data: dict | str) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"data: {payload}\n\n"


_COMPLEX_TASK = re.compile(
    r"(?:设计|规划|制定|架构|实现|调试|排查|诊断|优化|证明|推导|计算|"
    r"比较|权衡|分析|评估|拆解|方案|根因|算法|代码|数学|模型)"
)
_EXPLICIT_THINK = re.compile(r"(?:深度思考|深入分析|仔细分析|认真想想|好好想想|推理一下)")


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict)
        )
    return str(content or "")


def _requires_deep_thinking(messages: list[dict]) -> bool:
    """Keep casual chat fast while reserving high reasoning for real tasks."""
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        text = "".join(_message_text(message.get("content", "")).split())
        if not text:
            return False
        return bool(
            len(text) >= 240
            or _EXPLICIT_THINK.search(text)
            or (len(text) >= 12 and _COMPLEX_TASK.search(text))
        )
    return False


@app.post("/v1/chat/completions")
async def chat(request: Request):
    try:
        body = await _read_json_request(request, MAX_CHAT_INGRESS_BYTES)
        messages = _compact_chat_messages(_validate_chat_messages(body.get("messages")))
    except RequestPayloadError as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"message": str(exc), "type": "invalid_request_error"}},
        )
    extra = body.get("custom_extra_body") or {}
    if not isinstance(extra, dict):
        return JSONResponse(status_code=400, content={"error": {"message": "custom_extra_body must be an object"}})
    model_id = body.get("model", "gemini-3.5-flash")
    use_search = False
    if model_id in SEARCH_MODEL_ALIASES:
        model_id = "gemini-3.5-flash"
        use_search = True
    model_id = _resolve_model(model_id, body.get("model_role"))
    # ponytail: google_search flag via custom_extra_body or top-level param
    if not use_search:
        use_search = bool(body.get("google_search") or extra.get("google_search"))
    system_prompt, contents = _to_contents(messages)
    tools = []
    if use_search:
        tools.append(types.Tool(google_search=types.GoogleSearch()))
    if body.get("google_maps"):
        tools.append(types.Tool(google_maps={}))
    if body.get("code_execution"):
        tools.append(types.Tool(code_execution={}))
    if body.get("url_context"):
        tools.append(types.Tool(url_context={}))
    response_json_schema = body.get("response_json_schema")
    if response_json_schema is not None:
        try:
            schema_size = len(json.dumps(response_json_schema, ensure_ascii=False))
        except (TypeError, ValueError, OverflowError):
            schema_size = MAX_CHAT_REQUEST_BYTES
        if not isinstance(response_json_schema, dict) or schema_size > 16_000:
            return JSONResponse(
                status_code=400,
                content={"error": {"message": "response_json_schema must be a small object"}},
            )
    explicit_thinking = bool(body.get("thinking") or extra.get("thinking"))
    auto_deep_thinking = (
        model_id == "gemini-3.5-flash"
        and not explicit_thinking
        and _requires_deep_thinking(messages)
    )
    use_thinking = explicit_thinking  # ponytail: auto deep thinking disabled — Vertex AI 403s on high mode
    try:
        max_output_tokens = min(int(body.get("max_tokens", 4096)), 8192)
        # Thinking models may consume tiny OpenAI-compatible limits entirely
        # on hidden reasoning and then return an empty answer.  Preserve a
        # small model-specific floor so callers always receive visible output.
        max_output_tokens = max(
            max_output_tokens, MIN_MODEL_OUTPUT_TOKENS.get(model_id, 1)
        )
        thinking_budget = int(extra.get("thinking_budget") or 2048)
        temperature = float(body.get("temperature", extra.get("temperature", 1.05)))
        top_p = float(body.get("top_p", extra.get("top_p", 0.98)))
        if (
            max_output_tokens < 1
            or not math.isfinite(temperature)
            or not 0 <= temperature <= 2
            or not math.isfinite(top_p)
            or not 0 <= top_p <= 1
        ):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": "numeric generation parameters are invalid",
                    "type": "invalid_request_error",
                }
            },
        )
    config_options = dict(
        max_output_tokens=max_output_tokens,
        safety_settings=[
            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH),
        ],
        tools=tools if tools else None,
    )
    config_options["temperature"] = temperature
    config_options["top_p"] = top_p
    if response_json_schema is not None:
        config_options["response_mime_type"] = "application/json"
        config_options["response_json_schema"] = response_json_schema
    config = types.GenerateContentConfig(**config_options)
    if model_id == "gemini-3.5-flash":
        # Gemini 3.5 has built-in thinking; DON'T set thinking_config by default
        # (thinking_budget paradoxically makes the model eat ALL tokens for thoughts).
        # Only enable when explicitly requested via thinking:true.
        if use_thinking:
            config.thinking_config = types.ThinkingConfig(
                include_thoughts=explicit_thinking,
                thinking_level="high",
            )
        # else: no thinking_config — model handles thinking internally without
        # starving visible output. Requires sufficient max_output_tokens (>= 1000).
    elif use_thinking:
        config.thinking_config = types.ThinkingConfig(
            include_thoughts=True,
            thinking_budget=min(max(int(thinking_budget), 256), 8192),
        )
    if system_prompt:
        config.system_instruction = system_prompt

    try:
        retry_after = _upstream_retry_after()
        if retry_after:
            raise UpstreamCapacityError(retry_after)
        client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
        if body.get("stream"):
            async def stream_response():
                response_id = "gemini-" + uuid.uuid4().hex[:8]
                sent_role = False
                try:
                    stream = await client.aio.models.generate_content_stream(
                        model=model_id,
                        contents=contents,
                        config=config,
                    )
                    async for chunk in stream:
                        text = str(getattr(chunk, "text", "") or "")
                        if not text:
                            continue
                        delta = {"content": text}
                        if not sent_role:
                            delta["role"] = "assistant"
                            sent_role = True
                        yield _sse({
                            "id": response_id,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model_id,
                            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                        })
                    yield _sse({
                        "id": response_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model_id,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    })
                    yield _sse("[DONE]")
                except Exception as exc:
                    logger.exception("streaming chat completion failed")
                    message = CHAT_RATE_LIMIT_ERROR if _is_retryable_upstream_error(exc) else CHAT_UPSTREAM_ERROR
                    if message == CHAT_RATE_LIMIT_ERROR:
                        _open_upstream_cooldown()
                    yield _sse({"error": {"message": message, "type": "api_error"}})

            return StreamingResponse(stream_response(), media_type="text/event-stream")

        # Retry transient upstream errors and empty model replies with truncated
        # exponential backoff. A short shared cooldown prevents QQ plugin bursts
        # from repeatedly hitting Vertex while capacity is exhausted.
        for attempt in range(UPSTREAM_RETRY_ATTEMPTS):
            try:
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model=model_id,
                    contents=contents,
                    config=config,
                )
            except Exception as exc:
                if not _is_retryable_upstream_error(exc):
                    raise
                if attempt == UPSTREAM_RETRY_ATTEMPTS - 1:
                    retry_after = _open_upstream_cooldown()
                    logger.warning(
                        "Vertex capacity remained unavailable after %d attempts; cooling down for %.1fs",
                        UPSTREAM_RETRY_ATTEMPTS,
                        retry_after,
                    )
                    raise UpstreamCapacityError(retry_after) from exc
                delay = _upstream_retry_delay(attempt)
                logger.warning(
                    "Vertex request failed with retryable status %s; retrying in %.2fs (%d/%d)",
                    _upstream_status_code(exc),
                    delay,
                    attempt + 1,
                    UPSTREAM_RETRY_ATTEMPTS,
                )
                await asyncio.sleep(delay)
                continue
            usage = response.usage_metadata

            # Extract thoughts and final text from Gemini thinking response
            thoughts_parts: list[str] = []
            text_parts: list[str] = []
            execution_parts: list[str] = []
            executable_code_parts: list[str] = []
            if hasattr(response, "candidates") and response.candidates:
                for candidate in response.candidates:
                    parts = getattr(getattr(candidate, "content", None), "parts", []) or []
                    for part in parts:
                        if getattr(part, "thought", False):
                            thoughts_parts.append(str(getattr(part, "text", "") or ""))
                            continue
                        part_text = str(getattr(part, "text", "") or "")
                        if part_text:
                            text_parts.append(part_text)
                        # code_execution_result (output from executed code)
                        execution = getattr(part, "code_execution_result", None)
                        output = str(getattr(execution, "output", "") or "").strip()
                        if output:
                            execution_parts.append(output)
                        # executable_code (code Gemini generated, e.g. for vision analysis)
                        exe_code = getattr(part, "executable_code", None)
                        if exe_code is not None:
                            code_str = str(getattr(exe_code, "code", "") or "").strip()
                            if code_str:
                                executable_code_parts.append(code_str)

            final_text = str(getattr(response, "text", "") or "")
            if explicit_thinking and thoughts_parts:
                thought_block = "\n".join(thoughts_parts).strip()
                answer_block = "\n".join(text_parts).strip() or final_text
                visible_text = (
                    "🤔 深度思考中…\n\n"
                    + thought_block
                    + "\n\n━━━━━━━━━━\n\n"
                    + answer_block
                )
            else:
                visible_text = (
                    final_text
                    or "\n".join(execution_parts).strip()
                    or "\n".join(text_parts).strip()
                )
                # ponytail: Gemini sometimes generates executable_code parts
                # (e.g. when analyzing document images) even without code_execution
                # tool enabled. When all other text is empty, surface the code as
                # a fallback so the user sees something rather than an error.
                if not visible_text.strip() and executable_code_parts:
                    visible_text = (
                        "Gemini 尝试执行以下代码来分析文档：\n```python\n"
                        + "\n".join(executable_code_parts)
                        + "\n```"
                    )
                thought_block = ""

            if visible_text.strip():
                break  # got a real response
            if attempt < UPSTREAM_RETRY_ATTEMPTS - 1:
                await asyncio.sleep(_upstream_retry_delay(attempt))
        else:
            # All grounded attempts returned empty — retry once without
            # search / maps / code_execution / url_context tools before
            # giving up.  Google grounding can produce empty output
            # deterministically for some queries; a plain model reply is
            # almost always better than a 502.
            if tools:
                logger.warning(
                    "grounded generation returned empty after %d attempts; "
                    "retrying without tools",
                    UPSTREAM_RETRY_ATTEMPTS,
                )
                try:
                    config.tools = None
                    response = await asyncio.to_thread(
                        client.models.generate_content,
                        model=model_id,
                        contents=contents,
                        config=config,
                    )
                    visible_text = str(getattr(response, "text", "") or "").strip()
                    usage = response.usage_metadata
                    if not visible_text:
                        raise ValueError("empty model response (fallback)")
                except Exception:
                    raise ValueError("empty model response") from None
            else:
                raise ValueError("empty model response")

        result = {
            "id": "gemini-" + uuid.uuid4().hex[:8],
            "object": "chat.completion",
            "model": model_id,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": visible_text}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": getattr(usage, "prompt_token_count", 0) if usage else 0,
                "completion_tokens": getattr(usage, "candidates_token_count", 0) if usage else 0,
            },
        }
        if explicit_thinking and thought_block:
            result["thinking"] = {
                "thoughts": thought_block,
                "answer": answer_block or final_text,
            }
        # ponytail: attach grounding metadata when search was used
        if (use_search or body.get("google_maps")) and hasattr(response, "candidates") and response.candidates:
            candidate = response.candidates[0]
            if hasattr(candidate, "grounding_metadata") and candidate.grounding_metadata:
                gm = candidate.grounding_metadata
                sources = []
                if hasattr(gm, "grounding_chunks"):
                    for chunk in (gm.grounding_chunks or []):
                        if hasattr(chunk, "web") and chunk.web:
                            sources.append({"title": getattr(chunk.web, "title", ""), "uri": getattr(chunk.web, "uri", "")})
                        elif hasattr(chunk, "maps") and chunk.maps:
                            sources.append({"title": getattr(chunk.maps, "title", ""), "uri": getattr(chunk.maps, "uri", "")})
                result["grounding"] = {
                    "sources": sources,
                    "search_queries": list(getattr(gm, "web_search_queries", []) or []),
                }
        return result
    except UpstreamCapacityError as exc:
        logger.warning("chat completion temporarily rejected during Vertex cooldown")
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": str(max(1, math.ceil(exc.retry_after)))},
            content={"error": {"message": CHAT_RATE_LIMIT_ERROR, "type": "rate_limit_error"}},
        )
    except Exception as exc:
        logger.exception("chat completion failed")
        return JSONResponse(
            status_code=502,
            content={"error": {"message": CHAT_UPSTREAM_ERROR, "type": "api_error"}},
        )


@app.post("/v1/interactions")
async def create_interaction(request: Request):
    """Bounded Google Interactions canary for deep-research jobs.

    Existing chat-completion callers are deliberately untouched.  Model roles
    stay on the proven generate-content path; arbitrary tools and remote MCP
    servers are not accepted at this boundary.
    """
    global _research_agent_cooldown_until
    agent_error: Exception | None = None
    input_value: object = None
    system_instruction = ""
    try:
        body = await _read_json_request(request)
        input_value = body.get("input")
        input_size = len(json.dumps(input_value, ensure_ascii=False))
        if input_value in (None, "", []) or input_size > 100_000:
            raise RequestPayloadError("interaction input is invalid", 400)
        system_instruction = str(body.get("system_instruction") or "")
        if len(system_instruction) > 12_000:
            raise RequestPayloadError("system_instruction is too long", 400)

        research_role = body.get("research_role")
        kwargs = {
            "input": input_value,
            "stream": False,
            "labels": {"application": "xiaoning", "channel": "canary"},
        }
        if system_instruction:
            kwargs["system_instruction"] = system_instruction
        previous_id = str(body.get("previous_interaction_id") or "").strip()
        if previous_id:
            if not re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", previous_id):
                raise RequestPayloadError("previous_interaction_id is invalid", 400)
            kwargs["previous_interaction_id"] = previous_id

        if research_role is None:
            raise RequestPayloadError(
                "research_role is required; model roles use /v1/chat/completions",
                400,
            )
        if research_role not in RESEARCH_AGENT_ROLES:
            raise RequestPayloadError("unknown research_role", 400)
        kwargs.update(
            agent=RESEARCH_AGENT_ROLES[research_role],
            background=True,
            store=True,
        )

        if time.monotonic() >= _research_agent_cooldown_until:
            client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(client.interactions.create, **kwargs),
                    timeout=90,
                )
                return _interaction_json(response)
            except Exception as exc:
                agent_error = exc
                _research_agent_cooldown_until = (
                    time.monotonic() + RESEARCH_AGENT_COOLDOWN_SECONDS
                )
                logger.warning(
                    "research agent unavailable (%s); executing grounded fallback",
                    type(exc).__name__,
                )
        else:
            agent_error = RuntimeError("research agent is cooling down")

        fallback = await asyncio.wait_for(
            asyncio.to_thread(
                _generate_grounded_research,
                input_value,
                system_instruction,
            ),
            timeout=120,
        )
        return fallback
    except RequestPayloadError as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"message": str(exc), "type": "invalid_request_error"}},
        )
    except Exception as exc:
        upstream_error = agent_error or exc
        if "Resource setup has just started" in str(exc):
            return JSONResponse(
                status_code=503,
                headers={"Retry-After": "30"},
                content={"error": {"message": "research service is provisioning", "type": "api_error"}},
            )
        if _is_retryable_upstream_error(upstream_error):
            return JSONResponse(
                status_code=503,
                headers={"Retry-After": "60"},
                content={"error": {"message": CHAT_RATE_LIMIT_ERROR, "type": "rate_limit_error"}},
            )
        logger.exception("interaction creation failed")
        return JSONResponse(
            status_code=502,
            content={"error": {"message": CHAT_UPSTREAM_ERROR, "type": "api_error"}},
        )


def _response_visible_text(response: object) -> str:
    text = str(getattr(response, "text", "") or "").strip()
    if text:
        return text
    parts: list[str] = []
    for candidate in getattr(response, "candidates", None) or []:
        for part in getattr(getattr(candidate, "content", None), "parts", None) or []:
            if not getattr(part, "thought", False):
                value = str(getattr(part, "text", "") or "").strip()
                if value:
                    parts.append(value)
    return "\n".join(parts).strip()


def _grounding_sources(response: object) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    for candidate in getattr(response, "candidates", None) or []:
        metadata = getattr(candidate, "grounding_metadata", None)
        for chunk in getattr(metadata, "grounding_chunks", None) or []:
            web = getattr(chunk, "web", None)
            uri = str(getattr(web, "uri", "") or "").strip()
            if uri:
                sources.append(
                    {
                        "title": str(getattr(web, "title", "") or "").strip(),
                        "uri": uri,
                    }
                )
    return sources[:20]


def _generate_grounded_research(
    input_value: object, system_instruction: str = ""
) -> dict:
    """Complete a research request even when the stateful agent has no quota."""
    prompt = (
        input_value
        if isinstance(input_value, str)
        else json.dumps(input_value, ensure_ascii=False, separators=(",", ":"))
    )
    config = types.GenerateContentConfig(
        max_output_tokens=8192,
        temperature=0.3,
        tools=[types.Tool(google_search=types.GoogleSearch())],
        system_instruction=system_instruction or None,
    )
    last_error: Exception | None = None
    for model_id in (MODEL_ROLES["quality"], MODEL_ROLES["fast"]):
        try:
            client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
            response = client.models.generate_content(
                model=model_id,
                contents=prompt,
                config=config,
            )
            text = _response_visible_text(response)
            if not text:
                raise ValueError("empty grounded research response")
            return {
                "id": "fallback-" + uuid.uuid4().hex[:16],
                "object": "interaction",
                "status": "completed",
                "model": model_id,
                "output_text": text,
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    }
                ],
                "grounding": {"sources": _grounding_sources(response)},
                "fallback": {
                    "used": True,
                    "mode": "google_search",
                    "reason": "research_agent_unavailable",
                },
            }
        except Exception as exc:
            last_error = exc
            logger.warning(
                "grounded research model %s failed: %s",
                model_id,
                type(exc).__name__,
            )
    raise RuntimeError("grounded research fallback failed") from last_error


def _validated_interaction_id(interaction_id: str) -> str:
    value = str(interaction_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", value):
        raise ValueError("invalid interaction id")
    return value


@app.get("/v1/interactions/{interaction_id}")
async def get_interaction(interaction_id: str):
    try:
        client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
        response = await asyncio.wait_for(
            asyncio.to_thread(client.interactions.get, _validated_interaction_id(interaction_id)),
            timeout=30,
        )
        return _interaction_json(response)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": {"message": str(exc)}})
    except Exception:
        logger.exception("interaction lookup failed")
        return JSONResponse(status_code=502, content={"error": {"message": CHAT_UPSTREAM_ERROR}})


@app.post("/v1/interactions/{interaction_id}/cancel")
async def cancel_interaction(interaction_id: str):
    try:
        client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
        response = await asyncio.wait_for(
            asyncio.to_thread(client.interactions.cancel, _validated_interaction_id(interaction_id)),
            timeout=30,
        )
        return _interaction_json(response)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": {"message": str(exc)}})
    except Exception:
        logger.exception("interaction cancellation failed")
        return JSONResponse(status_code=502, content={"error": {"message": CHAT_UPSTREAM_ERROR}})


def _generate_music(prompt: str) -> tuple[bytes, str]:
    """Generate one original Lyria clip through the existing Vertex AI identity."""
    client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
    response = client.interactions.create(
        model=MUSIC_MODEL,
        input=[{"type": "text", "text": prompt}],
    )
    # Vertex's current Interaction schema exposes audio as ``output_audio``.
    # Keep the older outputs scan for installed client compatibility.
    audio = getattr(response, "output_audio", None) or next(
        (
            output
            for output in (getattr(response, "outputs", None) or [])
            if getattr(output, "type", None) == "audio"
        ),
        None,
    )
    encoded = getattr(audio, "data", None)
    if not isinstance(encoded, str):
        raise ValueError("missing music response")
    payload = base64.b64decode(encoded, validate=True)
    if not payload or len(payload) > MAX_MUSIC_BYTES:
        raise ValueError("invalid music size")
    mime = str(getattr(audio, "mime_type", "audio/mpeg") or "audio/mpeg")
    return payload, mime


@app.post("/v1/music/generations")
async def generate_music(request: Request):
    try:
        prompt = str((await request.json()).get("prompt", "")).strip()
    except (ValueError, json.JSONDecodeError):
        prompt = ""
    if not prompt or len(prompt) > 800:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "invalid music prompt", "type": "invalid_request_error"}},
        )
    try:
        payload, mime = await asyncio.to_thread(_generate_music, prompt)
    except Exception:
        logger.exception("music generation failed")
        return JSONResponse(
            status_code=502,
            content={"error": {"message": "music generation unavailable", "type": "api_error"}},
        )
    return {
        "created": int(time.time()),
        "data": [{"b64_json": base64.b64encode(payload).decode("ascii"), "mime_type": mime}],
    }


def _generate_imagen_sync(payload, n_images: int = 1) -> list[tuple[str, bytes, str]] | None:
    """Vertex Imagen 3/4 — high-quality dedicated image model via generate_images()."""
    n = min(max(int(n_images), 1), 4)
    for model_id in image_model_attempts(payload.model):
        if model_id not in IMAGEN_MODELS:
            break  # don't fall through to Gemini text-image models
        try:
            client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
            response = client.models.generate_images(
                model=model_id,
                prompt=payload.prompt,
                config=types.GenerateImagesConfig(
                    number_of_images=n,
                    aspect_ratio=payload.aspect_ratio,
                    safety_filter_level="block_only_high",
                ),
            )
            images = []
            for gen_img in (getattr(response, "generated_images", None) or []):
                img_obj = getattr(gen_img, "image", None)
                img_bytes = getattr(img_obj, "image_bytes", None)
                if img_bytes:
                    images.append(("image/png", img_bytes, model_id))
            if images:
                return images
            logger.warning("Imagen model %s returned no images", model_id)
        except Exception:
            logger.exception("Imagen generation failed for model %s", model_id)
    return None


def _generate_image_sync(payload):
    for model_id in image_model_attempts(payload.model):
        try:
            client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
            response = client.models.generate_content(
                model=model_id,
                contents=payload.prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["TEXT", "IMAGE"],
                    image_config=types.ImageConfig(
                        aspect_ratio=payload.aspect_ratio,
                        image_size=payload.image_size,
                    ),
                ),
            )
            image = extract_first_image_bytes(response)
            if image is None:
                logger.warning("image model %s returned no inline image", model_id)
                continue
            mime_type, image_bytes = image
            return mime_type, image_bytes, model_id
        except Exception:
            logger.exception("image generation failed for model %s", model_id)
    return None


@app.post("/v1/images/generations")
async def generate_image(request: Request):
    try:
        body = await request.json()
        payload = normalize_image_request(body)
        n_images = min(max(int(body.get("n", 1)), 1), 4)
    except (ImageRequestError, ValueError, json.JSONDecodeError, TypeError):
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "无效的作图请求。", "type": "invalid_request_error"}},
        )

    # ── Imagen 3/4 path (PRO tier) — DEPRECATED by Google, sunset 2026-06-30 ──
    if payload.model in IMAGEN_MODELS:
        return JSONResponse(
            status_code=502,
            content={"error": {"message": "Imagen 已下线，请使用 Gemini 图片模型。", "type": "api_error"}},
        )

    # ── Gemini image path (all tiers) ────────────────────────────────
    image = await asyncio.to_thread(_generate_image_sync, payload)
    if image is not None:
        mime_type, image_bytes, model_id = image
        return {
            "created": int(time.time()),
            "data": [
                {
                    "b64_json": base64.b64encode(image_bytes).decode("ascii"),
                    "mime_type": mime_type,
                    "model": model_id,
                }
            ],
        }
    return JSONResponse(
        status_code=502,
        content={"error": {"message": "作图服务暂时不可用，请稍后再试。", "type": "api_error"}},
    )


def _edit_image_sync(image_base64: str, prompt: str, model: str):
    """Edit the supplied pixels directly; never pass off a redraw as an edit."""
    try:
        image_bytes = base64.b64decode(image_base64, validate=True)
    except (ValueError, base64.binascii.Error):
        raise ImageRequestError("无效的图片数据。")
    if len(image_bytes) > 10 * 1024 * 1024:
        raise ImageRequestError("图片不能超过 10 MB。")
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        mime_type = "image/png"
    elif image_bytes.startswith(b"\xff\xd8\xff"):
        mime_type = "image/jpeg"
    elif image_bytes.startswith((b"GIF87a", b"GIF89a")):
        mime_type = "image/gif"
    elif image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        mime_type = "image/webp"
    else:
        raise ImageRequestError("不支持的图片格式。")

    try:
        from PIL import Image as PILImage
        with PILImage.open(io.BytesIO(image_bytes)) as source_image:
            source_image.load()
            source_size = source_image.size
    except Exception as exc:
        raise ImageRequestError("无法读取输入图片。") from exc

    edit_prompt = (
        "Edit the supplied image itself. Preserve its exact dimensions, crop, subject, "
        "pose, composition, colors, lighting, and all unaffected pixels as closely as "
        "possible. Do not redraw, recompose, replace, or add unrelated content. "
        "Apply only this requested change:\n" + prompt.strip()
    )
    edit_models = tuple(dict.fromkeys((
        model,
        "gemini-3.1-flash-image",
        "gemini-3-pro-image",
    )))
    for edit_model in edit_models:
        try:
            client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
            response = client.models.generate_content(
                model=edit_model,
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                    types.Part.from_text(text=edit_prompt),
                ],
                config=types.GenerateContentConfig(
                    response_modalities=["TEXT", "IMAGE"],
                ),
            )
            img = extract_first_image_bytes(response)
            if img is not None:
                _output_mime, output_bytes = img
                try:
                    with PILImage.open(io.BytesIO(output_bytes)) as edited_image:
                        edited_image.load()
                        cleaned = edited_image.convert(
                            "RGBA" if "A" in edited_image.getbands() else "RGB"
                        )
                        if cleaned.size != source_size:
                            cleaned = cleaned.resize(source_size, PILImage.Resampling.LANCZOS)
                        normalized = io.BytesIO()
                        cleaned.save(normalized, format="PNG", optimize=True)
                        output_bytes = normalized.getvalue()
                except Exception:
                    logger.warning("image edit: model %s returned invalid image bytes", edit_model)
                    continue
                logger.info("image edit: direct pixel edit via %s (%d bytes)",
                            edit_model, len(output_bytes))
                return "image/png", output_bytes, edit_model
            logger.warning("image edit: model %s returned no edited image", edit_model)
        except Exception:
            logger.exception("image edit failed for model %s", edit_model)

    return None


@app.post("/v1/images/edits")
async def edit_image(request: Request):
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "无效的编辑请求。", "type": "invalid_request_error"}},
        )
    image_b64 = str(body.get("image", "") or "").strip()
    if not image_b64:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "请提供要编辑的图片（base64）。", "type": "invalid_request_error"}},
        )
    prompt = " ".join(str(body.get("prompt", "") or "").split())
    if not prompt or len(prompt) > MAX_IMAGE_PROMPT_CHARS:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": f"编辑描述最多 {MAX_IMAGE_PROMPT_CHARS} 个字符。", "type": "invalid_request_error"}},
        )
    model = str(body.get("model", "") or "gemini-2.5-flash-image").strip()
    try:
        result = await asyncio.to_thread(_edit_image_sync, image_b64, prompt, model)
    except ImageRequestError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": str(exc), "type": "invalid_request_error"}},
        )
    except Exception:
        logger.exception("image edit failed")
        return JSONResponse(
            status_code=502,
            content={"error": {"message": "图片编辑服务暂时不可用，请稍后再试。", "type": "api_error"}},
        )
    if result is not None:
        mime_type, image_bytes, model_id = result
        return {
            "created": int(time.time()),
            "data": [{"b64_json": base64.b64encode(image_bytes).decode("ascii"), "mime_type": mime_type, "model": model_id}],
        }
    return JSONResponse(
        status_code=502,
        content={"error": {"message": "图片编辑失败，请尝试换一种描述。", "type": "api_error"}},
    )


VIDEO_MODEL = "veo-3.1-lite-generate-001"
VIDEO_MODEL_PRO = "veo-3.1-generate-001"    # Veo 3.1 full — audio-enabled, up to 8s
VIDEO_MODEL_PRO_FALLBACK = "veo-3.0-generate-001"  # Veo 3.0 fallback
VIDEO_DURATION = 4
VIDEO_ASPECT_RATIO = "16:9"
VIDEO_POLL_SECONDS = 10
VIDEO_MAX_POLL = 48  # 8 min max wait (Lite ~2-4 min, Standard ~5-8 min)
GIF_FRAMES = 4
GIF_FRAME_DELAY = 300  # ms per frame


def _generate_gif_frames(client, prompt: str, aspect: str, n: int) -> list[bytes]:
    """Generate N frame images via Gemini, return list of PNG bytes."""
    frames = []
    for i in range(n):
        frame_prompt = (
            f"{prompt} — frame {i + 1} of {n}. "
            f"Consistent style, smooth transition from previous frame."
        )
        if i == 0:
            frame_prompt = f"{prompt} — opening frame, establishing shot."
        if i == n - 1:
            frame_prompt = f"{prompt} — closing frame, final shot."
        img = None
        for attempt_prompt in dict.fromkeys((frame_prompt, prompt)):
            for model_id in image_model_attempts(IMAGE_MODEL_PRIMARY):
                try:
                    response = client.models.generate_content(
                        model=model_id,
                        contents=attempt_prompt,
                        config=types.GenerateContentConfig(
                            response_modalities=["TEXT", "IMAGE"],
                            image_config=types.ImageConfig(aspect_ratio=aspect, image_size="1K"),
                        ),
                    )
                    img = extract_first_image_bytes(response)
                    if img:
                        break
                    logger.warning("GIF frame %d model %s returned no image", i, model_id)
                except Exception:
                    logger.exception("GIF frame %d generation failed for model %s", i, model_id)
            if img:
                break
        if img:
            frames.append(img[1])  # img is (mime_type, bytes)
    return frames


def _generate_video_sync(prompt: str, model: str, duration: int, aspect: str):
    """Run the blocking Vertex polling and GIF fallback outside FastAPI's loop."""
    client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)

    # ── Try Veo first ──
    try:
        # Non-lite Veo models (3.1 full, 3.0) support generate_audio
        is_veo3_audio = "lite" not in model
        video_config = types.GenerateVideosConfig(
            aspect_ratio=aspect,
            duration_seconds=duration,
            enhance_prompt=True,
        )
        if is_veo3_audio:
            # Enable native audio (dialogue + ambience + music)
            try:
                video_config.generate_audio = True  # type: ignore[attr-defined]
            except Exception:
                pass  # older SDK — audio will be best-effort
        operation = client.models.generate_videos(
            model=model,
            prompt=prompt,
            config=video_config,
        )
        for _ in range(VIDEO_MAX_POLL):
            if operation.done:
                break
            time.sleep(VIDEO_POLL_SECONDS)
            operation = client.operations.get(operation)

        if operation.done:
            video = operation.result.generated_videos[0]
            video_bytes = video.video.video_bytes
            return {
                "created": int(time.time()),
                "data": [{"b64_json": base64.b64encode(video_bytes).decode("ascii"),
                           "mime_type": "video/mp4", "model": model, "duration": duration}],
            }
    except Exception:
        logger.info("Veo unavailable, falling back to multi-frame GIF")

    # ── Fallback: multi-frame GIF ──
    frames = _generate_gif_frames(client, prompt, aspect, GIF_FRAMES)
    if len(frames) < 2:
        return JSONResponse(
            status_code=502,
            content={"error": {"message": "视频生成服务暂时不可用，请稍后再试。", "type": "api_error"}},
        )

    try:
        from PIL import Image as PILImage
        images = []
        for data in frames:
            images.append(PILImage.open(io.BytesIO(data)))
        gif_buf = io.BytesIO()
        images[0].save(
            gif_buf, format="GIF", save_all=True,
            append_images=images[1:], duration=GIF_FRAME_DELAY, loop=0,
        )
        gif_bytes = gif_buf.getvalue()
        return {
            "created": int(time.time()),
            "data": [{"b64_json": base64.b64encode(gif_bytes).decode("ascii"),
                       "mime_type": "image/gif", "model": f"{IMAGE_MODEL_PRIMARY}-gif",
                       "frames": len(frames)}],
        }
    except Exception:
        logger.exception("GIF assembly failed")
        return JSONResponse(
            status_code=502,
            content={"error": {"message": "视频生成服务暂时不可用，请稍后再试。", "type": "api_error"}},
        )


@app.post("/v1/videos/generations")
async def generate_video(request: Request):
    try:
        body = await request.json()
        prompt = str(body.get("prompt", "")).strip()
        if not prompt or len(prompt) > 800:
            return JSONResponse(
                status_code=400,
                content={"error": {"message": "视频描述需在 1-800 字符。", "type": "invalid_request_error"}},
            )
        model = str(body.get("model", VIDEO_MODEL)).strip() or VIDEO_MODEL
        duration = max(1, min(int(body.get("duration", VIDEO_DURATION)), 8))
        aspect = body.get("aspect_ratio", VIDEO_ASPECT_RATIO)
        if aspect not in {"16:9", "9:16", "1:1"}:
            aspect = VIDEO_ASPECT_RATIO
    except (json.JSONDecodeError, TypeError, ValueError):
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "无效的视频请求。", "type": "invalid_request_error"}},
        )
    return await asyncio.to_thread(
        _generate_video_sync, prompt, model, duration, aspect
    )

# ── Video download endpoint ──────────────────────────────────────
VIDEO_DOWNLOAD_MAX_MB = 48  # QQ file limit ~50MB, leave margin
VIDEO_DOWNLOAD_TIMEOUT = (15, 90)
VIDEO_PAGE_HOSTS = {
    "youtube.com", "youtu.be", "bilibili.com", "b23.tv", "vimeo.com",
    "douyin.com", "iesdouyin.com", "ixigua.com", "acfun.cn", "kuaishou.com",
    "x.com", "twitter.com",
}


def _is_safe_video_url(url: str) -> bool:
    """Reject credentials, non-HTTP schemes, and hosts resolving to local networks."""
    try:
        parsed = urlparse(str(url or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.username or parsed.password:
            return False
        if parsed.port not in {None, 80, 443}:
            return False
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        }
        if not addresses:
            return False
        for raw in addresses:
            address = ipaddress.ip_address(raw)
            if (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_multicast
                or address.is_reserved
                or address.is_unspecified
            ):
                return False
        return True
    except (OSError, TypeError, ValueError):
        return False


def _is_supported_video_page(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in VIDEO_PAGE_HOSTS)


def _try_direct_download(
    url: str,
    extra_headers: dict[str, str] | None = None,
) -> tuple[bytes, str] | None:
    """Try to GET a direct video URL. Returns (bytes, mime_type) or None."""
    resp = None
    try:
        current_url = url
        headers = {"User-Agent": "Mozilla/5.0"}
        headers.update(extra_headers or {})
        for _ in range(4):
            if not _is_safe_video_url(current_url):
                return None
            resp = requests.get(
                current_url,
                stream=True,
                timeout=VIDEO_DOWNLOAD_TIMEOUT,
                headers=headers,
                allow_redirects=False,
            )
            if resp.is_redirect or resp.is_permanent_redirect:
                location = resp.headers.get("location", "")
                resp.close()
                if not location:
                    return None
                current_url = urljoin(current_url, location)
                continue
            break
        if resp is None or resp.is_redirect or resp.is_permanent_redirect:
            return None
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        if not ct.startswith("video/"):
            return None
        size = int(resp.headers.get("content-length", 0))
        if size > VIDEO_DOWNLOAD_MAX_MB * 1024 * 1024:
            return None
        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=8192):
            chunks.append(chunk)
            total += len(chunk)
            if total > VIDEO_DOWNLOAD_MAX_MB * 1024 * 1024:
                return None
        return b"".join(chunks), ct.split(";", 1)[0].strip().lower()
    except Exception:
        return None
    finally:
        if resp is not None:
            resp.close()


def _bilibili_bvid(url: str) -> str:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    if not (host == "bilibili.com" or host.endswith(".bilibili.com")):
        return ""
    match = re.search(r"/(BV[0-9A-Za-z]{10})(?:[/?#]|$)", parsed.path + "/")
    return match.group(1) if match else ""


def _try_bilibili_api_download(url: str) -> tuple[bytes, str] | None:
    """Resolve a public Bilibili page when its anti-bot page blocks yt-dlp."""
    bvid = _bilibili_bvid(url)
    if not bvid:
        return None
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
    }
    try:
        page_response = requests.get(
            "https://api.bilibili.com/x/player/pagelist",
            params={"bvid": bvid},
            headers=headers,
            timeout=(5, 15),
        )
        page_response.raise_for_status()
        pages = page_response.json().get("data", [])
        cid = pages[0].get("cid") if isinstance(pages, list) and pages else None
        if not cid:
            return None
        play_response = requests.get(
            "https://api.bilibili.com/x/player/playurl",
            params={
                "bvid": bvid,
                "cid": cid,
                "qn": 16,
                "fnval": 0,
                "fnver": 0,
                "fourk": 0,
            },
            headers=headers,
            timeout=(5, 15),
        )
        play_response.raise_for_status()
        candidates = play_response.json().get("data", {}).get("durl", [])
        for candidate in candidates if isinstance(candidates, list) else []:
            media_url = str(candidate.get("url") or "")
            declared_size = int(candidate.get("size") or 0)
            if declared_size > VIDEO_DOWNLOAD_MAX_MB * 1024 * 1024:
                continue
            result = _try_direct_download(
                media_url,
                {"Referer": f"https://www.bilibili.com/video/{bvid}"},
            )
            if result:
                return result
    except (requests.RequestException, ValueError, TypeError, IndexError, json.JSONDecodeError):
        return None
    return None


def _try_ytdlp_download(url: str) -> tuple[bytes, str] | None:
    """Use yt-dlp to extract and download a video. Returns (bytes, mime_type) or None."""
    try:
        if not _is_safe_video_url(url) or not _is_supported_video_page(url):
            return None
        import tempfile
        import yt_dlp

        with tempfile.TemporaryDirectory() as tmpdir:
            outtmpl = str(Path(tmpdir) / "%(id)s.%(ext)s")
            ydl_opts = {
                "outtmpl": outtmpl,
                "format": "worst[ext=mp4][filesize<50M]/worst[filesize<50M]/worst",
                "max_filesize": VIDEO_DOWNLOAD_MAX_MB * 1024 * 1024,
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "extract_flat": False,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info is None:
                    return None
                video_path = Path(outtmpl) if Path(outtmpl).exists() else None
                if video_path is None:
                    # find downloaded file
                    for f in Path(tmpdir).glob("*"):
                        if f.suffix in {".mp4", ".webm", ".mkv", ".mov"}:
                            video_path = f
                            break
                if video_path is None:
                    return None
                data = video_path.read_bytes()
                ext = video_path.suffix.lower()
                mime_map = {".mp4": "video/mp4", ".webm": "video/webm",
                            ".mkv": "video/x-matroska", ".mov": "video/quicktime"}
                mime = mime_map.get(ext, "video/mp4")
                if len(data) > VIDEO_DOWNLOAD_MAX_MB * 1024 * 1024:
                    return None
                return data, mime
    except Exception:
        return None


@app.post("/v1/videos/download")
async def download_video(request: Request):
    try:
        body = await request.json()
        url = str(body.get("url", "")).strip()
        if not _is_safe_video_url(url):
            return JSONResponse(status_code=400, content={"error": {"message": "无效的URL"}})
    except (json.JSONDecodeError, ValueError):
        return JSONResponse(status_code=400, content={"error": {"message": "无效请求"}})

    # 1. Try direct download
    result = await asyncio.to_thread(_try_direct_download, url)
    if result:
        data, mime = result
        return {
            "b64_json": base64.b64encode(data).decode("ascii"),
            "mime_type": mime,
            "source": "direct",
            "size": len(data),
        }

    # 2. Bilibili's public low-resolution play URL bypasses anti-bot page 412s.
    result = await asyncio.to_thread(_try_bilibili_api_download, url)
    if result:
        data, mime = result
        return {
            "b64_json": base64.b64encode(data).decode("ascii"),
            "mime_type": mime,
            "source": "bilibili",
            "size": len(data),
        }

    # 3. Try yt-dlp for other supported platforms
    result = await asyncio.to_thread(_try_ytdlp_download, url)
    if result:
        data, mime = result
        return {
            "b64_json": base64.b64encode(data).decode("ascii"),
            "mime_type": mime,
            "source": "yt-dlp",
            "size": len(data),
        }

    return JSONResponse(
        status_code=502,
        content={"error": {"message": "无法下载该视频（可能太大、需要登录、或平台限制）"}},
    )


# ═══════════════════════════════════════════════════════════════
# Invite Web UI — local website for Pro invite code management
# ═══════════════════════════════════════════════════════════════

INVITE_DATA_DIR = ROOT / "astrbot" / "data" / "plugin_data" / "xiaoning_pro"
INVITE_DB = INVITE_DATA_DIR / "pro_members.db"
INVITE_KEY_FILE = INVITE_DB.with_suffix(".key")
INVITE_FILE = INVITE_DATA_DIR / "invites.json"
INVITE_EXPIRE_HOURS = 72
INVITE_CODE_PREFIX = "XIAONING-"
INVITE_ACCESS_PASSWORD = (
    os.environ.get("INVITE_ACCESS_PASSWORD")
    or os.environ.get("XIAONING_PRO_PASSPHRASE", "")
)


def _load_invite_signing_key() -> bytes | None:
    """Load or generate the HMAC signing key for invites."""
    configured = os.environ.get("XIAONING_PRO_SIGNING_KEY")
    if configured:
        key = configured.encode("utf-8")
        return key if len(key) >= 16 else None
    try:
        return INVITE_KEY_FILE.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _invite_sign(
    code: str,
    target_qq: str,
    tier: str,
    days: int,
    expires_at: float,
    used: bool,
) -> str:
    key = _load_invite_signing_key()
    if key is None:
        return ""
    payload = (
        f"{code}|{target_qq}|{tier}|{days}|{expires_at:.6f}|{int(bool(used))}"
    ).encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _load_invites() -> dict:
    try:
        return json.loads(INVITE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"codes": {}}


def _save_invites(data: dict) -> None:
    INVITE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = INVITE_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(INVITE_FILE)


@contextmanager
def _invite_file_guard():
    INVITE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(INVITE_FILE.with_suffix(".lock"), "a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


INVITE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>小柠 Pro 邀请码管理</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font: 14px/1.6 system-ui, sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; display: flex; justify-content: center; padding: 40px 16px; }
main { width: 100%; max-width: 720px; }
h1 { font-size: 20px; margin-bottom: 24px; color: #f8fafc; }
h1 span { color: #38bdf8; }
.card { background: #1e293b; border: 1px solid #334155; border-radius: 10px; padding: 24px; margin-bottom: 20px; }
label { display: block; font-size: 13px; color: #94a3b8; margin-bottom: 4px; }
input, select { width: 100%; padding: 10px 12px; border: 1px solid #475569; border-radius: 6px; background: #0f172a; color: #e2e8f0; font-size: 14px; margin-bottom: 12px; }
input:focus, select:focus { outline: none; border-color: #38bdf8; }
.row { display: flex; gap: 12px; }
.row > * { flex: 1; }
.btn { display: inline-flex; align-items: center; justify-content: center; padding: 10px 20px; border: none; border-radius: 6px; font-size: 14px; font-weight: 600; cursor: pointer; transition: .15s; }
.btn-primary { background: #0284c7; color: #fff; width: 100%; }
.btn-primary:hover { background: #0369a1; }
.btn-sm { padding: 5px 12px; font-size: 12px; border-radius: 4px; }
.btn-danger { background: #b91c1c; color: #fff; }
.btn-danger:hover { background: #991b1b; }
.result { margin-top: 12px; padding: 14px; border-radius: 6px; font-size: 13px; display: none; }
.result.success { background: #14532d; border: 1px solid #16a34a; display: block; }
.result.error { background: #7f1d1d; border: 1px solid #dc2626; display: block; }
.result .code { font: bold 20px monospace; color: #4ade80; margin: 6px 0; user-select: all; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: #94a3b8; font-weight: 500; padding: 8px 10px; border-bottom: 1px solid #334155; }
td { padding: 8px 10px; border-bottom: 1px solid #1e293b; }
.status-badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
.status-active { background: #14532d; color: #4ade80; }
.status-used { background: #713f12; color: #fbbf24; }
.status-expired { background: #7f1d1d; color: #fca5a5; }
#auth-overlay { position: fixed; inset: 0; background: #0f172a; display: flex; align-items: center; justify-content: center; z-index: 999; }
#auth-overlay form { background: #1e293b; padding: 32px; border-radius: 10px; width: 320px; }
#auth-overlay h2 { margin-bottom: 16px; font-size: 16px; }
</style>
</head>
<body>
<div id="auth-overlay">
  <form onsubmit="unlock(event)">
    <h2>🔐 输入访问密码</h2>
    <input type="password" id="pwd" placeholder="密码" autofocus>
    <button type="submit" class="btn btn-primary">解锁</button>
  </form>
</div>
<main id="app" style="display:none">
  <h1>🎫 小柠 <span>Pro</span> 邀请码管理</h1>

  <div class="card">
    <h2 style="font-size:15px;margin-bottom:16px;color:#f8fafc">生成邀请码</h2>
    <form onsubmit="generate(event)">
      <div class="row">
        <div><label>目标 QQ 号</label><input type="text" id="qq" placeholder="例如 123456789" required></div>
        <div><label>等级</label><select id="tier"><option value="go">GO</option><option value="pro" selected>PRO</option></select></div>
        <div><label>天数</label><input type="number" id="days" value="30" min="1" max="365" required></div>
      </div>
      <button type="submit" class="btn btn-primary" id="gen-btn">生成邀请码</button>
    </form>
    <div class="result" id="gen-result"></div>
  </div>

  <div class="card">
    <h2 style="font-size:15px;margin-bottom:16px;color:#f8fafc">邀请码列表 <button class="btn btn-sm btn-primary" onclick="refresh()" style="margin-left:8px">刷新</button></h2>
    <div id="invite-list"><p style="color:#94a3b8">加载中…</p></div>
  </div>
</main>
<script>
let password = "";
async function api(path, body) {
  const r = await fetch(path, { method: "POST", headers: {"Content-Type":"application/json","X-Invite-Auth":password}, body: JSON.stringify(body) });
  return r.json();
}
function unlock(e) {
  e.preventDefault();
  password = document.getElementById("pwd").value;
  api("/invite/api/list", {}).then(d => {
    if (d.error) { alert("密码错误"); return; }
    document.getElementById("auth-overlay").style.display = "none";
    document.getElementById("app").style.display = "block";
    renderList(d);
  });
}
async function generate(e) {
  e.preventDefault();
  const btn = document.getElementById("gen-btn");
  btn.disabled = true; btn.textContent = "生成中…";
  const d = await api("/invite/api/generate", {
    target_qq: document.getElementById("qq").value.trim(),
    tier: document.getElementById("tier").value,
    days: parseInt(document.getElementById("days").value)
  });
  const r = document.getElementById("gen-result");
  r.className = "result";
  if (d.error) { r.className = "result error"; r.textContent = d.error; }
  else {
    r.className = "result success";
    r.innerHTML = `邀请码：<div class="code">${d.code}</div>目标 QQ: ${d.target_qq} | 等级: ${d.tier.toUpperCase()} | ${d.days} 天<br>有效期至: ${d.expires_str} (72 小时)<br><br>发送给用户：<code>/redeem ${d.code}</code>`;
    refresh();
  }
  btn.disabled = false; btn.textContent = "生成邀请码";
}
async function refresh() {
  const d = await api("/invite/api/list", {});
  renderList(d);
}
function renderList(d) {
  const el = document.getElementById("invite-list");
  const codes = d.codes || {};
  const entries = Object.entries(codes).sort((a,b) => b[1].created_at - a[1].created_at);
  if (!entries.length) { el.innerHTML = '<p style="color:#94a3b8">暂无邀请码</p>'; return; }
  let html = '<table><tr><th>邀请码</th><th>QQ</th><th>等级</th><th>天数</th><th>状态</th><th>过期时间</th><th></th></tr>';
  for (const [code, e] of entries) {
    let status, cls;
    if (e.used) { status = "已使用"; cls = "status-used"; }
    else if (Date.now()/1000 > e.expires_at) { status = "已过期"; cls = "status-expired"; }
    else { status = "有效"; cls = "status-active"; }
    const expires = new Date(e.expires_at * 1000).toLocaleString("zh-CN");
    html += `<tr>
      <td style="font-family:monospace;font-weight:600">${code}</td>
      <td>${e.target_qq}</td>
      <td>${e.tier.toUpperCase()}</td>
      <td>${e.days}</td>
      <td><span class="status-badge ${cls}">${status}</span></td>
      <td style="font-size:11px;color:#94a3b8">${expires}</td>
      <td>${!e.used ? `<button class="btn btn-sm btn-danger" onclick="revoke('${code}')">撤销</button>` : ""}</td>
    </tr>`;
  }
  html += '</table>';
  el.innerHTML = html;
}
async function revoke(code) {
  if (!confirm(`确定撤销 ${code}？`)) return;
  await api("/invite/api/revoke", {code});
  refresh();
}
</script>
</body>
</html>"""


@app.get("/invite", response_class=HTMLResponse)
async def invite_page():
    return HTMLResponse(content=INVITE_HTML)


def _check_invite_auth(request: Request) -> bool:
    token = request.headers.get("x-invite-auth", "")
    return bool(INVITE_ACCESS_PASSWORD) and hmac.compare_digest(
        token, INVITE_ACCESS_PASSWORD
    )


@app.post("/invite/api/generate")
async def invite_api_generate(request: Request):
    if not _check_invite_auth(request):
        return JSONResponse(status_code=403, content={"error": "密码错误"})
    try:
        body = await request.json()
        target_qq = str(body.get("target_qq", "")).strip()
        tier = str(body.get("tier", "pro")).lower()
        days = int(body.get("days", 30))
    except (json.JSONDecodeError, ValueError):
        return JSONResponse(status_code=400, content={"error": "无效的请求参数"})

    if not target_qq.isdigit() or len(target_qq) < 5:
        return JSONResponse(status_code=400, content={"error": "无效的 QQ 号"})
    if tier not in ("go", "pro"):
        return JSONResponse(status_code=400, content={"error": "等级必须是 go 或 pro"})
    max_days = 90 if tier == "go" else 365
    if not (1 <= days <= max_days):
        return JSONResponse(
            status_code=400,
            content={"error": f"{tier.upper()} 天数需在 1-{max_days} 之间"},
        )

    now = time.time()
    with _invite_file_guard():
        store = _load_invites()
        codes = store.setdefault("codes", {})
        code = f"{INVITE_CODE_PREFIX}{secrets.token_hex(4).upper()}"
        while code in codes:
            code = f"{INVITE_CODE_PREFIX}{secrets.token_hex(4).upper()}"
        expires_at = now + INVITE_EXPIRE_HOURS * 3600

        entry = {
            "target_qq": target_qq,
            "tier": tier,
            "days": days,
            "created_at": now,
            "expires_at": expires_at,
            "used": False,
        }
        entry["_sig"] = _invite_sign(code, target_qq, tier, days, expires_at, False)
        if not entry["_sig"]:
            return JSONResponse(
                status_code=503,
                content={"error": "邀请码签名密钥不可用，请先启动 Pro 服务"},
            )

        codes[code] = entry
        _save_invites(store)

    logger.info("[InviteWeb] Created invite tier=%s days=%s", tier, days)
    return {
        "ok": True,
        "code": code,
        "target_qq": target_qq,
        "tier": tier,
        "days": days,
        "expires_at": expires_at,
        "expires_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expires_at)),
    }


@app.post("/invite/api/list")
async def invite_api_list(request: Request):
    if not _check_invite_auth(request):
        return JSONResponse(status_code=403, content={"error": "密码错误"})
    with _invite_file_guard():
        store = _load_invites()
    return {"codes": store.get("codes", {})}


@app.post("/invite/api/revoke")
async def invite_api_revoke(request: Request):
    if not _check_invite_auth(request):
        return JSONResponse(status_code=403, content={"error": "密码错误"})
    try:
        body = await request.json()
        code = str(body.get("code", "")).strip().upper()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse(status_code=400, content={"error": "无效的请求参数"})

    with _invite_file_guard():
        store = _load_invites()
        if code not in store.get("codes", {}):
            return JSONResponse(status_code=404, content={"error": "邀请码不存在"})
        if store["codes"][code].get("used"):
            return JSONResponse(status_code=400, content={"error": "该邀请码已被使用，无法撤销"})

        entry = store["codes"][code]
        entry["used"] = True
        entry["used_at"] = time.time()
        entry["_sig"] = _invite_sign(
            code,
            entry["target_qq"],
            entry["tier"],
            entry["days"],
            entry["expires_at"],
            True,
        )
        _save_invites(store)
    logger.info("[InviteWeb] Revoked invite")
    return {"ok": True}


# ── Unified search endpoint ────────────────────────────────────────
_SEARCH_CACHE: dict[str, tuple[float, dict]] = {}
_SEARCH_CACHE_TTL = 300  # 5 min TTL for repeated queries
_SEARCH_CACHE_MAX = 200

_NETEASE_SEARCH_URL = "https://music.163.com/api/search/get"


def _search_netease_song_cached(query: str) -> dict | None:
    """Search NetEase music with cache."""
    cache_key = f"netease:{query}"
    now = time.time()
    if cache_key in _SEARCH_CACHE:
        ts, result = _SEARCH_CACHE[cache_key]
        if now - ts < _SEARCH_CACHE_TTL:
            return result
    try:
        resp = requests.get(
            _NETEASE_SEARCH_URL,
            params={"s": query, "type": 1, "limit": 5, "offset": 0},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/"},
            timeout=(5, 15),
        )
        resp.raise_for_status()
        songs = (resp.json().get("result") or {}).get("songs") or []
        song = next((s for s in songs if isinstance(s, dict) and str(s.get("id", "")).isdigit()), None)
        if not song:
            return None
        artists = song.get("artists") or []
        result = {
            "song_id": str(song["id"]),
            "title": str(song.get("name") or query),
            "artist": "/".join(
                str(a.get("name")) for a in artists if isinstance(a, dict) and a.get("name")
            ),
        }
    except Exception:
        return None
    # LRU eviction
    if len(_SEARCH_CACHE) >= _SEARCH_CACHE_MAX:
        oldest = min(_SEARCH_CACHE, key=lambda k: _SEARCH_CACHE[k][0], default=None)
        if oldest:
            _SEARCH_CACHE.pop(oldest, None)
    _SEARCH_CACHE[cache_key] = (now, result)
    return result


def _search_bilibili_cached(query: str, limit: int = 5) -> list[dict]:
    """Search Bilibili public videos with cache."""
    cache_key = f"bili:{query}:{limit}"
    now = time.time()
    if cache_key in _SEARCH_CACHE:
        ts, result = _SEARCH_CACHE[cache_key]
        if now - ts < _SEARCH_CACHE_TTL:
            return result
    try:
        resp = requests.get(
            "https://api.bilibili.com/x/web-interface/search/type",
            params={"search_type": "video", "keyword": query, "page": 1},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://search.bilibili.com/"},
            timeout=(5, 15),
        )
        resp.raise_for_status()
        items = resp.json().get("data", {}).get("result", [])
        results: list[dict] = []
        for item in items[:limit]:
            if not isinstance(item, dict):
                continue
            bvid = str(item.get("bvid") or "").strip()
            if not bvid:
                continue
            title = re.sub(r"<[^>]+>", "", str(item.get("title") or "")).strip()
            results.append({
                "title": title or bvid,
                "url": f"https://www.bilibili.com/video/{bvid}",
                "bvid": bvid,
                "platform": "bilibili",
            })
    except Exception:
        return []
    if len(_SEARCH_CACHE) >= _SEARCH_CACHE_MAX:
        oldest = min(_SEARCH_CACHE, key=lambda k: _SEARCH_CACHE[k][0], default=None)
        if oldest:
            _SEARCH_CACHE.pop(oldest, None)
    _SEARCH_CACHE[cache_key] = (now, results)
    return results


@app.post("/v1/search")
async def unified_search(request: Request):
    """Unified search: web, music, video. Reduces per-plugin search implementations."""
    try:
        body = await request.json()
        query = str(body.get("query", "")).strip()
        search_type = str(body.get("type", "web")).strip().lower()
        limit = min(max(int(body.get("limit", 5)), 1), 10)
    except (json.JSONDecodeError, ValueError, TypeError):
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "无效的搜索请求", "type": "invalid_request_error"}},
        )
    if not query or len(query) > 500:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "搜索词需在 1-500 字符", "type": "invalid_request_error"}},
        )

    result: dict = {"query": query, "type": search_type}

    # Music search
    if search_type in ("music", "all"):
        song = _search_netease_song_cached(query)
        if song:
            result["music"] = song

    # Video search (Bilibili public API)
    if search_type in ("video", "all"):
        videos = _search_bilibili_cached(query, limit)
        if videos:
            result["videos"] = videos

    # Web search via Gemini grounding
    if search_type in ("web", "all"):
        try:
            client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
            search_tool = types.Tool(google_search=types.GoogleSearch())
            response = client.models.generate_content(
                model="gemini-3.5-flash",
                contents=f"搜索以下内容并给出简洁中文摘要（≤300字），附带来源链接：{query}",
                config=types.GenerateContentConfig(
                    tools=[search_tool],
                    max_output_tokens=600,
                    temperature=0.3,
                ),
            )
            web_text = str(getattr(response, "text", "") or "").strip()
            if web_text:
                result["web_summary"] = web_text[:600]
            grounding = getattr(response, "candidates", None)
            if grounding:
                sources = []
                for c in grounding:
                    for src in (getattr(getattr(c, "grounding_metadata", None), "grounding_chunks", None) or []):
                        src_web = getattr(src, "web", None)
                        uri = getattr(src_web, "uri", "") if src_web else ""
                        title = getattr(src_web, "title", "") if src_web else ""
                        if uri:
                            sources.append({"title": title, "uri": uri})
                if sources:
                    result["web_sources"] = sources[:5]
        except Exception:
            logger.debug("Unified search web grounding failed for: %s", query)

    # Don't return empty-handed
    if len(result) <= 2:  # only query + type
        return JSONResponse(
            status_code=404,
            content={"error": {"message": f"未找到「{query}」的相关结果", "type": "not_found"}},
        )
    result["created"] = int(time.time())
    return result


def _configure_logging() -> None:
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        ROOT / "gemini-proxy-runtime.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.propagate = True


if __name__ == "__main__":
    _configure_logging()
    logger.info(
        "starting proxy primary=%s image=%s location=%s",
        "gemini-3.5-flash",
        IMAGE_MODEL_PRIMARY,
        LOCATION,
    )
    uvicorn.run(app, host="127.0.0.1", port=3000, log_level="info")
