"""Windows DPAPI storage for the minimum data needed to resume Agent jobs."""

from __future__ import annotations

import csv
import ctypes
import hashlib
import json
import os
import re
import subprocess
import uuid
from ctypes import wintypes
from dataclasses import asdict, dataclass
from pathlib import Path

try:
    from .agent_core import normalize_backend, validate_task
except ImportError:  # Direct module loading in unit tests.
    from agent_core import normalize_backend, validate_task


MAGIC = b"XNJ1"
_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class PayloadIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class EncryptedJobPayload:
    task: str
    scope: str
    backend: str
    work_dir_relative: str
    recovery: str
    delivery_cursor: tuple[str, ...] = ()
    plan: tuple[dict[str, object], ...] = ()
    step_cursor: int = 0


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _input_blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    return _DataBlob(len(data), pointer), buffer


def _dpapi(data: bytes, entropy: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        raise RuntimeError("DPAPI 仅支持 Windows")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    operation.restype = wintypes.BOOL
    operation.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR if protect else ctypes.c_void_p,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    source, source_buffer = _input_blob(data)
    extra, extra_buffer = _input_blob(entropy)
    output = _DataBlob()
    description = None
    ok = operation(
        ctypes.byref(source),
        description,
        ctypes.byref(extra),
        None,
        None,
        0x1,
        ctypes.byref(output),
    )
    _ = (source_buffer, extra_buffer)
    if not ok:
        raise OSError(ctypes.get_last_error(), "DPAPI operation failed")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(output.pbData)


def _run_hidden(command: list[str]) -> None:
    subprocess.run(
        command,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _windows_user_sid() -> str:
    result = subprocess.run(
        ["whoami.exe", "/user", "/fo", "csv", "/nh"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    row = next(csv.reader([result.stdout.strip()]))
    sid = row[-1].strip()
    if not sid.startswith("S-1-"):
        raise RuntimeError("无法确认当前用户安全标识")
    return sid


def _harden_private_path(directory: Path, files: tuple[Path, ...] = ()) -> None:
    if os.name != "nt":
        return
    sid = _windows_user_sid()
    _run_hidden(
        [
            "icacls.exe",
            str(directory),
            "/inheritance:r",
            "/grant:r",
            f"*{sid}:(OI)(CI)F",
            "*S-1-5-18:(OI)(CI)F",
            "*S-1-5-32-544:(OI)(CI)F",
        ]
    )
    for path in files:
        if path.exists():
            _run_hidden(
                [
                    "icacls.exe",
                    str(path),
                    "/inheritance:r",
                    "/grant:r",
                    f"*{sid}:F",
                    "*S-1-5-18:F",
                    "*S-1-5-32-544:F",
                ]
            )


def _validate_payload(payload: EncryptedJobPayload) -> EncryptedJobPayload:
    task = validate_task(payload.task)
    scope = str(payload.scope or "").strip()
    if not scope or len(scope) > 500:
        raise ValueError("会话范围无效")
    relative_text = str(payload.work_dir_relative or "").strip()
    relative = Path(relative_text)
    if not relative_text or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("工作目录必须使用安全相对标识")
    recovery = str(payload.recovery or "").lower()
    if recovery not in {"blocked", "replay_safe"}:
        raise ValueError("恢复策略无效")
    cursor = tuple(str(item).lower() for item in payload.delivery_cursor)
    if any(not _DIGEST.fullmatch(item) for item in cursor):
        raise ValueError("交付摘要无效")
    raw_plan = tuple(payload.plan)
    if len(raw_plan) > 8:
        raise ValueError("任务计划过长")
    normalized_plan: list[dict[str, object]] = []
    valid_actions = {"read_only", "workspace_write", "high_impact", "unknown"}
    for position, raw_step in enumerate(raw_plan):
        if not isinstance(raw_step, dict):
            raise ValueError("任务计划无效")
        task_id = str(raw_step.get("task_id", "")).strip()
        instruction = str(raw_step.get("instruction", "")).strip()
        action_class = str(raw_step.get("action_class", "")).strip().lower()
        try:
            index = int(raw_step.get("index", -1))
        except (TypeError, ValueError) as exc:
            raise ValueError("任务计划无效") from exc
        if (
            not _JOB_ID.fullmatch(task_id)
            or index != position
            or not instruction
            or len(instruction) > 1000
            or action_class not in valid_actions
        ):
            raise ValueError("任务计划无效")
        normalized_plan.append(
            {
                "task_id": task_id,
                "index": index,
                "instruction": instruction,
                "action_class": action_class,
                "expected_artifact": bool(raw_step.get("expected_artifact", False)),
            }
        )
    step_cursor = int(payload.step_cursor)
    if step_cursor < 0 or step_cursor > len(normalized_plan):
        raise ValueError("任务步骤游标无效")
    return EncryptedJobPayload(
        task=task,
        scope=scope,
        backend=normalize_backend(payload.backend),
        work_dir_relative=str(relative),
        recovery=recovery,
        delivery_cursor=cursor,
        plan=tuple(normalized_plan),
        step_cursor=step_cursor,
    )


class EncryptedPayloadStore:
    """Store versioned JSON as current-user DPAPI ciphertext with strict ACLs."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        _harden_private_path(self.root)

    def _path(self, job_id: str) -> Path:
        normalized = str(job_id or "").strip()
        if not _JOB_ID.fullmatch(normalized):
            raise ValueError("任务编号无效")
        return self.root / f"{normalized}.bin"

    @staticmethod
    def _entropy(job_id: str) -> bytes:
        return hashlib.sha256(f"qqbot-local-agent-v1:{job_id}".encode("ascii")).digest()

    def write(self, job_id: str, payload: EncryptedJobPayload) -> None:
        path = self._path(job_id)
        validated = _validate_payload(payload)
        body = json.dumps(
            {"version": 1, **asdict(validated)},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        protected = _dpapi(body, self._entropy(job_id), protect=True)
        temporary = self.root / f".{path.stem}.{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_bytes(MAGIC + protected)
            _harden_private_path(self.root, (temporary,))
            os.replace(temporary, path)
            _harden_private_path(self.root, (path,))
        finally:
            temporary.unlink(missing_ok=True)

    def read(self, job_id: str) -> EncryptedJobPayload:
        path = self._path(job_id)
        try:
            raw = path.read_bytes()
            if not raw.startswith(MAGIC):
                raise ValueError("bad header")
            body = _dpapi(raw[len(MAGIC) :], self._entropy(job_id), protect=False)
            data = json.loads(body.decode("utf-8"))
            if data.pop("version", None) != 1:
                raise ValueError("bad version")
            data["delivery_cursor"] = tuple(data.get("delivery_cursor", ()))
            data["plan"] = tuple(data.get("plan", ()))
            return _validate_payload(EncryptedJobPayload(**data))
        except Exception as exc:
            raise PayloadIntegrityError("加密任务载荷无效") from exc

    def delete(self, job_id: str) -> None:
        self._path(job_id).unlink(missing_ok=True)

    def exists(self, job_id: str) -> bool:
        return self._path(job_id).is_file()
