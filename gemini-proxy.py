"""Small OpenAI-compatible proxy for Vertex AI Gemini."""

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import io
import json
import logging
import os
import re
import secrets
import socket
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from google import genai
from google.genai import types

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from image_proxy_core import (
    IMAGE_MODEL_FALLBACK,
    IMAGE_MODEL_PRIMARY,
    ImageRequestError,
    extract_first_image_bytes,
    image_model_attempts,
    normalize_image_request,
)

app = FastAPI()
PROJECT = os.getenv("VERTEX_PROJECT", "solar-modem-496213-f5")
LOCATION = os.getenv("VERTEX_LOCATION", "global")
MODEL_IDS = {"gemini-2.5-flash", "gemini-2.5-pro"}
SEARCH_MODEL_ALIAS = "gemini-2.5-flash-search"
MUSIC_MODEL = "lyria-3-clip-preview"
MAX_MUSIC_BYTES = 20 * 1024 * 1024
logger = logging.getLogger("vertex-gemini-proxy")


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
        ]
        + [
            {"id": SEARCH_MODEL_ALIAS, "object": "model", "capabilities": {"vision": True, "audio": True, "search": True}}
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
    use_search = False
    if model_id == SEARCH_MODEL_ALIAS:
        model_id = "gemini-2.5-flash"
        use_search = True
    if model_id not in MODEL_IDS:
        model_id = "gemini-2.5-flash"
    # ponytail: google_search flag via custom_extra_body or top-level param
    if not use_search:
        use_search = bool(body.get("google_search") or (body.get("custom_extra_body") or {}).get("google_search"))
    system_prompt, contents = _to_contents(body.get("messages", []))
    tools = []
    if use_search:
        tools.append(types.Tool(google_search=types.GoogleSearch()))
    config = types.GenerateContentConfig(
        max_output_tokens=min(int(body.get("max_tokens", 2048)), 4096),
        tools=tools if tools else None,
    )
    if system_prompt:
        config.system_instruction = system_prompt

    try:
        client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
        response = client.models.generate_content(model=model_id, contents=contents, config=config)
        usage = response.usage_metadata
        result = {
            "id": "gemini-" + uuid.uuid4().hex[:8],
            "object": "chat.completion",
            "model": model_id,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": response.text or ""}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": getattr(usage, "prompt_token_count", 0) if usage else 0,
                "completion_tokens": getattr(usage, "candidates_token_count", 0) if usage else 0,
            },
        }
        # ponytail: attach grounding metadata when search was used
        if use_search and hasattr(response, "candidates") and response.candidates:
            candidate = response.candidates[0]
            if hasattr(candidate, "grounding_metadata") and candidate.grounding_metadata:
                gm = candidate.grounding_metadata
                sources = []
                if hasattr(gm, "grounding_chunks"):
                    for chunk in gm.grounding_chunks:
                        if hasattr(chunk, "web") and chunk.web:
                            sources.append({"title": getattr(chunk.web, "title", ""), "uri": getattr(chunk.web, "uri", "")})
                result["grounding"] = {
                    "sources": sources,
                    "search_queries": list(getattr(gm, "web_search_queries", []) or []),
                }
        return result
    except Exception as exc:
        logger.exception("chat completion failed")
        return JSONResponse(
            status_code=502,
            content={"error": {"message": str(exc), "type": "api_error"}},
        )


def _generate_music(prompt: str) -> tuple[bytes, str]:
    """Generate one original Lyria clip through the existing Vertex AI identity."""
    client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
    response = client.interactions.create(
        model=MUSIC_MODEL,
        input=[{"type": "text", "text": prompt}],
    )
    audio = next(
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


@app.post("/v1/images/generations")
async def generate_image(request: Request):
    try:
        payload = normalize_image_request(await request.json())
    except (ImageRequestError, ValueError, json.JSONDecodeError):
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "无效的作图请求。", "type": "invalid_request_error"}},
        )

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
                        image_size="1K",
                    ),
                ),
            )
            image = extract_first_image_bytes(response)
            if image is None:
                logger.warning("image model %s returned no inline image", model_id)
                continue
            mime_type, image_bytes = image
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
        except Exception:
            logger.exception("image generation failed for model %s", model_id)
    return JSONResponse(
        status_code=502,
        content={"error": {"message": "作图服务暂时不可用，请稍后再试。", "type": "api_error"}},
    )


VIDEO_MODEL = "veo-3.1-lite-generate-001"
VIDEO_DURATION = 4
VIDEO_ASPECT_RATIO = "16:9"
VIDEO_POLL_SECONDS = 10
VIDEO_MAX_POLL = 48  # 8 min max wait (Lite ~2-4 min, Standard ~5-8 min)
GIF_FRAMES = 4
GIF_FRAME_DELAY = 300  # ms per frame


def _generate_gif_frames(client, prompt: str, aspect: str, n: int) -> list[bytes]:
    """Generate N frame images via Gemini, return list of PNG bytes."""
    size = "1024x1024" if aspect == "1:1" else "1280x720"
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
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash-image",
                contents=frame_prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["TEXT", "IMAGE"],
                    image_config=types.ImageConfig(aspect_ratio=aspect, image_size="1K"),
                ),
            )
            img = extract_first_image_bytes(response)
            if img is None:
                logger.warning("GIF frame %d returned no image, retrying", i)
                # Retry once with simpler prompt
                response = client.models.generate_content(
                    model="gemini-2.5-flash-image",
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_modalities=["TEXT", "IMAGE"],
                        image_config=types.ImageConfig(aspect_ratio=aspect, image_size="1K"),
                    ),
                )
                img = extract_first_image_bytes(response)
            if img:
                frames.append(img[1])  # img is (mime_type, bytes)
        except Exception:
            logger.exception("GIF frame %d generation failed", i)
    return frames


def _generate_video_sync(prompt: str, model: str, duration: int, aspect: str):
    """Run the blocking Vertex polling and GIF fallback outside FastAPI's loop."""
    client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)

    # ── Try Veo first ──
    try:
        operation = client.models.generate_videos(
            model=model,
            prompt=prompt,
            config=types.GenerateVideosConfig(
                aspect_ratio=aspect,
                duration_seconds=duration,
                enhance_prompt=True,
            ),
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
                       "mime_type": "image/gif", "model": "gemini-2.5-flash-image-gif",
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


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=3000, log_level="info")
