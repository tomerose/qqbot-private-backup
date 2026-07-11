"""Independent completion evidence for planned Agent steps."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

try:
    from .bounded_process_io import capture_bounded_process
    from .task_planner import TaskStep
except ImportError:  # Direct module loading in unit tests.
    from bounded_process_io import capture_bounded_process
    from task_planner import TaskStep


@dataclass(frozen=True)
class VerificationEvidence:
    verified: bool
    code: str


@dataclass(frozen=True)
class VerificationRun:
    exit_code: int | None
    reason: str


_PROJECT_VERIFICATION_HINT = re.compile(
    r"代码|测试|构建|编译|修复|重构|python|typescript|next\.js", re.I
)


def should_run_project_verification(step: TaskStep) -> bool:
    return bool(_PROJECT_VERIFICATION_HINT.search(step.instruction))


async def run_verification_command(
    command: list[str],
    work_dir: Path,
    *,
    timeout: float = 600,
) -> VerificationRun:
    if not command or any(not str(part) for part in command):
        raise ValueError("验证命令无效")
    root = Path(work_dir)
    if root.is_symlink():
        raise ValueError("验证目录无效")
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise ValueError("验证目录无效") from exc
    if not root.is_dir():
        raise ValueError("验证目录无效")
    process = await asyncio.create_subprocess_exec(
        *(str(part) for part in command),
        cwd=str(root),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    capture = await capture_bounded_process(
        process,
        stdout_limit=512 * 1024,
        stderr_limit=128 * 1024,
        timeout=max(1.0, min(float(timeout), 900.0)),
    )
    return VerificationRun(capture.returncode, capture.reason)


def select_verification_command(work_dir: Path) -> list[str] | None:
    root = Path(work_dir)
    if root.is_symlink():
        return None
    try:
        root = root.resolve(strict=True)
    except OSError:
        return None
    if not root.is_dir():
        return None
    package_path = root / "package.json"
    if package_path.is_file() and not package_path.is_symlink():
        try:
            package = json.loads(package_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            package = {}
        scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
        if isinstance(scripts, dict) and isinstance(scripts.get("test"), str):
            return ["npm", "test"]
    tests_dir = root / "tests"
    if tests_dir.is_dir() and not tests_dir.is_symlink():
        return [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
    return None


def verify_step(
    step: TaskStep,
    exit_code: int | None,
    deliverables: Sequence[object],
    verification_exit: int | None,
) -> VerificationEvidence:
    if exit_code != 0:
        return VerificationEvidence(False, "execution_failed")
    if step.expected_artifact and not deliverables:
        return VerificationEvidence(False, "artifact_missing")
    if verification_exit not in {None, 0}:
        return VerificationEvidence(False, "verification_failed")
    return VerificationEvidence(True, "verified")
