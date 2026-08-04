"""Explicit birthday parsing and Google Lyria birthday-song generation."""

from __future__ import annotations

import base64
import re
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import requests


MUSIC_PROXY_URL = "http://127.0.0.1:3000/v1/music/generations"
MAX_SONG_BYTES = 20 * 1024 * 1024
_EXPLICIT_BIRTHDAY = re.compile(
    r"(?:我|本人)(?:的)?生日(?:是|在|为)?\s*"
    r"(?P<month>0?[1-9]|1[0-2])\s*月\s*(?P<day>0?[1-9]|[12]\d|3[01])(?!\d)\s*(?:日|号)?"
)
_TODAY_BIRTHDAY = re.compile(r"(?:今天|今日).{0,6}(?:是)?(?:我|本人)(?:的)?生日")


@dataclass(frozen=True)
class Birthday:
    month: int
    day: int


def parse_explicit_birthday(text: object, today: date | None = None) -> Birthday | None:
    """Accept only a user's explicit solar-calendar birthday statement."""
    raw = str(text or "").strip()
    if not raw or "生日" not in raw or "农历" in raw or "阴历" in raw:
        return None
    current = today or date.today()
    if _TODAY_BIRTHDAY.search(raw):
        return Birthday(current.month, current.day)
    match = _EXPLICIT_BIRTHDAY.search(raw)
    if not match:
        return None
    birthday = Birthday(int(match.group("month")), int(match.group("day")))
    try:
        date(2000, birthday.month, birthday.day)
    except ValueError:
        return None
    return birthday


def is_due_birthday(data: dict, today: date) -> bool:
    """Return true when a stored birthday is due today and not greeted this year."""
    try:
        return (
            int(data.get("month", 0)) == today.month
            and int(data.get("day", 0)) == today.day
            and int(data.get("last_greeted_year", 0)) != today.year
        )
    except (TypeError, ValueError):
        return False


def birthday_greeting(display_name: object = "") -> str:
    name = str(display_name or "").strip()[:30]
    prefix = f"{name}，" if name else ""
    return f"{prefix}生日快乐！今天把快乐和偏爱都收好，愿新的一岁有想要的答案，也有自在的日子。"


def generate_birthday_song(output_root: Path) -> Path:
    """Generate one short, original birthday song through the existing Lyria proxy."""
    response = requests.post(
        MUSIC_PROXY_URL,
        json={
            "prompt": (
                "A short original Chinese birthday song, warm and bright, "
                "gentle celebratory melody, no imitation of any existing song."
            )
        },
        timeout=(30, 300),
    )
    response.raise_for_status()
    data = response.json().get("data", [])
    if not data or not isinstance(data[0].get("b64_json"), str):
        raise ValueError("missing birthday music response")
    payload = base64.b64decode(data[0]["b64_json"], validate=True)
    if not payload or len(payload) > MAX_SONG_BYTES:
        raise ValueError("invalid birthday music payload")
    mime = str(data[0].get("mime_type") or "audio/mpeg").lower()
    output_root.mkdir(parents=True, exist_ok=True)
    suffix = ".wav" if "wav" in mime else ".mp3"
    path = output_root / f"birthday-song-{uuid.uuid4().hex}{suffix}"
    path.write_bytes(payload)
    return path
