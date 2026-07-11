"""Independent structural and render checks for generated Word documents."""

from __future__ import annotations

import asyncio
import re
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree


SOFFICE_EXE = Path(r"C:\Program Files\LibreOffice\program\soffice.exe")
_RESEARCH = re.compile(r"最近|最新|大事件|研究|调研|分析报告|github|来源|引用", re.I)
_URL = re.compile(r"https?://", re.I)


@dataclass(frozen=True)
class DocumentQualityDecision:
    allowed: bool
    code: str


def requires_research_quality(task: str) -> bool:
    return bool(_RESEARCH.search(str(task or "")))


def inspect_docx_quality(path: Path, *, research: bool) -> DocumentQualityDecision:
    candidate = Path(path)
    if candidate.is_symlink() or candidate.suffix.lower() != ".docx":
        return DocumentQualityDecision(False, "docx_invalid")
    try:
        resolved = candidate.resolve(strict=True)
        with zipfile.ZipFile(resolved) as archive:
            document_xml = archive.read("word/document.xml")
            try:
                relationships_xml = archive.read("word/_rels/document.xml.rels")
            except KeyError:
                relationships_xml = b""
        root = ElementTree.fromstring(document_xml)
        text = "".join(
            str(element.text or "")
            for element in root.iter()
            if str(element.tag).endswith("}t") or element.tag == "t"
        ).strip()
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, ElementTree.ParseError):
        return DocumentQualityDecision(False, "docx_invalid")
    if not text:
        return DocumentQualityDecision(False, "docx_empty")
    if not research:
        return DocumentQualityDecision(True, "docx_valid")
    if len(text) < 500:
        return DocumentQualityDecision(False, "docx_substance")
    external_targets: list[str] = []
    if relationships_xml:
        try:
            relationships = ElementTree.fromstring(relationships_xml)
            external_targets = [
                str(element.attrib.get("Target", ""))
                for element in relationships.iter()
                if element.attrib.get("TargetMode") == "External"
            ]
        except ElementTree.ParseError:
            return DocumentQualityDecision(False, "docx_invalid")
    source_count = len(_URL.findall(text)) + sum(
        1 for target in external_targets if _URL.match(target)
    )
    if source_count < 2:
        return DocumentQualityDecision(False, "docx_sources")
    return DocumentQualityDecision(True, "docx_research_valid")


async def render_docx(path: Path, qa_dir: Path, *, timeout: float = 90) -> DocumentQualityDecision:
    candidate = Path(path).resolve(strict=True)
    output = Path(qa_dir).resolve(strict=False)
    output.mkdir(parents=True, exist_ok=True)
    profile = output / "libreoffice-profile"
    profile.mkdir(exist_ok=True)
    if not SOFFICE_EXE.is_file():
        return DocumentQualityDecision(False, "docx_renderer_missing")
    process = await asyncio.create_subprocess_exec(
        str(SOFFICE_EXE),
        "--headless",
        f"-env:UserInstallation={profile.as_uri()}",
        "--convert-to",
        "pdf",
        "--outdir",
        str(output),
        str(candidate),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        returncode = await asyncio.wait_for(process.wait(), timeout=max(5.0, float(timeout)))
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return DocumentQualityDecision(False, "docx_render_timeout")
    rendered = output / f"{candidate.stem}.pdf"
    try:
        valid_pdf = rendered.is_file() and rendered.stat().st_size > 100 and rendered.read_bytes()[:5] == b"%PDF-"
    except OSError:
        valid_pdf = False
    if returncode != 0 or not valid_pdf:
        return DocumentQualityDecision(False, "docx_render_failed")
    return DocumentQualityDecision(True, "docx_rendered")
