"""Bounded local DLP checks for files before they leave the machine."""

from __future__ import annotations

import re
import os
import stat
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


MAX_TEXT_BYTES = 5 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 100
MAX_ARCHIVE_TOTAL_BYTES = 20 * 1024 * 1024

TEXT_SUFFIXES = {
    "", ".txt", ".md", ".rst", ".py", ".js", ".jsx", ".ts", ".tsx",
    ".json", ".yaml", ".yml", ".xml", ".rels", ".csv", ".log", ".ini",
    ".toml", ".html", ".css", ".sql", ".sh", ".ps1", ".bat", ".cmd",
    ".java", ".c", ".cc", ".cpp", ".h", ".hpp", ".go", ".rs",
    # Office document internals — python-pptx / python-docx / openpyxl generate these
    ".bin", ".vml", ".emf", ".wmf",
}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
VIDEO_SUFFIXES = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}
ARCHIVE_SUFFIXES = {".zip", ".docx", ".xlsx", ".pptx"}
NESTED_ARCHIVE_SUFFIXES = ARCHIVE_SUFFIXES | {".7z", ".rar", ".tar", ".gz"}
SENSITIVE_FILE_NAMES = {
    ".env", "cookies", "cookies.sqlite", "credentials.json", "id_rsa",
    "id_ed25519", "login data", "keychain.db", "secrets.json",
}
SENSITIVE_FILE_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".kdbx"}

