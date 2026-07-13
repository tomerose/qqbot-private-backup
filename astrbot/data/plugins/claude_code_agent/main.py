"""Owner-only QQ entry point for local Claude, Codex, and WorkBuddy agents."""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.message_components import File, Image, Plain
from astrbot.api.star import Context, Star
from astrbot.core.workspace import default_workspace_root

from .agent_core import (
    ApprovalRegistry,
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    DEFAULT_WORKSPACE,
    DEFAULT_WORK_DIR,
    DEFAULT_CODEX_MODEL,
    MAX_OUTPUT_BYTES,
    Deliverable,
    build_job_agent_env,
    build_backend_command,
    create_job_dir,
    build_process_tree_kill_command,
    discover_deliverables,
    extract_agent_command,
    is_within_allowed_roots,
    is_inline_media_payload,
    normalize_backend,
    parse_backend_result,
    redact_sensitive_text,
    referenced_workspace_files,
    upload_aiocqhttp_group_file,
    validate_task,
    validate_work_dir,
)
from .job_store import JobStore
from .encrypted_payload_store import (
    EncryptedJobPayload,
    EncryptedPayloadStore,
    PayloadIntegrityError,
)
from .natural_router import extract_natural_agent_text, route_natural_agent
from .access_policy import AccessPolicy, Capability
from .bounded_process_io import capture_bounded_process
from .action_policy import ActionClass
from .backend_router import BackendRoute, route_backend
from .backend_health import BackendHealthCache
from .artifact_staging import (
    collect_staged_artifacts,
    expected_artifact_suffixes,
    quarantine_failed_attempt,
    select_execution_dir,
)
from .document_quality import (
    inspect_docx_quality,
    render_docx,
    requires_research_quality,
)
from .step_policy import assess_step, step_digest
from .task_orchestrator import StepExecution, TaskEvent, TaskOrchestrator
from .task_planner import ExecutionPlan, TaskRequest, TaskStep, plan_task
from .progress_policy import ProgressPolicy
from .response_style import format_task_reply
from .eta_policy import estimate_eta
from .task_verifier import (
    run_verification_command,
    select_verification_command,
    should_run_project_verification,
    verify_step,
)
from .trusted_policy import (
    TrustedDecision,
    TrustedDisposition,
    TrustedPolicy,
    assess_trusted_task,
)
try:
    from draw_command.pro_access import agent_available, get_tier, Tier, use_agent
except ImportError:  # AstrBot package import path.
    from data.plugins.draw_command.pro_access import agent_available, get_tier, Tier, use_agent

OWNER_ID = "1211000567"
MAX_REPLY_CHARS = 3500


