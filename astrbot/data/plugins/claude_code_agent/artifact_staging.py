"""Private staging rules for file-producing Agent tasks."""

from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

try:
    from .delivery_dlp import is_sensitive_name
except ImportError:  # Direct module loading in unit tests.
    from delivery_dlp import is_sensitive_name


_CODE_OR_PROJECT = re.compile(
    r"代码|测试|构建|编译|修复|重构|python|typescript|next\.js", re.I
)
_WORD = re.compile(r"(?<![a-z])(?:docx|word)(?![a-z])|文档|报告|总结|文章|论文|笔记|方案|说明", re.I)
_IMAGE = re.compile(r"作图|画图|绘图|图片|海报|插画|封面|logo|图像", re.I)
_PRESENTATION = re.compile(r"(?<![a-z])pptx?(?![a-z])|powerpoint|幻灯片|演示文稿", re.I)
_SHEET = re.compile(r"(?<![a-z])(?:xlsx?|excel)(?![a-z])|电子表格|工作簿|表格", re.I)
_PDF = re.compile(r"(?<![a-z])pdf(?![a-z])", re.I)
# ponytail: broad catch-all — any creation verb + any noun = artifact intent
_GENERIC_ARTIFACT = re.compile(
    r"(?:生成|创建|制作|导出|编写|撰写|整理|做|写|弄|搞|整)\s*(?:[了个]|一份?|一个)?\s*"
    r"(?:文件|报告|总结|文档|笔记|文章|表格|图表|清单|方案|说明|资料|产出|网页|网站|页面|"
    r"[a-z]+\.[a-z0-9]+)",
    re.I,
)
_ARTIFACT_ACTION = re.compile(
    r"生成|创建|制作|导出|编写|撰写|产出|"
    r"整理\s*(?:成|为|一份|一个)|[做写弄搞整]\s*(?:[了个]|成|一份|一个)",
    re.I,
)
_EXPLICIT_SUFFIX = re.compile(r"(?i)(?<![\w.])[^\s/\\]+(\.[a-z0-9]{1,8})\b")

_VIDEO = re.compile(r"\u89c6\u9891|\u77ed\u7247|\u52a8\u753b|video|vid", re.I)
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_SUFFIXES = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}
WEB_SUFFIXES = {".html", ".htm", ".css", ".js", ".json", ".xml", ".svg"}
SUPPORTED_ARTIFACT_SUFFIXES = IMAGE_SUFFIXES | VIDEO_SUFFIXES | WEB_SUFFIXES | {
    ".docx", ".pdf", ".pptx", ".xlsx", ".txt", ".md", ".csv", ".zip"
}


def expected_artifact_suffixes(task: str) -> set[str]:
    text = str(task or "")
    explicit = {
        match.group(1).lower()
        for match in _EXPLICIT_SUFFIX.finditer(text)
        if match.group(1).lower() in SUPPORTED_ARTIFACT_SUFFIXES
    }
    if explicit:
        return explicit
    # Specific formats must win over broad words such as “报告” or “文档”.
    if _PRESENTATION.search(text):
        return {".pptx"}
    if _SHEET.search(text):
        return {".xlsx"}
    if _PDF.search(text):
        return {".pdf"}
    if _IMAGE.search(text):
        return set(IMAGE_SUFFIXES)
    if _VIDEO.search(text):
        return set(VIDEO_SUFFIXES)
    if _WORD.search(text):
        return {".docx"}
    # ponytail: generic artifact intent — collect any supported type.
    if _GENERIC_ARTIFACT.search(text):
        return set(SUPPORTED_ARTIFACT_SUFFIXES)
    # Catch-all: if the user clearly wants to create/write something but we
    # can't identify the format, accept everything the backend might generate.
    if _ARTIFACT_ACTION.search(text):
        return set(SUPPORTED_ARTIFACT_SUFFIXES)
    return set()


def is_artifact_request(task: str) -> bool:
    """Distinguish creating a deliverable from merely reading an existing file."""
    text = str(task or "")
    # ponytail: any creation verb → user wants output
    return bool(_ARTIFACT_ACTION.search(text))


def select_execution_dir(task: str, work_dir: Path, job_dir: Path) -> Path:
    # ponytail: isolate to job_dir when the task looks like it will produce
    # files — avoids polluting the user's real working directory.
    produces_files = is_artifact_request(task) or _GENERIC_ARTIFACT.search(str(task or ""))
    if produces_files and not _CODE_OR_PROJECT.search(str(task or "")):
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
    # Only stage direct job artifacts. Recursing into a project/workspace could
    # accidentally send an unrelated user file merely because it shares a suffix.
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


def quarantine_failed_attempt(job_dir: Path, backend: str) -> Path:
    root = Path(job_dir).resolve(strict=True)
    name = str(backend or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{1,32}", name):
        raise ValueError("后端名称无效")
    quarantine_root = (root / "failed-attempts").resolve(strict=False)
    if quarantine_root.parent != root:
        raise ValueError("失败隔离目录越界")
    quarantine_root.mkdir(exist_ok=True)
    target = quarantine_root / f"{name}-{uuid.uuid4().hex[:8]}"
    target.mkdir()

    output = root / "outputs"
    quarantined_output = target / "outputs"
    quarantined_output.mkdir()
    if output.is_dir() and not output.is_symlink():
        for child in list(output.iterdir()):
            shutil.move(str(child), str(quarantined_output / child.name))

    reserved = {"outputs", "failed-attempts", "private-gh-config", "qa"}
    for child in list(root.iterdir()):
        if child.name in reserved or child.is_symlink():
            continue
        shutil.move(str(child), str(target / child.name))
    return target