_SENSITIVE_NAME_FRAGMENT = re.compile(
    r"api[_-]?key|token|password|secret|credential|cookie", re.I
)
_LOCAL_PATH = re.compile(
    r"(?i)(?:file:/+)?(?<![a-z])(?:[a-z]:[\\/]|\\\\)[^\s`\"<>|，。；！？）】},;!]+"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|password|secret|"
    r"cookie|credential)(?:\s*[:=]\s*)[^\s,;]{6,}"
)
_KNOWN_SECRET = re.compile(
    r"(?i)\b(?:sk-[a-z0-9_-]{16,}|gh[oprsu]_[a-z0-9]{20,}|"
    r"AIza[a-z0-9_-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)


@dataclass(frozen=True)
class DLPDecision:
    allowed: bool
    code: str


def is_sensitive_name(path: Path | PurePosixPath) -> bool:
    candidate = Path(str(path))
    name = candidate.name.lower()
    parts = {part.lower() for part in candidate.parts}
    return bool(
        name in SENSITIVE_FILE_NAMES
        or candidate.suffix.lower() in SENSITIVE_FILE_SUFFIXES
        or _SENSITIVE_NAME_FRAGMENT.search(name)
        or parts & {".ssh", ".aws", ".gnupg", "credentials", "secrets"}
    )


def _contains_sensitive_text(data: bytes) -> bool:
    if b"\x00" in data[:4096]:
        return False
    for encoding in ("utf-8", "utf-16"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        return True
    return bool(
        _LOCAL_PATH.search(text)
        or _SECRET_ASSIGNMENT.search(text)
        or _KNOWN_SECRET.search(text)
    )


def _safe_archive_member(name: str) -> PurePosixPath | None:
    normalized = str(name or "").replace("\\", "/")
    member = PurePosixPath(normalized)
    if not normalized or member.is_absolute() or ".." in member.parts:
        return None
    if any(part in {"", "."} for part in member.parts):
        return None
    return member


def _inspect_archive(path: Path) -> DLPDecision:
    try:
        if not zipfile.is_zipfile(path):
            return DLPDecision(False, "invalid_archive")
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                return DLPDecision(False, "archive_member_limit")
            total = 0
            for info in members:
                if info.is_dir():
                    continue
                member = _safe_archive_member(info.filename)
                if member is None:
                    return DLPDecision(False, "archive_path")
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    return DLPDecision(False, "archive_symlink")
                if info.flag_bits & 0x1:
                    return DLPDecision(False, "archive_encrypted")
                total += max(0, int(info.file_size))
                if info.file_size > MAX_TEXT_BYTES or total > MAX_ARCHIVE_TOTAL_BYTES:
                    return DLPDecision(False, "archive_size_limit")
                if is_sensitive_name(member):
                    return DLPDecision(False, "sensitive_name")
                suffix = member.suffix.lower()
                if suffix in NESTED_ARCHIVE_SUFFIXES:
                    return DLPDecision(False, "nested_archive")
                if suffix in IMAGE_SUFFIXES:
                    continue
                if suffix not in TEXT_SUFFIXES:
                    return DLPDecision(False, "unsupported_archive_member")
                with archive.open(info) as source:
                    data = source.read(MAX_TEXT_BYTES + 1)
                if len(data) > MAX_TEXT_BYTES:
                    return DLPDecision(False, "archive_size_limit")
                if _contains_sensitive_text(data):
                    return DLPDecision(False, "sensitive_content")
    except (OSError, ValueError, zipfile.BadZipFile):
        return DLPDecision(False, "invalid_archive")
    return DLPDecision(True, "clean")


def _resolved_inside(path: Path, root: Path) -> tuple[Path, Path] | None:
    candidate = Path(path)
    if candidate.is_symlink():
        return None
    try:
        resolved = candidate.resolve(strict=True)
        base = Path(root).resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_file() or not (resolved == base or base in resolved.parents):
        return None
    return resolved, base


def _image_has_metadata(path: Path) -> bool | None:
    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            metadata_keys = {
                "comment", "description", "exif", "icc_profile", "parameters",
                "prompt", "software", "xmp", "xml",
            }
            info_keys = {str(key).lower() for key in image.info}
            png_text = getattr(image, "text", None)
            return bool(
                image.getexif()
                or png_text
                or info_keys.intersection(metadata_keys)
            )
    except Exception:
        return None


def _is_valid_video(path: Path) -> bool:
    """Reject arbitrary binaries renamed with a supported video suffix."""
    try:
        with path.open("rb") as source:
            header = source.read(16)
    except OSError:
        return False
    suffix = path.suffix.lower()
    if suffix in {".mp4", ".mov", ".m4v"}:
        return len(header) >= 12 and header[4:8] == b"ftyp"
    if suffix in {".webm", ".mkv"}:
        return header.startswith(b"\x1a\x45\xdf\xa3")
    return False


def strip_image_metadata(path: Path, root: Path) -> bool:
    checked = _resolved_inside(path, root)
    if checked is None:
        return False
    resolved, _base = checked
    if resolved.suffix.lower() not in IMAGE_SUFFIXES:
        return False
    temporary = resolved.with_name(f".{resolved.stem}.{uuid.uuid4().hex}.tmp{resolved.suffix}")
    try:
        from PIL import Image

        with Image.open(resolved) as source:
            source.load()
            cleaned = source.copy()
            image_format = source.format
        save_options: dict[str, object] = {}
        if image_format == "JPEG":
            save_options.update(quality=95, optimize=True)
        elif image_format == "WEBP":
            save_options.update(quality=95, method=4)
        cleaned.save(temporary, format=image_format, **save_options)
        os.replace(temporary, resolved)
        return True
    except Exception:
        return False
    finally:
        temporary.unlink(missing_ok=True)


def inspect_deliverable(
    path: Path,
    root: Path,
    *,
    max_file_bytes: int = 20 * 1024 * 1024,
) -> DLPDecision:
    candidate = Path(path)
    if candidate.is_symlink():
        return DLPDecision(False, "symlink")
    checked = _resolved_inside(candidate, root)
    if checked is None:
        try:
            candidate.resolve(strict=True)
        except OSError:
            return DLPDecision(False, "missing")
        return DLPDecision(False, "outside_root")
    resolved, base = checked
    try:
        size = resolved.stat().st_size
    except OSError:
        return DLPDecision(False, "missing")
    if size <= 0 or size > max(0, int(max_file_bytes)):
        return DLPDecision(False, "file_size")
    if is_sensitive_name(resolved):
        return DLPDecision(False, "sensitive_name")
    suffix = resolved.suffix.lower()
    if suffix in ARCHIVE_SUFFIXES:
        return _inspect_archive(resolved)
    if suffix in IMAGE_SUFFIXES:
        metadata = _image_has_metadata(resolved)
        if metadata is None:
            return DLPDecision(False, "invalid_image")
        return DLPDecision(not metadata, "clean" if not metadata else "image_metadata")
    if suffix in VIDEO_SUFFIXES:
        valid = _is_valid_video(resolved)
        return DLPDecision(valid, "clean" if valid else "invalid_video")
    if suffix not in TEXT_SUFFIXES:
        return DLPDecision(False, "unsupported_type")
    try:
        data = resolved.read_bytes()
    except OSError:
        return DLPDecision(False, "unreadable")
    if len(data) > MAX_TEXT_BYTES:
        return DLPDecision(False, "file_size")
    if _contains_sensitive_text(data):
        return DLPDecision(False, "sensitive_content")
    return DLPDecision(True, "clean")