class ClaudeCodeAgent(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._access_policy = AccessPolicy(
            self.config.get("pro_user_ids", OWNER_ID)
        )
        self._trusted_policy = TrustedPolicy(
            self.config.get("trusted_pro_user_ids", OWNER_ID)
        )
        self._pro_db_path = (
            Path(__file__).resolve().parents[2]
            / "plugin_data"
            / "xiaoning_pro"
            / "pro_members.db"
        )
        self.workspace = DEFAULT_WORKSPACE.resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.backend = normalize_backend(self.config.get("default_backend", BACKEND_CLAUDE))
        self.codex_model = str(self.config.get("codex_model", DEFAULT_CODEX_MODEL)).strip() or DEFAULT_CODEX_MODEL
        configured_dir = self.config.get("default_work_dir", str(DEFAULT_WORK_DIR))
        self.work_dir = validate_work_dir(configured_dir)
        self.timeout_seconds = max(60, min(int(self.config.get("timeout_seconds", 1800)), 7200))
        self.max_attachment_files = max(1, min(int(self.config.get("max_attachment_files", 10)), 20))
        attachment_mb = max(1, min(int(self.config.get("max_attachment_mb", 20)), 100))
        self.max_attachment_bytes = attachment_mb * 1024 * 1024
        self.recovery_root = DEFAULT_WORK_DIR.resolve()
        self.max_queued_jobs = max(1, min(int(self.config.get("max_queued_jobs", 3)), 5))
        self._queued_handlers = 0
        self._execution_lock = asyncio.Lock()
        self._active_job_id: str | None = None
        self._active_backend: str | None = None
        self._active_proc: asyncio.subprocess.Process | None = None
        self._cancel_requested = False
        self._approvals = ApprovalRegistry(ttl_seconds=300)
        self._progress_policy = ProgressPolicy()
        self._backend_health = BackendHealthCache()
        self._payload_store = EncryptedPayloadStore(
            self.workspace / "state" / "private_jobs"
        )
        self._job_store = JobStore(self.workspace / "state" / "jobs.db")
        recovered = self._job_store.recover_interrupted()
        if recovered:
            logger.warning(f"[LocalAgent] recovered interrupted jobs count={recovered}")
        self._recovery_task: asyncio.Task | None = None
        try:
            self._recovery_task = asyncio.get_running_loop().create_task(
                self._recover_jobs()
            )
        except RuntimeError:
            pass

    def _is_owner(self, ctx: Context) -> bool:
        sender_id = ctx.get_sender_id()
        return self._can_manage_runtime(ctx) or self._is_public_pro(sender_id)

    def _can_manage_runtime(self, ctx: Context) -> bool:
        policy = getattr(self, "_access_policy", AccessPolicy((OWNER_ID,)))
        trusted = getattr(self, "_trusted_policy", TrustedPolicy((OWNER_ID,)))
        sender_id = ctx.get_sender_id()
        return policy.authorize(sender_id, Capability.LOCAL_AGENT) and trusted.is_trusted(
            sender_id
        )

    def _is_public_pro(self, sender_id: str) -> bool:
        """Return True when *sender_id* holds an active Pro/GO membership."""
        try:
            return get_tier(sender_id, self._pro_db()) >= Tier.GO
        except Exception:
            return False

    def _pro_db(self) -> Path:
        return getattr(
            self,
            "_pro_db_path",
            Path(__file__).resolve().parents[2] / "plugin_data" / "xiaoning_pro" / "pro_members.db",
        )

    def _check_agent_access(self, sender_id: object) -> tuple[bool, str]:
        """Returns (allowed, reason). GO: 1x/week, PRO: unlimited."""
        path = self._pro_db()
        available, reason = agent_available(sender_id, path)
        if not available:
            return False, reason
        tier = get_tier(sender_id, path)
        if tier >= Tier.PRO:
            return True, ""
        if tier == Tier.GO:
            return True, "go"  # "go" signals caller to call use_agent() after success
        return False, "Agent 功能需要 GO 或 PRO 权限"

    def _authorize_agent_task(self, sender_id: object, task: str):
        if self._trusted_policy.is_trusted(sender_id):
            return self._trusted_policy.authorize_task(
                sender_id, task, self.work_dir, self.recovery_root
            )
        tier = get_tier(sender_id, self._pro_db())
        if tier >= Tier.GO:
            return assess_trusted_task(task, self.work_dir, self.recovery_root)
        return TrustedDecision(TrustedDisposition.DENY, "not_pro")

    @staticmethod
    def _reply(ctx: Context, component):
        return ctx.chain_result([component])

    @staticmethod
    def _event_text(ctx: Context) -> str:
        value = getattr(ctx, "message_str", "")
        if value:
            return str(value)
        message_obj = getattr(ctx, "message_obj", None)
        value = getattr(message_obj, "message_str", "")
        if value:
            return str(value)
        for getter_name in ("get_message_str", "get_message_text"):
            getter = getattr(ctx, getter_name, None)
            if callable(getter):
                try:
                    value = getter()
                except Exception:
                    continue
                if value:
                    return str(value)
        return ""

    @staticmethod
    def _command_text(ctx: Context) -> str:
        message_obj = getattr(ctx, "message_obj", None)
        return extract_agent_command(
            ClaudeCodeAgent._event_text(ctx),
            getattr(message_obj, "message", None),
            str(getattr(message_obj, "self_id", "") or ""),
            str(ctx.get_group_id() or ""),
        )

    @staticmethod
    def _natural_text(ctx: Context) -> str:
        message_obj = getattr(ctx, "message_obj", None)
        return extract_natural_agent_text(
            ClaudeCodeAgent._event_text(ctx),
            getattr(message_obj, "message", None),
            str(getattr(message_obj, "self_id", "") or ""),
            str(ctx.get_group_id() or ""),
        )

    @staticmethod
    def _approval_scope(ctx: Context) -> str:
        origin = str(getattr(ctx, "unified_msg_origin", "") or "").strip()
        if origin:
            return origin
        group_id = str(ctx.get_group_id() or "").strip()
        sender_id = str(ctx.get_sender_id() or "").strip()
        return f"group:{group_id}" if group_id else f"private:{sender_id}"

    def _help_text(self, *, trusted_runtime: bool) -> str:
        common = (
            "Agent 任务\n"
            "/agent run <任务>\n"
            "/agent approve <确认码>\n"
            "/agent status\n"
            "/agent cancel\n"
        )
        if not trusted_runtime:
            return common + "GO 每周 1 次，Pro 不限次数；任务在独立安全工作区运行。"
        return (
            common
            + "/agent use claude|codex|workbuddy\n"
            + "/agent cwd [绝对目录]\n"
            + f"当前后端：{self.backend}。工作目录已设置，本机路径不在聊天中显示。"
        )

    async def _deliver_file(self, event: AstrMessageEvent, path: Path) -> bool:
        """Deliver a regular local file, using real OneBot group upload when needed."""
        try:
            roots = [self.workspace, default_workspace_root(event.unified_msg_origin)]
            if not is_within_allowed_roots(path, roots):
                logger.warning("[LocalAgent] blocked local file outside approved roots")
                return False
            if event.get_group_id() and hasattr(event, "bot"):
                await upload_aiocqhttp_group_file(event.bot, event.get_group_id(), path)
            else:
                await event.send(MessageChain([File(name=path.name, file=str(path))]))
            return True
        except Exception as exc:
            logger.error(
                f"[LocalAgent] file delivery failed: {type(exc).__name__}"
            )
            return False

    def _is_declared_plugin_media(self, event: AstrMessageEvent, path: Path) -> bool:
        """Allow only existing media outputs explicitly registered by the media plugins."""
        try:
            if path.is_symlink():
                return False
            resolved = path.resolve(strict=True)
        except OSError:
            return False

        declarations = (
            ("_pro_draw_output_paths", self.workspace / "pro_draw", {".png", ".jpg", ".jpeg", ".webp"}),
            ("_pro_video_output_paths", self.workspace / "pro_video", {".mp4", ".webm", ".mkv", ".mov", ".gif"}),
        )
        for extra_key, root, suffixes in declarations:
            if resolved.suffix.lower() not in suffixes:
                continue
            try:
                resolved_root = root.resolve(strict=True)
            except OSError:
                continue
            if resolved_root not in resolved.parents:
                continue
            for raw_path in event.get_extra(extra_key, []) or []:
                candidate = Path(str(raw_path or ""))
                try:
                    if not candidate.is_symlink() and candidate.resolve(strict=True) == resolved:
                        return True
                except OSError:
                    continue
        return False

    @filter.on_decorating_result(priority=-9999)
    async def protect_privacy_and_deliver_files(self, event: AstrMessageEvent) -> None:
        """Redact host paths and deliver files created by AstrBot's native agent."""
        result = event.get_result()
        if result is None or not result.chain:
            return

        if isinstance(result.chain, str):
            components = [Plain(result.chain)]
        elif isinstance(result.chain, MessageChain):
            components = list(result.chain.chain)
        else:
            components = list(result.chain)

        plain_text = "\n".join(
            component.text for component in components if isinstance(component, Plain)
        )
        cleaned: list[object] = []
        represented_paths: set[str] = set()
        allowed_roots = [self.workspace, default_workspace_root(event.unified_msg_origin)]
        for component in components:
            if isinstance(component, Plain):
                component.text = redact_sensitive_text(component.text)
                cleaned.append(component)
                continue
            if isinstance(component, File) and component.file_:
                path = Path(component.file_)
                if is_within_allowed_roots(path, allowed_roots):
                    represented_paths.add(str(path.resolve()))
                    if event.get_group_id() and hasattr(event, "bot"):
                        if await self._deliver_file(event, path):
                            continue
                    cleaned.append(component)
                else:
                    cleaned.append(Plain("[本地文件因隐私策略未发送]"))
                    logger.warning("[LocalAgent] blocked local file outside approved roots")
                continue
            if isinstance(component, Image):
                raw = str(component.path or component.file or "")
                if is_inline_media_payload(raw):
                    cleaned.append(Plain("[内联图片因隐私策略未发送]"))
                    logger.warning("[LocalAgent] blocked inline image without trusted provenance")
                    continue
                local_path: Path | None = None
                if raw.lower().startswith("file:"):
                    parsed = urlparse(raw)
                    decoded = unquote(parsed.path)
                    if len(decoded) >= 3 and decoded[0] == "/" and decoded[2] == ":":
                        decoded = decoded[1:]
                    local_path = Path(decoded)
                elif raw and Path(raw).is_absolute():
                    local_path = Path(raw)
                if (
                    local_path is not None
                    and not self._is_declared_plugin_media(event, local_path)
                    and not is_within_allowed_roots(local_path, allowed_roots)
                ):
                    cleaned.append(Plain("[本地图片因隐私策略未发送]"))
                    logger.warning("[LocalAgent] blocked local image outside approved roots")
                    continue
                cleaned.append(component)
                continue
            cleaned.append(component)

        if self._is_owner(event):
            already_sent = set(event.get_extra("local_agent_workspace_files_sent") or [])
            try:
                root = default_workspace_root(event.unified_msg_origin)
                referenced = referenced_workspace_files(
                    root,
                    plain_text,
                    max_files=self.max_attachment_files,
                    max_file_bytes=self.max_attachment_bytes,
                )
                for item in referenced:
                    key = str(item.path.resolve())
                    if key in represented_paths or key in already_sent:
                        continue
                    if item.kind == "image":
                        cleaned.append(Image.fromFileSystem(str(item.path)))
                        already_sent.add(key)
                    elif await self._deliver_file(event, item.path):
                        already_sent.add(key)
                event.set_extra("local_agent_workspace_files_sent", sorted(already_sent))
            except Exception as exc:
                logger.warning(
                    f"[LocalAgent] referenced file delivery skipped: {type(exc).__name__}"
                )

        result.chain = cleaned

    async def _terminate_active_process(self) -> bool:
        proc = self._active_proc
        if proc is None or proc.returncode is not None:
            return False
        try:
            killer = await asyncio.create_subprocess_exec(
                *build_process_tree_kill_command(proc.pid),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            await asyncio.wait_for(killer.wait(), timeout=10)
            await asyncio.wait_for(proc.wait(), timeout=3)
        except (FileNotFoundError, asyncio.TimeoutError, ProcessLookupError):
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        return True

    async def _stop_process(self) -> bool:
        if self._active_proc is None or self._active_proc.returncode is not None:
            return False
        self._cancel_requested = True
        return await self._terminate_active_process()

    async def _execute(
        self,
        job_id: str,
        job_dir: Path,
        task: str,
        backend: str,
        high_risk_approved: bool,
    ) -> tuple[str, list[Deliverable], str, int | None, str]:
        trusted_runtime = bool(getattr(self, "_execution_trusted_runtime", True))
        output_dir = job_dir / "outputs"
        boundary = assess_trusted_task(task, self.work_dir, self.recovery_root)
        if boundary.disposition is TrustedDisposition.DENY:
            return "任务被本机安全边界拒绝。", [], "failed", None, boundary.code
        execution_dir = (
            select_execution_dir(task, self.work_dir, job_dir)
            if trusted_runtime
            else job_dir.resolve(strict=True)
        )
        command = build_backend_command(
            backend,
            task,
            execution_dir,
            output_dir,
            self.codex_model,
            high_risk_approved=high_risk_approved,
            trusted_runtime=trusted_runtime,
        )
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        returncode: int | None = None
        stdout = b""
        stderr = b""
        capture_reason = "completed"
        try:
            self._active_proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(execution_dir),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
                env=build_job_agent_env(job_dir),
            )
            capture = await capture_bounded_process(
                self._active_proc,
                stdout_limit=MAX_OUTPUT_BYTES,
                stderr_limit=256_000,
                timeout=self.timeout_seconds,
                terminate=self._terminate_active_process,
            )
            stdout = capture.stdout
            stderr = capture.stderr
            returncode = capture.returncode
            capture_reason = capture.reason
        except FileNotFoundError:
            return f"执行失败：找不到 {backend} 的本机 CLI。", [], "failed", None, "cli_missing"
        finally:
            self._active_proc = None

        if self._cancel_requested:
            self._cancel_requested = False
            return "任务已取消。", [], "cancelled", returncode, "cancelled"
        if capture_reason == "timeout":
            return f"任务已超过 {self.timeout_seconds} 秒并终止。", [], "timeout", returncode, "timeout"
        if capture_reason in {"stdout_limit", "stderr_limit"}:
            return f"{backend} 输出超过安全上限，任务已终止。", [], "failed", returncode, capture_reason
        raw = stdout.decode("utf-8", errors="replace")
        if backend == "codex":
            result_file = job_dir / "agent-result.txt"
            if result_file.is_file():
                raw = result_file.read_text(encoding="utf-8", errors="replace")
        if returncode not in (None, 0):
            logger.error(f"[LocalAgent] backend={backend} job={job_id} exit={returncode}")
            error_text = stderr.decode("utf-8", errors="replace").strip()
            error_summary = redact_sensitive_text(error_text[-800:])
            suffix = f"\n错误摘要：{error_summary}" if error_summary else ""
            return (
                f"{backend} 执行失败（退出码 {returncode}）。{suffix}",
                [], "failed", returncode, "nonzero_exit",
            )

        collect_staged_artifacts(
            job_dir,
            output_dir,
            expected_artifact_suffixes(task),
            max_files=self.max_attachment_files,
            max_file_bytes=self.max_attachment_bytes,
        )
        deliverables = discover_deliverables(
            output_dir,
            max_files=self.max_attachment_files,
            max_file_bytes=self.max_attachment_bytes,
        )
        quality_checked: list[Deliverable] = []
        for item in deliverables:
            if item.path.suffix.lower() != ".docx":
                quality_checked.append(item)
                continue
            structural = inspect_docx_quality(
                item.path, research=requires_research_quality(task)
            )
            if not structural.allowed:
                logger.warning(
                    f"[LocalAgent] job={job_id} word_quality={structural.code}"
                )
                continue
            rendered = await render_docx(item.path, job_dir / "qa")
            if not rendered.allowed:
                logger.warning(
                    f"[LocalAgent] job={job_id} word_quality={rendered.code}"
                )
                continue
            quality_checked.append(item)
        deliverables = quality_checked
        result = parse_backend_result(backend, raw)
        if not result and not deliverables:
            logger.error(f"[LocalAgent] backend={backend} job={job_id} empty_result")
            return f"{backend} 没有返回可读结果。", [], "failed", returncode, "empty_result"
        if not result:
            result = "文件已生成并通过交付检查。"
        names = "、".join(item.path.name for item in deliverables)
        suffix = f"\n交付文件：{names}" if names else ""
        reply = f"任务 {job_id} 执行结束（退出码 0） · {backend}{suffix}\n\n{result}"
        if len(reply) > MAX_REPLY_CHARS:
            reply = reply[:MAX_REPLY_CHARS] + "\n…（文字已截断）"
        return reply, deliverables, "completed", returncode, ""

    def _relative_work_dir(self, resumable: bool) -> str:
        if not resumable:
            return "."
        try:
            relative = self.work_dir.resolve(strict=False).relative_to(self.recovery_root)
        except (OSError, ValueError):
            return "."
        return str(relative) if str(relative) else "."

    def _delete_payload(self, job_id: str) -> None:
        try:
            self._payload_store.delete(job_id)
        except Exception as exc:
            logger.error(
                f"[LocalAgent] encrypted payload cleanup failed: {type(exc).__name__}"
            )

    @staticmethod
    def _directory_within(path: Path, root: Path) -> bool:
        candidate = Path(path)
        if candidate.is_symlink():
            return False
        try:
            resolved = candidate.resolve(strict=True)
            base = Path(root).resolve(strict=True)
        except OSError:
            return False
        return resolved.is_dir() and (resolved == base or base in resolved.parents)

    async def _send_active_text(self, scope: str, text: str) -> bool:
        try:
            return bool(
                await self.context.send_message(
                    scope, MessageChain([Plain(redact_sensitive_text(text))])
                )
            )
        except Exception as exc:
            logger.warning(
                f"[LocalAgent] active delivery failed: {type(exc).__name__}"
            )
            return False

    @staticmethod
    def _file_digest(path: Path) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _manifest_digest(self, deliverables: list[Deliverable]) -> str:
        entries = sorted(
            f"{item.path.name}:{self._file_digest(item.path)}" for item in deliverables
        )
        return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()

    @staticmethod
    def _payload_with_cursor(
        payload: EncryptedJobPayload, digests: set[str]
    ) -> EncryptedJobPayload:
        return EncryptedJobPayload(
            task=payload.task,
            scope=payload.scope,
            backend=payload.backend,
            work_dir_relative=payload.work_dir_relative,
            recovery=payload.recovery,
            delivery_cursor=tuple(sorted(digests)),
            plan=payload.plan,
            step_cursor=payload.step_cursor,
            trusted_runtime=payload.trusted_runtime,
        )

    @staticmethod
    def _plan_records(plan: ExecutionPlan) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "task_id": step.task_id,
                "index": step.index,
                "instruction": step.instruction,
                "action_class": step.action_class.value,
                "expected_artifact": step.expected_artifact,
            }
            for step in plan.steps
        )

    @staticmethod
    def _plan_from_payload(payload: EncryptedJobPayload, task_id: str) -> ExecutionPlan:
        steps = tuple(
            TaskStep(
                task_id=str(item["task_id"]),
                index=int(item["index"]),
                instruction=str(item["instruction"]),
                action_class=ActionClass(str(item["action_class"])),
                expected_artifact=bool(item["expected_artifact"]),
            )
            for item in payload.plan
        )
        if not steps or any(step.task_id != task_id for step in steps):
            raise ValueError("任务计划无效")
        return ExecutionPlan(task_id, payload.backend, steps)

    @staticmethod
    def _payload_with_step_cursor(
        payload: EncryptedJobPayload, step_cursor: int
    ) -> EncryptedJobPayload:
        return EncryptedJobPayload(
            task=payload.task,
            scope=payload.scope,
            backend=payload.backend,
            work_dir_relative=payload.work_dir_relative,
            recovery=payload.recovery,
            delivery_cursor=payload.delivery_cursor,
            plan=payload.plan,
            step_cursor=int(step_cursor),
            trusted_runtime=payload.trusted_runtime,
        )

    async def _deliver_recovered_files(
        self,
        scope: str,
        job_id: str,
        payload: EncryptedJobPayload,
        deliverables: list[Deliverable],
    ) -> bool:
        delivered_digests = set(payload.delivery_cursor)
        current_payload = payload
        scope_parts = str(scope or "").rsplit(":", 2)
        group_target = (
            (scope_parts[0], scope_parts[2])
            if len(scope_parts) == 3
            and scope_parts[1] == "GroupMessage"
            and scope_parts[2].isdigit()
            else None
        )
        for item in deliverables:
            digest = self._file_digest(item.path)
            if digest in delivered_digests:
                continue
            component = (
                Image.fromFileSystem(str(item.path))
                if item.kind == "image"
                else File(file=str(item.path), name=item.path.name)
            )
            try:
                if item.kind != "image" and group_target is not None:
                    platform = self.context.get_platform_inst(group_target[0])
                    bot = platform.get_client() if platform is not None else None
                    await upload_aiocqhttp_group_file(
                        bot, group_target[1], item.path
                    )
                    sent = True
                else:
                    sent = bool(
                        await self.context.send_message(
                            scope, MessageChain([component])
                        )
                    )
            except Exception as exc:
                logger.warning(
                    f"[LocalAgent] recovered file delivery failed: {type(exc).__name__}"
                )
                return False
            if not sent:
                return False
            delivered_digests.add(digest)
            current_payload = self._payload_with_cursor(
                current_payload, delivered_digests
            )
            self._payload_store.write(job_id, current_payload)
        return True

    async def _recover_jobs(self) -> None:
        """Resume only DPAPI-backed replay-safe jobs; block everything else."""
        for record in self._job_store.list_interrupted():
            job_id = str(record["job_id"])
            try:
                payload = self._payload_store.read(job_id)
            except PayloadIntegrityError:
                try:
                    self._job_store.transition(
                        job_id,
                        "recovery_blocked",
                        "recovery_blocked",
                        error_code="payload_invalid",
                    )
                finally:
                    self._delete_payload(job_id)
                continue

            if record.get("recovery") != "replay_safe" or payload.recovery != "replay_safe":
                self._job_store.transition(
                    job_id,
                    "recovery_blocked",
                    "recovery_blocked",
                    error_code="reapproval_required",
                )
                self._delete_payload(job_id)
                await self._send_active_text(
                    payload.scope,
                    f"任务 {job_id} 在重启时中断。为避免重复产生副作用，需要重新确认后再执行。",
                )
                continue

            work_dir = (self.recovery_root / payload.work_dir_relative).resolve(
                strict=False
            )
            job_dir = (self.workspace / "jobs" / job_id).resolve(strict=False)
            if (
                not self._directory_within(work_dir, self.recovery_root)
                or not self._directory_within(job_dir, self.workspace / "jobs")
            ):
                self._job_store.transition(
                    job_id,
                    "recovery_blocked",
                    "recovery_blocked",
                    error_code="recovery_boundary",
                )
                self._delete_payload(job_id)
                await self._send_active_text(
                    payload.scope,
                    f"任务 {job_id} 无法在原安全工作区恢复，请重新提交。",
                )
                continue

            async with self._execution_lock:
                original_work_dir = self.work_dir
                original_trusted_runtime = getattr(
                    self, "_execution_trusted_runtime", True
                )
                self.work_dir = work_dir
                self._execution_trusted_runtime = payload.trusted_runtime
                self._active_job_id = job_id
                self._active_backend = payload.backend
                self._cancel_requested = False
                self._job_store.transition(job_id, "recovering", "recovering")
                await self._send_active_text(payload.scope, f"任务 {job_id} 已从中断处继续。")
                try:
                    if record.get("delivery_digest"):
                        deliverables = discover_deliverables(
                            job_dir / "outputs",
                            max_files=self.max_attachment_files,
                            max_file_bytes=self.max_attachment_bytes,
                        )
                        self._job_store.transition(job_id, "verifying", "verifying")
                        self._job_store.transition(job_id, "delivering", "delivering")
                        delivered = await self._deliver_recovered_files(
                            payload.scope, job_id, payload, deliverables
                        )
                        if delivered:
                            self._job_store.finish(
                                job_id,
                                "completed",
                                exit_code=0,
                                deliverable_count=len(deliverables),
                            )
                            self._delete_payload(job_id)
                            await self._send_active_text(
                                payload.scope,
                                f"任务 {job_id} 的剩余文件已交付。",
                            )
                        continue
                    if payload.plan:
                        plan = self._plan_from_payload(payload, job_id)
                        remaining = plan.steps[payload.step_cursor :]
                        if not remaining or any(
                            step.action_class is not ActionClass.READ_ONLY
                            for step in remaining
                        ):
                            self._job_store.finish(
                                job_id,
                                "recovery_blocked",
                                error_code="step_not_replay_safe",
                            )
                            self._delete_payload(job_id)
                            await self._send_active_text(
                                payload.scope,
                                f"任务 {job_id} 的当前步骤不能自动恢复，请重新提交。",
                            )
                            continue
                        responses: list[str] = []
                        recovered_items: dict[str, Deliverable] = {}
                        state = "completed"
                        exit_code = 0
                        error_code = ""
                        for step in remaining:
                            route = route_backend(
                                step,
                                plan.preferred_backend,
                                {"claude", "codex", "workbuddy"},
                                set(),
                            )
                            if route.backend is None:
                                state = "failed"
                                error_code = route.code
                                break
                            response, step_items, step_state, step_exit, step_error = await self._execute(
                                job_id,
                                job_dir,
                                step.instruction,
                                route.backend,
                                high_risk_approved=False,
                            )
                            evidence = verify_step(
                                step,
                                step_exit if step_state == "completed" else (step_exit or 1),
                                step_items,
                                None,
                            )
                            if not evidence.verified:
                                state = "failed"
                                exit_code = step_exit
                                error_code = step_error or evidence.code
                                break
                            responses.append(response)
                            for item in step_items:
                                recovered_items[str(item.path.resolve())] = item
                            payload = self._payload_with_step_cursor(
                                payload, step.index + 1
                            )
                            self._payload_store.write(job_id, payload)
                            self._job_store.record_step(
                                job_id,
                                step_index=step.index,
                                step_count=len(plan.steps),
                            )
                        response = responses[-1] if responses else "任务恢复未完成。"
                        deliverables = list(recovered_items.values())
                    else:
                        response, deliverables, state, exit_code, error_code = await self._execute(
                            job_id,
                            job_dir,
                            payload.task,
                            payload.backend,
                            high_risk_approved=False,
                        )
                    if state == "completed":
                        self._job_store.transition(job_id, "verifying", "verifying")
                        self._job_store.record_delivery(
                            job_id, self._manifest_digest(deliverables)
                        )
                        self._job_store.transition(job_id, "delivering", "delivering")
                        text_delivered = await self._send_active_text(
                            payload.scope, response
                        )
                        files_delivered = await self._deliver_recovered_files(
                            payload.scope, job_id, payload, deliverables
                        )
                        delivered = text_delivered and files_delivered
                        if delivered:
                            self._job_store.finish(
                                job_id,
                                "completed",
                                exit_code=exit_code,
                                deliverable_count=len(deliverables),
                            )
                            self._delete_payload(job_id)
                    else:
                        self._job_store.finish(
                            job_id,
                            state,
                            exit_code=exit_code,
                            deliverable_count=len(deliverables),
                            error_code=error_code,
                        )
                        self._delete_payload(job_id)
                        await self._send_active_text(payload.scope, response)
                except Exception as exc:
                    logger.error(
                        f"[LocalAgent] recovery failed job={job_id}: {type(exc).__name__}"
                    )
                    try:
                        self._job_store.finish(
                            job_id, "failed", error_code="recovery_exception"
                        )
                        self._delete_payload(job_id)
                    except Exception:
                        pass
                finally:
                    self.work_dir = original_work_dir
                    self._execution_trusted_runtime = original_trusted_runtime
                    self._active_job_id = None
                    self._active_backend = None

    async def _handle_planned_action(
        self,
        ctx: Context,
        action: str,
        parts: list[str],
        natural_intent: object | None,
    ):
        sender_id = str(ctx.get_sender_id() or "")
        scope = self._approval_scope(ctx)
        approved_steps: dict[str, str] = {}
        try:
            if action in {"approve", "confirm"}:
                pending = (
                    self._approvals.consume_latest(sender_id, scope)
                    if action == "confirm"
                    else self._approvals.consume(parts[2], sender_id, scope)
                )
                if pending is None or not pending.task_id or not pending.step_digest:
                    yield self._reply(ctx, Plain("确认码无效、已过期或不属于当前会话。"))
                    return
                job_id = pending.task_id
                payload = self._payload_store.read(job_id)
                trusted_runtime = payload.trusted_runtime
                plan = self._plan_from_payload(payload, job_id)
                if payload.step_cursor >= len(plan.steps):
                    raise ValueError("任务步骤游标无效")
                current_step = plan.steps[payload.step_cursor]
                if (
                    step_digest(current_step) != pending.step_digest
                    or pending.work_dir != self.work_dir.resolve(strict=False)
                ):
                    yield self._reply(ctx, Plain("任务状态已变化，请重新提交。"))
                    return
                record = self._job_store.get(job_id)
                if record is None or record["state"] != "awaiting_approval":
                    yield self._reply(ctx, Plain("任务状态已变化，请重新提交。"))
                    return
                job_dir = (self.workspace / "jobs" / job_id).resolve(strict=True)
                approved_steps[pending.step_digest] = pending.backend
            else:
                backend = (
                    getattr(natural_intent, "backend", None)
                    if natural_intent is not None
                    else None
                ) or self.backend
                trusted_runtime = self._can_manage_runtime(ctx)
                task = validate_task(parts[2])
                trusted_decision = self._authorize_agent_task(sender_id, task)
                if trusted_decision.disposition is TrustedDisposition.DENY:
                    yield self._reply(
                        ctx, Plain("这个任务超出本机安全边界，没有执行。")
                    )
                    return
                # Tier preflight only. GO is charged after verified execution;
                # Pro follows the published unlimited-Agent contract.
                if not trusted_runtime:
                    available, reason = self._check_agent_access(sender_id)
                    if not available:
                        yield self._reply(ctx, Plain(reason))
                        return
                # ponytail: check queue capacity BEFORE persisting to avoid orphaned entries.
                if self._execution_lock.locked() and self._queued_handlers >= self.max_queued_jobs:
                    yield self._reply(ctx, Plain("任务队列已满，请稍后再试。"))
                    return
                job_id = uuid.uuid4().hex[:12]
                job_dir = create_job_dir(self.workspace, job_id)
                plan = plan_task(TaskRequest(job_id, task, backend))
                replay_safe = all(
                    step.action_class is ActionClass.READ_ONLY for step in plan.steps
                ) and self._directory_within(self.work_dir, self.recovery_root)
                payload = EncryptedJobPayload(
                    task=task,
                    scope=scope,
                    backend=plan.preferred_backend,
                    work_dir_relative=self._relative_work_dir(replay_safe),
                    recovery="replay_safe" if replay_safe else "blocked",
                    plan=self._plan_records(plan),
                    step_cursor=0,
                    trusted_runtime=trusted_runtime,
                )
                self._payload_store.write(job_id, payload)
                self._job_store.start(
                    job_id,
                    sender_id,
                    scope,
                    task,
                    plan.preferred_backend,
                    "planned",
                    state="planned",
                    recovery=payload.recovery,
                    step_count=len(plan.steps),
                )
        except (ValueError, PayloadIntegrityError) as exc:
            logger.warning(f"[LocalAgent] job plan rejected: {type(exc).__name__}")
            yield self._reply(ctx, Plain("任务计划无效或已失效，请重新提交。"))
            return
        except OSError as exc:
            logger.error(f"[LocalAgent] job persistence failed: {type(exc).__name__}")
            yield self._reply(ctx, Plain("任务存储失败，请稍后再试。"))
            return

        will_queue = self._execution_lock.locked()
        queue_ahead = self._queued_handlers + (1 if will_queue else 0)
        eta = estimate_eta(plan, queue_ahead=queue_ahead)
        queued_counted = False
        lock_acquired = False
        if will_queue:
            self._queued_handlers += 1
            queued_counted = True
            current = self._job_store.get(job_id)
            if current and current["state"] in {"planned", "awaiting_approval"}:
                self._job_store.transition(job_id, "queued", "queued")
            yield self._reply(
                ctx,
                Plain(f"任务 {job_id} 已排队，{eta.text}。完成后会直接交付已验证文件。"),
            )
        try:
            await self._execution_lock.acquire()
            lock_acquired = True
            if queued_counted:
                self._queued_handlers = max(0, self._queued_handlers - 1)
                queued_counted = False
            current = self._job_store.get(job_id)
            if current is None:
                raise ValueError("任务不存在")
            charge_go_usage = False
            if not self._can_manage_runtime(ctx):
                tier = get_tier(sender_id, self._pro_db())
                if tier == Tier.GO:
                    available, reason = agent_available(sender_id, self._pro_db())
                    if not available:
                        self._job_store.finish(
                            job_id, "failed", error_code="agent_quota_exhausted"
                        )
                        self._delete_payload(job_id)
                        yield self._reply(ctx, Plain(reason))
                        return
                    charge_go_usage = True
            self._job_store.transition(
                job_id,
                "executing",
                "executing",
                step_index=payload.step_cursor,
            )
            self._active_job_id = job_id
            self._active_backend = plan.preferred_backend
            self._cancel_requested = False
            started_event = TaskEvent(
                "started", job_id, payload.step_cursor, "started"
            )
            if self._progress_policy.should_emit(started_event, time.monotonic()):
                yield self._reply(
                    ctx,
                    Plain(
                        format_task_reply(
                            "started",
                            f"任务已进入执行队列，{eta.text}。完成后会直接交付已验证文件。",
                        )
                    ),
                )

            output_dir = job_dir / "outputs"
            available_backends = await self._backend_health.available()
            def policy(step: TaskStep):
                decision = assess_step(
                    step,
                    self.work_dir,
                    output_dir,
                    allowed_work_root=self.recovery_root,
                    allowed_output_root=self.workspace,
                )
                # ponytail: unknown steps are approved via assess_step's
                # requires_approval=True; once approved the orchestrator runs
                # them on the host (same path as HIGH_IMPACT). No dead
                # isolation adapter exists, so do not synthesize a blocked
                # decision that would waste the user's approval effort.
                return decision

            def router(step: TaskStep, attempted: frozenset[str]):
                if not trusted_runtime:
                    if BACKEND_CODEX not in available_backends or BACKEND_CODEX in attempted:
                        return BackendRoute(None, "public_sandbox_unavailable")
                    return BackendRoute(BACKEND_CODEX, "selected")
                selected = route_backend(
                    step,
                    plan.preferred_backend,
                    available_backends,
                    attempted,
                )
                approved_backend = approved_steps.get(step_digest(step))
                if approved_backend and selected.backend != approved_backend:
                    return BackendRoute(None, "approval_backend_changed")
                return selected

            async def execute(step: TaskStep, route):
                original_trusted_runtime = getattr(
                    self, "_execution_trusted_runtime", True
                )
                self._execution_trusted_runtime = trusted_runtime
                try:
                    response, deliverables, state, exit_code, _error = await self._execute(
                        job_id,
                        job_dir,
                        step.instruction,
                        str(route.backend),
                        high_risk_approved=step_digest(step) in approved_steps,
                    )
                finally:
                    self._execution_trusted_runtime = original_trusted_runtime
                isolated_attempt = (
                    not trusted_runtime
                    or select_execution_dir(step.instruction, self.work_dir, job_dir)
                    == job_dir.resolve(strict=True)
                )
                started_side_effect = step.action_class is not ActionClass.READ_ONLY
                if isolated_attempt and not deliverables:
                    quarantine_failed_attempt(job_dir, str(route.backend))
                    started_side_effect = False
                effective_exit = exit_code if state == "completed" else (exit_code or 1)
                verification_exit = None
                if state == "completed" and should_run_project_verification(step):
                    verification_root = job_dir if not trusted_runtime else self.work_dir
                    verification_command = select_verification_command(verification_root)
                    if verification_command is not None:
                        verification = await run_verification_command(
                            verification_command,
                            verification_root,
                            timeout=min(self.timeout_seconds, 600),
                        )
                        verification_exit = (
                            verification.exit_code
                            if verification.reason == "completed"
                            else 1
                        )
                return StepExecution(
                    effective_exit,
                    tuple(deliverables),
                    verification_exit,
                    started_side_effect,
                    response=response,
                )

            async def verifier(step: TaskStep, execution: StepExecution):
                return verify_step(
                    step,
                    execution.exit_code,
                    execution.deliverables,
                    execution.verification_exit,
                )

            orchestrator = TaskOrchestrator(
                policy=policy,
                router=router,
                executor=execute,
                verifier=verifier,
                approval_check=lambda step: step_digest(step) in approved_steps,
            )
            outcome = await orchestrator.run(plan, start_index=payload.step_cursor)
            completed_indices = [
                event.step_index
                for event in outcome.events
                if event.kind == "step_completed"
            ]
            next_cursor = (
                max(completed_indices) + 1
                if completed_indices
                else payload.step_cursor
            )
            if outcome.state == "awaiting_approval":
                blocked_event = outcome.events[-1]
                next_cursor = blocked_event.step_index
                payload = self._payload_with_step_cursor(payload, next_cursor)
                self._payload_store.write(job_id, payload)
                self._job_store.record_step(
                    job_id, step_index=next_cursor, step_count=len(plan.steps)
                )
                self._job_store.transition(
                    job_id,
                    "awaiting_approval",
                    "awaiting_approval",
                    step_index=next_cursor,
                )
                step = plan.steps[next_cursor]
                selected_route = route_backend(
                    step,
                    plan.preferred_backend,
                    available_backends,
                    set(),
                )
                if selected_route.backend is None:
                    raise ValueError("任务后端不可用")
                pending = self._approvals.issue(
                    sender_id,
                    scope,
                    step.instruction,
                    selected_route.backend,
                    self.work_dir,
                    task_id=job_id,
                    step_digest=step_digest(step),
                )
                approval_event = TaskEvent(
                    "approval_required", job_id, next_cursor, "approval_required"
                )
                if self._progress_policy.should_emit(
                    approval_event, time.monotonic()
                ):
                    yield self._reply(
                        ctx,
                        Plain(
                            format_task_reply(
                                "approval_required",
                                "高风险步骤尚未执行。",
                                "5 分钟内回复“确认执行”，或发送 "
                                f"/agent approve {pending.token}。",
                            )
                        ),
                    )
                return
            if not outcome.verified:
                self._job_store.finish(
                    job_id,
                    "failed",
                    error_code=outcome.events[-1].code,
                )
                self._delete_payload(job_id)
                detail = "验证未通过，未标记为完成。"
                failed_event = TaskEvent(
                    "failed", job_id, next_cursor, outcome.events[-1].code
                )
                if self._progress_policy.should_emit(failed_event, time.monotonic()):
                    yield self._reply(
                        ctx, Plain(format_task_reply("failed", detail))
                    )
                return

            if charge_go_usage and not use_agent(sender_id, self._pro_db()):
                logger.warning("[LocalAgent] GO usage record could not be committed")

            unique: dict[str, Deliverable] = {}
            for item in outcome.deliverables:
                if isinstance(item, Deliverable):
                    unique[str(item.path.resolve())] = item
            deliverables = list(unique.values())
            payload = self._payload_with_step_cursor(payload, len(plan.steps))
            self._payload_store.write(job_id, payload)
            self._job_store.record_step(
                job_id, step_index=len(plan.steps) - 1, step_count=len(plan.steps)
            )
            self._job_store.transition(job_id, "verifying", "verifying")
            self._job_store.record_delivery(
                job_id, self._manifest_digest(deliverables)
            )
            self._job_store.transition(job_id, "delivering", "delivering")
            response = outcome.responses[-1] if outcome.responses else "验证已通过。"
            delivered_digests: set[str] = set(payload.delivery_cursor)
            all_delivered = True
            for item in deliverables:
                delivered = True
                if item.kind == "image":
                    yield self._reply(ctx, Image(file=str(item.path)))
                elif ctx.get_group_id() and hasattr(ctx, "bot"):
                    delivered = await self._deliver_file(ctx, item.path)
                    if delivered:
                        yield self._reply(ctx, Plain(f"文件已上传到群文件：{item.path.name}"))
                else:
                    yield self._reply(ctx, File(file=str(item.path), name=item.path.name))
                if delivered:
                    delivered_digests.add(self._file_digest(item.path))
                    payload = self._payload_with_cursor(payload, delivered_digests)
                    self._payload_store.write(job_id, payload)
                all_delivered = all_delivered and delivered
            if all_delivered:
                self._job_store.finish(
                    job_id,
                    "completed",
                    exit_code=0,
                    deliverable_count=len(deliverables),
                )
                self._delete_payload(job_id)
                completed_event = TaskEvent(
                    "completed", job_id, len(plan.steps) - 1, "completed"
                )
                if self._progress_policy.should_emit(completed_event, time.monotonic()):
                    yield self._reply(
                        ctx,
                        Plain(format_task_reply("completed", detail=response)),
                    )
            else:
                delivery_event = TaskEvent(
                    "failed", job_id, len(plan.steps) - 1, "delivery_pending"
                )
                if self._progress_policy.should_emit(delivery_event, time.monotonic()):
                    yield self._reply(
                        ctx,
                        Plain(
                            format_task_reply(
                                "failed",
                                "文件未成功交付，已保留恢复记录；服务恢复后只会重试交付，不会重复执行任务。",
                            )
                        ),
                    )
        except Exception as exc:
            try:
                self._job_store.finish(
                    job_id,
                    "failed",
                    error_code=f"planned_{type(exc).__name__}",
                )
            except Exception:
                pass
            self._delete_payload(job_id)
            logger.error(
                f"[LocalAgent] planned job={job_id} failed: {type(exc).__name__}"
            )
            yield self._reply(
                ctx,
                Plain(
                    format_task_reply(
                        "failed", "任务异常终止，错误已按隐私规则记录。"
                    )
                ),
            )
        finally:
            self._active_job_id = None
            self._active_backend = None
            if queued_counted:
                self._queued_handlers = max(0, self._queued_handlers - 1)
            if lock_acquired and self._execution_lock.locked():
                self._execution_lock.release()

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=900)
    async def on_message(self, ctx: Context):
        message = self._command_text(ctx)
        natural_intent = None
        if not message.startswith("/agent"):
            natural_intent = route_natural_agent(self._natural_text(ctx))
            if natural_intent is not None:
                if not self._is_owner(ctx):
                    yield self._reply(
                        ctx,
                        Plain("Agent 需要 GO 或 Pro 资格。发送 /pro status 查看当前资格。")
                    )
                    return
                if natural_intent.action == "run":
                    message = f"/agent run {natural_intent.task}"
                elif natural_intent.action == "confirm":
                    message = "/agent confirm"
                else:
                    message = f"/agent {natural_intent.action}"
        if not message.startswith("/agent"):
            return
        parts = message.split(maxsplit=2)
        action = parts[1].lower() if len(parts) > 1 else "help"

        if not self._is_owner(ctx):
            yield self._reply(
                ctx, Plain("Agent 需要 GO 或 Pro 资格。发送 /pro status 查看当前资格。")
            )
            return
        if action in {"use", "cwd"} and not self._can_manage_runtime(ctx):
            yield self._reply(ctx, Plain("该管理指令仅限小姚使用。"))
            return
        if action in {"status", "cancel"} and not self._is_owner(ctx):
            yield self._reply(ctx, Plain("该操作需要 GO 或 Pro 资格。"))
            return
        if action in {"help", "?"}:
            yield self._reply(
                ctx,
                Plain(
                    self._help_text(
                        trusted_runtime=self._can_manage_runtime(ctx)
                    )
                ),
            )
            return
        if action == "use":
            if len(parts) < 3:
                yield self._reply(ctx, Plain(f"当前后端：{self.backend}"))
                return
            try:
                self.backend = normalize_backend(parts[2])
            except ValueError as exc:
                yield self._reply(ctx, Plain(str(exc)))
                return
            yield self._reply(ctx, Plain(f"已切换到 {self.backend}。"))
            return
        if action == "cwd":
            if len(parts) < 3:
                yield self._reply(ctx, Plain("工作目录已设置；为保护机主隐私，不在聊天中显示绝对路径。"))
                return
            try:
                candidate = validate_work_dir(parts[2])
                if not self._directory_within(candidate, self.recovery_root):
                    raise ValueError("目录不在允许的项目根内")
                self.work_dir = candidate
            except ValueError as exc:
                yield self._reply(ctx, Plain(f"目录未切换：{exc}"))
                return
            yield self._reply(ctx, Plain("工作目录已切换；绝对路径不会出现在聊天回复中。"))
            return
        if action == "status":
            running = self._active_job_id or "无"
            active = f" · {self._active_backend}" if self._active_backend else ""
            yield self._reply(ctx, Plain(f"任务：{running}{active}\n后端：{self.backend}\n工作目录：已设置（路径已隐藏）"))
            return
        if action == "cancel":
            stopped = await self._stop_process()
            yield self._reply(ctx, Plain("已请求取消当前任务。" if stopped else "当前没有可取消的任务。"))
            return
        if action not in {"run", "approve", "confirm"} or (
            action in {"run", "approve"} and len(parts) < 3
        ):
            yield self._reply(ctx, Plain(self._help_text()))
            return
        async for reply in self._handle_planned_action(
            ctx, action, parts, natural_intent
        ):
            yield reply
        return

    async def terminate(self):
        """Stop the full child process tree during a graceful AstrBot shutdown/reload."""
        await self._stop_process()
        if self._recovery_task is not None and not self._recovery_task.done():
            self._recovery_task.cancel()


# AstrBot discovers the Star subclass in this module automatically.
