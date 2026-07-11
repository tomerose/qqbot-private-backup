"""Validation and response extraction for the local Vertex image endpoint."""

from __future__ import annotations

import base64
from dataclasses import dataclass


IMAGE_MODEL_PRIMARY = "gemini-3.1-flash-image"
IMAGE_MODEL_FALLBACK = "gemini-2.5-flash-image"
IMAGE_MODELS = frozenset({IMAGE_MODEL_PRIMARY, IMAGE_MODEL_FALLBACK})
MAX_IMAGE_PROMPT_CHARS = 500
_SIZE_TO_ASPECT_RATIO = {
    "512x512": "1:1",
    "1024x1024": "1:1",
    "1024x1536": "2:3",
    "1536x1024": "3:2",
    "1024x576": "16:9",
    "576x1024": "9:16",
}


class ImageRequestError(ValueError):
    """A generic request error that does not expose proxy internals."""


@dataclass(frozen=True)
class ImageRequest:
    prompt: str
    model: str
    aspect_ratio: str


def normalize_image_request(body: object) -> ImageRequest:
    if not isinstance(body, dict):
        raise ImageRequestError("请求格式无效。")
    prompt = " ".join(str(body.get("prompt", "")).split())
    if not prompt:
        raise ImageRequestError("请提供画面描述。")
    if len(prompt) > MAX_IMAGE_PROMPT_CHARS:
        raise ImageRequestError(f"画面描述最多 {MAX_IMAGE_PROMPT_CHARS} 个字符。")
    if any(ord(char) < 32 for char in prompt):
        raise ImageRequestError("画面描述包含不支持的控制字符。")
    requested_model = str(body.get("model", "")).strip()
    model = requested_model if requested_model in IMAGE_MODELS else IMAGE_MODEL_PRIMARY
    size = str(body.get("size", "1024x1024")).lower().replace(" ", "")
    return ImageRequest(
        prompt=prompt,
        model=model,
        aspect_ratio=_SIZE_TO_ASPECT_RATIO.get(size, "1:1"),
    )


def extract_first_image_bytes(response: object) -> tuple[str, bytes] | None:
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            inline_data = getattr(part, "inline_data", None)
            mime_type = str(getattr(inline_data, "mime_type", "") or "").lower()
            data = getattr(inline_data, "data", None)
            if not mime_type.startswith("image/") or not data:
                continue
            if isinstance(data, str):
                try:
                    data = base64.b64decode(data, validate=True)
                except ValueError:
                    continue
            if isinstance(data, bytes):
                return mime_type, data
    return None
