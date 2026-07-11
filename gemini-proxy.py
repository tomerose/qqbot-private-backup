"""Small OpenAI-compatible proxy for Vertex AI Gemini."""

import base64
import json
import os
import uuid

import uvicorn
from fastapi import FastAPI, Request
from google import genai
from google.genai import types

app = FastAPI()
PROJECT = os.getenv("VERTEX_PROJECT", "solar-modem-496213-f5")
LOCATION = os.getenv("VERTEX_LOCATION", "global")
MODEL_IDS = {"gemini-2.5-flash", "gemini-2.5-pro"}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": "vertex-gemini-proxy"}


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {"id": model_id, "object": "model", "capabilities": {"vision": True, "audio": True}}
            for model_id in sorted(MODEL_IDS)
        ],
    }


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


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = json.loads((await request.body()).decode("utf-8", errors="replace"))
    model_id = body.get("model", "gemini-2.5-flash")
    if model_id not in MODEL_IDS:
        model_id = "gemini-2.5-flash"
    system_prompt, contents = _to_contents(body.get("messages", []))
    config = types.GenerateContentConfig(max_output_tokens=min(int(body.get("max_tokens", 2048)), 4096))
    if system_prompt:
        config.system_instruction = system_prompt

    try:
        client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
        response = client.models.generate_content(model=model_id, contents=contents, config=config)
        usage = response.usage_metadata
        return {
            "id": "gemini-" + uuid.uuid4().hex[:8],
            "object": "chat.completion",
            "model": model_id,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": response.text or ""}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": getattr(usage, "prompt_token_count", 0) if usage else 0,
                "completion_tokens": getattr(usage, "candidates_token_count", 0) if usage else 0,
            },
        }
    except Exception as exc:
        return {"error": {"message": str(exc), "type": "api_error"}}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=3000, log_level="info")
