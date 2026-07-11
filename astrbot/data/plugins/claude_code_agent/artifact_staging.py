"""Private staging rules for file-producing Agent tasks."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

try:
    from .delivery_dlp import is_sensitive_name
except ImportError:  # Direct module loading in unit tests.
    from delivery_dlp import is_sensitive_name


_CODE_OR_PROJECT = re.compile(
    r"代码|项目|仓库|测试|构建|编译|修复|重构|python|typescript|next\.js", re.I
)
_WORD = re.compile(r"\bdocx\b|\bword\b|Word|文档", re.I)
_IMAGE = re.compile(r"作图|画图|绘图|图片|海报|插画|封面|logo|图像", re.I)
_PRESENTATION = re.compile(r"\bpptx?\b|幻灯片|演示文稿", re.I)
_SHEET = re.compile(r"\bxlsx?\b|电子表格|工作簿", re.I)
_PDF = re.compile(r"\bpdf\b", re.I)
_EXPLICIT_SUFFIX = re.compile(r"(?i)(?<![\w.])[^\s/\\]+(\.[a-z0-9]{1,8})\b")

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def expected_artifact_suffixes(task: str) -> set[str]:
    text = str(task or "")
    explicit = {match.group(1).lower() for match in _EXPLICIT_SUFFIX.finditer(text)}
    if explicit:
        return explicit
    if _WORD.search(text):
        return {".docx"}
    if _IMAGE.search(text):
        return set(IMAGE_SUFFIXES)
    if _PRESENTATION.search(text):
        return {".pptx"}
    if _SHEET.search(text):
        return {".xlsx"}
    if _PDF.search(text):
        return {".pdf"}
    return set()


def select_execution_dir(task: str, work_dir: Path, job_dir: Path) -> Path:
    if expected_artifact_suffixes(task) and not _CODE_OR_PROJECT.search(str(task or "")):
        return Path(job_dir).resolve(strict=True)
    return Path(work_dir).resolve(strict=True)


def collect_staged_artifacts(
    job_dir: Path,
    output_dir: Path,
    expected_suffixes: set[str],
    *,
    max_files: int = 20,
    max_file_bytes: int = 100 * 1024 * 1024,
) -> list[Path]:
    root = Path(job_dir).resolve(strict=True)
    output = Path(output_dir).resolve(strict=True)
    allowed = {str(suffix).lower() for suffix in expected_suffixes}
    if not allowed or output.parent != root:
        return []
    copied: list[Path] = []
    for candidate in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        if len(copied) >= max(0, int(max_files)):
            break
        if candidate.parent != root or candidate.is_symlink() or not candidate.is_file():
            continue
        if candidate.suffix.lower() not in allowed or is_sensitive_name(candidate):
            continue
        try:
            size = candidate.stat().st_size
        except OSError:
            continue
        if size <= 0 or size > max(0, int(max_file_bytes)):
            continue
        destination = output / candidate.name
        if destination.exists():
            continue
        shutil.copy2(candidate, destination)
        copied.append(destination)
    return copied
