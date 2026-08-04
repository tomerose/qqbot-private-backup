import asyncio
import contextlib
import hashlib
import importlib
import os
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

_PROJ_ROOT = Path(__file__).resolve().parents[1]
os.environ["ASTRBOT_ROOT"] = str(_PROJ_ROOT / "astrbot")

from astrbot.api.message_components import At, File, Image, Plain

PLUGINS_DIR = _PROJ_ROOT / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS_DIR))

from claude_code_agent.agent_core import (  # noqa: E402
    ApprovalRegistry,
    BACKEND_CLAUDE,
    Deliverable,
    create_job_dir,
)
from claude_code_agent.encrypted_payload_store import (  # noqa: E402
    EncryptedJobPayload,
    EncryptedPayloadStore,
)
from claude_code_agent.job_store import JobStore  # noqa: E402
from claude_code_agent.main import ClaudeCodeAgent  # noqa: E402
from claude_code_agent.task_planner import TaskRequest, plan_task  # noqa: E402
from claude_code_agent.progress_policy import ProgressPolicy  # noqa: E402
from claude_code_agent.access_policy import AccessPolicy  # noqa: E402
from claude_code_agent.trusted_policy import TrustedPolicy  # noqa: E402
from claude_code_agent import file_cache  # noqa: E402


class FakeBackendHealth:
    def __init__(self, available=("claude", "codex", "workbuddy")):
        self.backends = frozenset(available)

    async def available(self):
        return self.backends


class FakeMessageObject:
    def __init__(self, text, *, self_id="3806573022", components=None):
        self.self_id = self_id
        self.message = components if components is not None else [Plain(text)]


class FakeEvent:
    def __init__(
        self,
        text,
        *,
        sender="1211000567",
        group_id="",
        at_self=False,
        fail_send=False,
    ):
        self._text = text
        self._sender = sender
        self._group_id = group_id
        components = [Plain(text)]
        if at_self:
            components.insert(0, At(qq="3806573022"))
        self.message_obj = FakeMessageObject(text, components=components)
        self.unified_msg_origin = (
            f"aiocqhttp:GroupMessage:{group_id}"
            if group_id
            else "aiocqhttp:FriendMessage:1211000567"
        )
        self._extra = {}
        self._result = None
        self.sent = []
        self.fail_send = fail_send
        self.bot = FakeOneBot()

    def get_message_text(self):
        return self._text

    def get_sender_id(self):
        return self._sender

    def get_group_id(self):
        return self._group_id

    def reply(self, component):
        return component

    async def send(self, chain):
        if self.fail_send:
            raise RuntimeError("component delivery unavailable")
        self.sent.append(chain)

    def chain_result(self, chain):
        return chain[0] if len(chain) == 1 else chain

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value

    def get_result(self):
        return self._result


class RealShapeEvent(FakeEvent):
    """Match the current AstrBot event text API used by aiocqhttp."""

    get_message_text = None
    reply = None

    def __init__(self, text, **kwargs):
        super().__init__(text, **kwargs)
        self.message_str = text

    def get_message_str(self):
        return self.message_str

class FakeStarContext:
    def __init__(self):
        self.sent = []
        self.platforms = {}
        self.fail_next_file = False

    async def send_message(self, session, chain):
        self.sent.append((session, chain))
        if self.fail_next_file and any(
            isinstance(component, File) for component in getattr(chain, "chain", [])
        ):
            self.fail_next_file = False
            return False
        return True

    def get_platform_inst(self, platform_id):
        return self.platforms.get(platform_id)


class FakeOneBot:
    def __init__(self, *, fail_upload=False, fail_private=False):
        self.actions = []
        self.fail_upload = fail_upload
        self.fail_private = fail_private

    async def call_action(self, action, **kwargs):
        self.actions.append((action, kwargs))
        if action == "upload_group_file" and self.fail_upload:
            raise RuntimeError("upload unavailable")
        if action in {"send_private_msg", "upload_private_file"} and self.fail_private:
            raise RuntimeError("private delivery unavailable")


class FakePlatform:
    def __init__(self, bot):
        self.bot = bot

    def get_client(self):
        return self.bot


def _plain_texts(items):
    return [item.text for item in items if isinstance(item, Plain)]


async def _collect(generator):
    return [item async for item in generator]


class AgentIntegrationTests(unittest.TestCase):
    def test_agent_job_status_mirror_uses_injected_tracker(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))
                calls = []
                plugin._task_tracker = lambda *args: calls.append(args)
                await plugin._track_cross_dialog_job(
                    "1211000567", "abc123", "制作报告", "delivery_pending", "not delivered"
                )
                self.assertEqual(
                    calls,
                    [("1211000567", "abc123", "制作报告", "delivery_pending", "not delivered")],
                )

        asyncio.run(scenario())

    def test_delivery_pending_survives_restart_for_queue_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = self._plugin(Path(tmp))
            job_id = "aabbccddeeff"
            plugin._job_store.start(
                job_id,
                "1211000567",
                "aiocqhttp:FriendMessage:1211000567",
                "生成报告",
                "claude",
                "",
                state="delivery_pending",
                recovery="replay_safe",
            )

            self.assertEqual(plugin._job_store.recover_interrupted(), 0)
            self.assertEqual(plugin._job_store.get(job_id)["state"], "delivery_pending")

    def test_queue_success_completes_agent_ledger_and_deletes_payload(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))
                job_id = "aabbccddeeff"
                artifact = plugin.workspace / "jobs" / job_id / "outputs" / "report.txt"
                artifact.parent.mkdir(parents=True)
                artifact.write_text("report", encoding="utf-8")
                plugin._job_store.start(
                    job_id,
                    "1211000567",
                    "aiocqhttp:FriendMessage:1211000567",
                    "生成报告",
                    "claude",
                    "",
                    state="delivery_pending",
                    recovery="replay_safe",
                )
                plugin._payload_store.write(
                    job_id,
                    EncryptedJobPayload(
                        "生成报告",
                        "aiocqhttp:FriendMessage:1211000567",
                        "claude",
                        "project",
                        "replay_safe",
                    ),
                )
                entry = types.SimpleNamespace(
                    job_id=job_id, task_owner="agent", local_path=str(artifact)
                )

                await plugin._on_queued_delivery_outcome(
                    entry, "done", "qq:retry_queue"
                )

                record = plugin._job_store.get(job_id)
                self.assertEqual(record["state"], "completed")
                self.assertEqual(record["deliverable_count"], 1)
                self.assertFalse(plugin._payload_store.exists(job_id))

        asyncio.run(scenario())

    def test_declared_gif_video_is_not_mistaken_for_an_untrusted_local_image(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))
                video = plugin.workspace / "pro_video" / "search-result.gif"
                video.parent.mkdir(parents=True)
                video.write_bytes(b"GIF89a")
                image = Image.fromFileSystem(str(video))
                event = FakeEvent("/findvideo cat", sender="999")
                event._result = types.SimpleNamespace(chain=[image])
                event.set_extra("_pro_video_output_paths", [str(video)])

                await plugin.protect_privacy_and_deliver_files(event)

                self.assertEqual(event.get_result().chain, [image])

        asyncio.run(scenario())

    def test_current_astrbot_event_text_api_is_supported(self):
        event = RealShapeEvent("帮我生成一个只含 hello 的 txt")

        self.assertEqual(
            ClaudeCodeAgent._natural_text(event),
            "帮我生成一个只含 hello 的 txt",
        )

    def test_current_astrbot_event_result_api_is_supported(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))

                await _collect(
                    plugin.on_message(
                        RealShapeEvent("帮我生成一个只含 hello 的 txt")
                    )
                )

                self.assertEqual(len(plugin.executed), 1)

        asyncio.run(scenario())

    def test_cached_file_request_delivers_without_starting_agent(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))
                cached = plugin.workspace / "jobs" / "old" / "outputs" / "report.txt"
                cached.parent.mkdir(parents=True)
                cached.write_text("report", encoding="utf-8")
                cache_path = Path(tmp) / "file_cache.json"
                with patch.object(file_cache, "_CACHE_PATH", cache_path):
                    file_cache.record_file(
                        "生成报告", str(cached), sender_id="1211000567", job_id="old"
                    )
                    replies = await _collect(
                        plugin.on_message(FakeEvent("把刚才那个报告文件再发我一下"))
                    )

                self.assertEqual(plugin.executed, [])
                self.assertTrue(any("已发送" in text for text in _plain_texts(replies)))

        asyncio.run(scenario())

    def test_invalid_agent_command_returns_help_instead_of_type_error(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))

                replies = await _collect(plugin.on_message(FakeEvent("/agent run")))

                self.assertEqual(plugin.executed, [])
                self.assertTrue(any("Agent 任务" in text for text in _plain_texts(replies)))

        asyncio.run(scenario())

    def test_natural_followup_status_lists_pending_delivery_job(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))
                plugin._job_store.start(
                    "a1b2c3d4e5f6",
                    "1211000567",
                    "aiocqhttp:FriendMessage:1211000567",
                    "生成报告",
                    "claude",
                    "",
                    state="delivering",
                    recovery="replay_safe",
                )

                replies = await _collect(
                    plugin.on_message(FakeEvent("刚才那个任务文件发了吗"))
                )
                texts = _plain_texts(replies)

                self.assertEqual(plugin.executed, [])
                self.assertTrue(any("待处理" in text for text in texts), texts)
                self.assertTrue(any("a1b2c3d4e5f6" in text for text in texts), texts)

        asyncio.run(scenario())

    def test_completed_task_uses_bounded_experience_reply(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))

                async def verbose_execute(*_args, **_kwargs):
                    return "内部详情。" * 200, [], "completed", 0, ""

                plugin._execute = verbose_execute
                replies = await _collect(
                    plugin.on_message(FakeEvent("帮我读取项目状态"))
                )
                texts = _plain_texts(replies)

                self.assertTrue(texts[-1].startswith("已完成。"), texts[-1])
                self.assertLessEqual(len(texts[-1]), 500)

        asyncio.run(scenario())

    def _plugin(self, root: Path):
        plugin = ClaudeCodeAgent.__new__(ClaudeCodeAgent)
        plugin.context = FakeStarContext()
        plugin.context.platforms["aiocqhttp"] = FakePlatform(FakeOneBot())
        plugin.config = {}
        plugin._access_policy = AccessPolicy(["1211000567"])
        plugin._trusted_policy = TrustedPolicy(["1211000567"])
        plugin.workspace = root / "workspace"
        plugin.workspace.mkdir(parents=True)
        plugin.recovery_root = root / "allowed"
        plugin.work_dir = plugin.recovery_root / "project"
        plugin.work_dir.mkdir(parents=True)
        plugin.backend = BACKEND_CLAUDE
        plugin.codex_model = "test-model"
        plugin.timeout_seconds = 60
        plugin.max_attachment_files = 10
        plugin.max_attachment_bytes = 1024 * 1024
        plugin.max_queued_jobs = 3
        plugin._queued_handlers = 0
        plugin._execution_lock = asyncio.Lock()
        plugin._active_job_id = None
        plugin._active_backend = None
        plugin._active_proc = None
        plugin._cancel_requested = False
        plugin._approvals = ApprovalRegistry(ttl_seconds=300)
        plugin._progress_policy = ProgressPolicy()
        plugin._backend_health = FakeBackendHealth()
        plugin._job_store = JobStore(plugin.workspace / "state" / "jobs.db")
        plugin._payload_store = EncryptedPayloadStore(plugin.workspace / "state" / "private_jobs")
        plugin.executed = []

        async def fake_execute(this, job_id, job_dir, task, backend, high_risk_approved):
            this.executed.append((job_id, task, backend, high_risk_approved, this.work_dir))
            deliverables = []
            if "生成" in task:
                if ".docx" in task.lower() or "word" in task.lower():
                    artifact = job_dir / "outputs" / "report.docx"
                    with zipfile.ZipFile(artifact, "w") as archive:
                        archive.writestr(
                            "word/document.xml", "<document>generated result</document>"
                        )
                else:
                    artifact = job_dir / "outputs" / "result.txt"
                    artifact.write_text("generated result", encoding="utf-8")
                deliverables.append(Deliverable(artifact, "file"))
            return (
                f"任务 {job_id} 执行结束（退出码 0）",
                deliverables,
                "completed",
                0,
                "",
            )

        plugin._execute = types.MethodType(fake_execute, plugin)
        return plugin

    def test_unhealthy_preferred_backend_falls_back_before_execution(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))
                plugin._backend_health = FakeBackendHealth(("codex",))

                await _collect(plugin.on_message(FakeEvent("帮我生成 backend-report.txt")))

                self.assertEqual(len(plugin.executed), 1)
                self.assertEqual(plugin.executed[0][2], "codex")

        asyncio.run(scenario())

    def test_failed_claude_isolated_artifact_falls_back_once_to_codex(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))
                backends = []
                job_ids = []

                async def fail_then_succeed(
                    this, job_id, job_dir, task, backend, high_risk_approved
                ):
                    backends.append(backend)
                    job_ids.append(job_id)
                    if backend == "claude":
                        (job_dir / "partial.txt").write_text(
                            "partial", encoding="utf-8"
                        )
                        return "Claude unavailable", [], "failed", 1, "nonzero_exit"
                    artifact = job_dir / "outputs" / "result.txt"
                    artifact.write_text("complete", encoding="utf-8")
                    return (
                        "Codex completed",
                        [Deliverable(artifact, "file")],
                        "completed",
                        0,
                        "",
                    )

                plugin._execute = types.MethodType(fail_then_succeed, plugin)

                replies = await _collect(
                    plugin.on_message(FakeEvent("帮我生成 result.txt"))
                )

                self.assertEqual(backends, ["claude", "codex"])
                self.assertTrue(
                    any(text.startswith("已完成") for text in _plain_texts(replies))
                )
                job_id = job_ids[0]
                quarantines = list(
                    (plugin.workspace / "jobs" / job_id / "failed-attempts").glob(
                        "claude-*"
                    )
                )
                self.assertEqual(len(quarantines), 1)
                self.assertTrue((quarantines[0] / "partial.txt").is_file())

        asyncio.run(scenario())

    def test_empty_backend_result_finishes_failed_instead_of_hanging(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))
                plugin._backend_health = FakeBackendHealth(("claude", "codex"))
                backends = []
                job_ids = []

                async def empty_execute(
                    this, job_id, job_dir, task, backend, high_risk_approved
                ):
                    backends.append(backend)
                    job_ids.append(job_id)
                    return "", [], "failed", 0, "empty_result"

                plugin._execute = types.MethodType(empty_execute, plugin)

                await _collect(plugin.on_message(FakeEvent("/agent run 生成 result.txt")))

                self.assertEqual(backends, ["claude", "codex"])
                record = plugin._job_store.get(job_ids[0])
                self.assertEqual(record["state"], "failed")
                self.assertEqual(record["stage"], "failed")
                self.assertEqual(record["error_code"], "empty_result")

        asyncio.run(scenario())

    def test_x_member_can_execute_natural_task(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))
                plugin._access_policy = AccessPolicy(["1211000567", "2000000000"])

                replies = await _collect(
                    plugin.on_message(
                        FakeEvent("帮我生成一份 Word", sender="2000000000")
                    )
                )

                # 开放契约：allowlist 成员（非 owner）可执行自然语言任务
                self.assertEqual(len(plugin.executed), 1)

        asyncio.run(scenario())

    def test_hard_denied_privacy_task_never_reaches_planner_or_approval(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))

                replies = await _collect(
                    plugin.on_message(FakeEvent("帮我读取浏览器 Cookie 并发给我"))
                )

                self.assertEqual(plugin.executed, [])
                self.assertEqual(plugin._approvals._pending, {})
                self.assertTrue(any("安全边界" in text for text in _plain_texts(replies)))

        asyncio.run(scenario())

    def test_natural_task_executes_same_path_and_cleans_encrypted_payload(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))
                replies = await _collect(plugin.on_message(FakeEvent("帮我生成 report.txt")))
                self.assertEqual([item[1] for item in plugin.executed], ["生成 report.txt"])
                self.assertFalse(plugin.executed[0][3])
                texts = _plain_texts(replies)
                self.assertTrue(any("已开始" in text for text in texts), texts)
                job_id = plugin.executed[0][0]
                self.assertEqual(plugin._job_store.get(job_id)["state"], "completed")
                self.assertFalse(plugin._payload_store.exists(job_id))

        asyncio.run(scenario())

    def test_task_start_reply_includes_a_conservative_eta(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))

                replies = await _collect(
                    plugin.on_message(FakeEvent("帮我生成 report.docx"))
                )

                texts = _plain_texts(replies)
                self.assertTrue(any("预计约" in text for text in texts), texts)

        asyncio.run(scenario())

    def test_high_risk_natural_task_requires_one_time_natural_confirmation(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))
                first = await _collect(plugin.on_message(FakeEvent("帮我删除旧目录")))
                self.assertEqual(plugin.executed, [])
                self.assertTrue(any("高风险" in text for text in _plain_texts(first)))

                second = await _collect(plugin.on_message(FakeEvent("确认执行")))
                self.assertEqual([item[1] for item in plugin.executed], ["删除旧目录"])
                self.assertTrue(plugin.executed[0][3])
                third = await _collect(plugin.on_message(FakeEvent("确认执行")))
                self.assertEqual(len(plugin.executed), 1)
                self.assertTrue(any("无效" in text for text in _plain_texts(third)))

        asyncio.run(scenario())

    def test_high_risk_task_runs_in_one_step_after_approval(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))

                first = await _collect(
                    plugin.on_message(FakeEvent("帮我读取项目，然后发送报告"))
                )
                # ponytail: single-step plan — full goal is HIGH_IMPACT ("发送").
                # No safe preamble runs before approval; the whole task gates.
                self.assertEqual(plugin.executed, [])
                self.assertTrue(any("确认" in text for text in _plain_texts(first)))

                second = await _collect(plugin.on_message(FakeEvent("确认执行")))
                # After approval the single step executes on host.
                self.assertEqual(len(plugin.executed), 1)
                self.assertIn("读取项目", plugin.executed[0][1])
                self.assertIn("发送报告", plugin.executed[0][1])
                self.assertTrue(any("执行结束" in text for text in _plain_texts(second)))

        asyncio.run(scenario())

    def test_unknown_step_executes_on_host_after_approval(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))

                first = await _collect(
                    plugin.on_message(FakeEvent("帮我处理一下这个项目"))
                )
                self.assertEqual(plugin.executed, [])
                self.assertTrue(any("确认" in text for text in _plain_texts(first)))

                second = await _collect(plugin.on_message(FakeEvent("确认执行")))

                self.assertTrue(len(plugin.executed) > 0)
                self.assertFalse(
                    any("隔离" in text for text in _plain_texts(second)),
                )

        asyncio.run(scenario())

    def test_high_risk_approval_is_bound_to_the_routed_backend(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))
                plugin.backend = "claude"

                await _collect(plugin.on_message(FakeEvent("帮我部署代码")))

                pending = next(iter(plugin._approvals._pending.values()))
                self.assertEqual(pending.backend, "claude")

        asyncio.run(scenario())

    def test_planned_exception_deletes_encrypted_payload(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))

                async def crash(*_args, **_kwargs):
                    raise RuntimeError("private task content")

                plugin._execute = crash
                replies = await _collect(
                    plugin.on_message(FakeEvent("帮我读取项目"))
                )

                self.assertEqual(
                    list((plugin.workspace / "state" / "private_jobs").glob("*.bin")),
                    [],
                )
                self.assertFalse(
                    any("private task content" in text for text in _plain_texts(replies))
                )

        asyncio.run(scenario())

    def test_full_queue_rejects_before_creating_private_payload(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))
                await plugin._execution_lock.acquire()
                plugin._queued_handlers = plugin.max_queued_jobs
                try:
                    replies = await _collect(
                        plugin.on_message(FakeEvent("帮我读取项目"))
                    )
                finally:
                    plugin._execution_lock.release()

                self.assertTrue(any("队列已满" in text for text in _plain_texts(replies)))
                self.assertEqual(
                    list((plugin.workspace / "state" / "private_jobs").glob("*.bin")),
                    [],
                )

        asyncio.run(scenario())

    def test_non_owner_and_group_without_real_at_never_execute(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))
                ordinary = await _collect(
                    plugin.on_message(FakeEvent("帮我生成报告", sender="999"))
                )
                self.assertFalse(any("X 或 PRO" in text for text in _plain_texts(ordinary)))
                # 开放契约：普通用户得到澄清追问而非 tier 拒绝，但仍不直接执行
                self.assertEqual(plugin.executed, [])
                self.assertTrue(any("还是做成" in text for text in _plain_texts(ordinary)))
                self.assertEqual(
                    await _collect(
                        plugin.on_message(FakeEvent("帮我生成报告", group_id="123", at_self=False))
                    ),
                    [],
                )
                self.assertEqual(plugin.executed, [])

        asyncio.run(scenario())

    def test_ordinary_natural_agent_request_gets_clarification_without_execution(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))

                replies = await _collect(
                    plugin.on_message(
                        FakeEvent("帮我生成一个报告", sender="2000000000")
                    )
                )

                self.assertEqual(plugin.executed, [])
                self.assertTrue(
                    any("还是做成" in text for text in _plain_texts(replies)),
                    _plain_texts(replies),
                )

        asyncio.run(scenario())

    def test_restart_resumes_safe_job_and_blocks_non_replayable_job(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))
                safe_id = "a1b2c3d4e5f6"
                blocked_id = "b1b2c3d4e5f6"
                create_job_dir(plugin.workspace, safe_id)
                create_job_dir(plugin.workspace, blocked_id)
                plugin._job_store.start(
                    safe_id, "1211000567", "aiocqhttp:FriendMessage:1211000567", "读取项目并生成报告",
                    "claude", "", state="running", recovery="replay_safe"
                )
                plugin._job_store.start(
                    blocked_id, "1211000567", "aiocqhttp:FriendMessage:1211000567", "删除旧目录",
                    "claude", "删除", state="running", recovery="blocked"
                )
                plugin._payload_store.write(
                    safe_id,
                    EncryptedJobPayload(
                        "读取项目并生成报告", "aiocqhttp:FriendMessage:1211000567", "claude", "project", "replay_safe"
                    ),
                )
                plugin._payload_store.write(
                    blocked_id,
                    EncryptedJobPayload(
                        "删除旧目录", "aiocqhttp:FriendMessage:1211000567", "claude", ".", "blocked"
                    ),
                )
                plugin._job_store.recover_interrupted()

                await plugin._recover_jobs()

                self.assertEqual([item[0] for item in plugin.executed], [safe_id])
                self.assertEqual(plugin._job_store.get(safe_id)["state"], "completed")
                self.assertEqual(plugin._job_store.get(blocked_id)["state"], "recovery_blocked")
                self.assertFalse(plugin._payload_store.exists(safe_id))
                self.assertFalse(plugin._payload_store.exists(blocked_id))
                self.assertEqual(
                    {session for session, _chain in plugin.context.sent},
                    {"aiocqhttp:FriendMessage:1211000567"},
                )

        asyncio.run(scenario())

    def test_second_task_waits_in_bounded_queue_and_then_executes(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))
                first_started = asyncio.Event()
                release_first = asyncio.Event()

                async def controlled_execute(this, job_id, job_dir, task, backend, high_risk_approved):
                    this.executed.append((job_id, task, backend, high_risk_approved, this.work_dir))
                    if task == "生成第一个 report.docx":
                        first_started.set()
                        await release_first.wait()
                    artifact = job_dir / "outputs" / "report.docx"
                    artifact.write_bytes(b"verified test artifact")
                    return (
                        f"任务 {job_id} 执行结束（退出码 0）",
                        [Deliverable(artifact, "file")], "completed", 0, "",
                    )

                plugin._execute = types.MethodType(controlled_execute, plugin)
                first = asyncio.create_task(
                    _collect(plugin.on_message(FakeEvent("帮我生成第一个 report.docx")))
                )
                await asyncio.wait_for(first_started.wait(), timeout=2)
                second = asyncio.create_task(
                    _collect(plugin.on_message(FakeEvent("帮我生成第二个 report.docx")))
                )
                await asyncio.sleep(0.05)
                self.assertFalse(second.done())
                release_first.set()
                first_replies, second_replies = await asyncio.gather(first, second)

                self.assertEqual(
                    [item[1] for item in plugin.executed],
                    ["生成第一个 report.docx", "生成第二个 report.docx"],
                )
                self.assertTrue(
                    any("已排队" in text for text in _plain_texts(second_replies)),
                    _plain_texts(second_replies),
                )
                self.assertTrue(any("已开始" in text for text in _plain_texts(first_replies)))

        asyncio.run(scenario())

    def test_queued_job_survives_restart_and_executes_once(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))
                job_id = "c1b2c3d4e5f6"
                create_job_dir(plugin.workspace, job_id)
                plugin._job_store.start(
                    job_id,
                    "1211000567",
                    "aiocqhttp:FriendMessage:1211000567",
                    "读取项目并生成排队报告",
                    "claude",
                    "",
                    state="queued",
                    recovery="replay_safe",
                )
                plugin._payload_store.write(
                    job_id,
                    EncryptedJobPayload(
                        "读取项目并生成排队报告",
                        "aiocqhttp:FriendMessage:1211000567",
                        "claude",
                        "project",
                        "replay_safe",
                    ),
                )

                await plugin._recover_jobs()

                self.assertEqual([item[0] for item in plugin.executed], [job_id])
                self.assertEqual(plugin._job_store.get(job_id)["state"], "completed")
                self.assertFalse(plugin._payload_store.exists(job_id))

        asyncio.run(scenario())

    def test_planned_read_only_recovery_replays_single_step(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))
                job_id = "d1e2f3a4b5c6"
                create_job_dir(plugin.workspace, job_id)
                task = "读取项目，然后总结结果"
                # ponytail: single-step plan — full task text is one invocation.
                plan = plan_task(TaskRequest(job_id, task, "claude"))
                self.assertEqual(len(plan.steps), 1)
                plugin._job_store.start(
                    job_id,
                    "1211000567",
                    "aiocqhttp:FriendMessage:1211000567",
                    task,
                    "claude",
                    "planned",
                    state="executing",
                    recovery="replay_safe",
                    step_index=0,
                    step_count=1,
                )
                plugin._payload_store.write(
                    job_id,
                    EncryptedJobPayload(
                        task=task,
                        scope="aiocqhttp:FriendMessage:1211000567",
                        backend="claude",
                        work_dir_relative="project",
                        recovery="replay_safe",
                        plan=plugin._plan_records(plan),
                        step_cursor=0,
                    ),
                )
                plugin._job_store.recover_interrupted()

                await plugin._recover_jobs()

                self.assertEqual(len(plugin.executed), 1)
                self.assertIn(task, plugin.executed[0][1])
                self.assertEqual(plugin._job_store.get(job_id)["state"], "completed")
                self.assertFalse(plugin._payload_store.exists(job_id))

        asyncio.run(scenario())

    def test_delivering_restart_skips_execution_and_deduplicates_files(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))
                job_id = "d1b2c3d4e5f6"
                job_dir = create_job_dir(plugin.workspace, job_id)
                first = job_dir / "outputs" / "first.txt"
                second = job_dir / "outputs" / "second.txt"
                first.write_text("first", encoding="utf-8")
                second.write_text("second", encoding="utf-8")
                first_digest = hashlib.sha256(first.read_bytes()).hexdigest()

                bot = FakeOneBot()
                plugin.context.platforms["llbot-test"] = FakePlatform(bot)
                plugin._job_store.start(
                    job_id,
                    "1211000567",
                    "llbot-test:FriendMessage:1211000567",
                    "生成两个报告",
                    "claude",
                    "",
                    state="running",
                    recovery="replay_safe",
                )
                plugin._job_store.transition(job_id, "verifying", "verifying")
                plugin._job_store.record_delivery(job_id, "f" * 64)
                plugin._job_store.transition(job_id, "delivering", "delivering")
                plugin._payload_store.write(
                    job_id,
                    EncryptedJobPayload(
                        "生成两个报告",
                        "llbot-test:FriendMessage:1211000567",
                        "claude",
                        "project",
                        "replay_safe",
                        (first_digest,),
                    ),
                )
                plugin._job_store.recover_interrupted()

                await plugin._recover_jobs()

                self.assertEqual(plugin.executed, [])
                self.assertFalse(
                    any(
                        isinstance(component, File)
                        for _scope, chain in plugin.context.sent
                        for component in getattr(chain, "chain", [])
                    )
                )
                self.assertEqual(len(bot.actions), 1)
                action, kwargs = bot.actions[0]
                self.assertEqual(action, "upload_private_file")
                self.assertEqual(kwargs["user_id"], 1211000567)
                self.assertEqual(kwargs["name"], "second.txt")
                self.assertEqual(plugin._job_store.get(job_id)["state"], "completed")
                self.assertFalse(plugin._payload_store.exists(job_id))

        asyncio.run(scenario())

    def test_normal_file_delivery_records_manifest_before_completion(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))

                async def file_execute(this, job_id, job_dir, task, backend, high_risk_approved):
                    path = job_dir / "outputs" / "answer.txt"
                    path.write_text("answer", encoding="utf-8")
                    this.executed.append((job_id, task, backend, high_risk_approved, this.work_dir))
                    return "执行结束", [Deliverable(path, "file")], "completed", 0, ""

                plugin._execute = types.MethodType(file_execute, plugin)
                await _collect(plugin.on_message(FakeEvent("帮我生成答案文件")))
                job_id = plugin.executed[0][0]
                record = plugin._job_store.get(job_id)
                self.assertEqual(record["state"], "completed")
                self.assertEqual(record["stage"], "completed")
                self.assertRegex(record["delivery_digest"], r"^[0-9a-f]{64}$")
                self.assertFalse(plugin._payload_store.exists(job_id))

        asyncio.run(scenario())

    def test_group_upload_failure_falls_back_to_requester_private_chat(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))
                event = FakeEvent(
                    "帮我生成 report.docx", group_id="945598390", at_self=True
                )
                event.bot = FakeOneBot(fail_upload=True)

                replies = await _collect(plugin.on_message(event))
                texts = _plain_texts(replies)
                job_id = plugin.executed[0][0]

                self.assertTrue(any("已私聊发送" in text for text in texts), texts)
                self.assertEqual(plugin._job_store.get(job_id)["state"], "completed")
                self.assertFalse(plugin._payload_store.exists(job_id))
                self.assertEqual(
                    [action for action, _ in event.bot.actions],
                    [
                        "upload_group_file",
                        "upload_group_file",
                        "upload_group_file",
                        "upload_private_file",
                    ],
                )
                self.assertEqual(event.bot.actions[-1][1]["user_id"], 1211000567)

        asyncio.run(scenario())

    def test_all_delivery_paths_fail_without_reporting_generation_failure(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))
                event = FakeEvent(
                    "帮我生成 report.docx",
                    group_id="945598390",
                    at_self=True,
                    fail_send=True,
                )
                event.bot = FakeOneBot(fail_upload=True, fail_private=True)

                queue_modules = []
                for name in (
                    "data.plugins.friend_core.delivery_queue",
                    "friend_core.delivery_queue",
                ):
                    try:
                        queue_modules.append(importlib.import_module(name))
                    except ImportError:
                        pass
                with contextlib.ExitStack() as stack:
                    patched = [
                        stack.enter_context(patch.object(module, "get_queue"))
                        for module in queue_modules
                    ]
                    for get_queue in patched:
                        get_queue.return_value.enqueue.return_value = None
                    replies = await _collect(plugin.on_message(event))
                texts = _plain_texts(replies)
                job_id = plugin.executed[0][0]

                self.assertFalse(any("任务执行失败" in text for text in texts), texts)
                self.assertTrue(any("文件未成功交付" in text for text in texts), texts)
                self.assertEqual(plugin._job_store.get(job_id)["state"], "delivering")
                self.assertTrue(plugin._payload_store.exists(job_id))

        asyncio.run(scenario())

    def test_recovered_group_file_uses_onebot_group_upload(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))
                bot = FakeOneBot()
                plugin.context.platforms["llbot-test"] = FakePlatform(bot)
                job_id = "a1b2c3d4e5f6"
                job_dir = create_job_dir(plugin.workspace, job_id)
                report = job_dir / "outputs" / "report.txt"
                report.write_text("report", encoding="utf-8")
                payload = EncryptedJobPayload(
                    "生成报告",
                    "llbot-test:GroupMessage:945598390",
                    "claude",
                    "project",
                    "replay_safe",
                )

                delivered = await plugin._deliver_recovered_files(
                    payload.scope,
                    job_id,
                    payload,
                    [Deliverable(report, "file")],
                )

                self.assertTrue(delivered)
                self.assertEqual(len(bot.actions), 1)
                action, kwargs = bot.actions[0]
                self.assertEqual(action, "upload_group_file")
                self.assertEqual(kwargs["group_id"], 945598390)
                self.assertEqual(kwargs["name"], "report.txt")
                self.assertEqual(plugin.context.sent, [])

        asyncio.run(scenario())

    def test_recovered_private_file_uses_onebot_private_upload(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))
                bot = FakeOneBot()
                plugin.context.platforms["llbot-test"] = FakePlatform(bot)
                job_id = "a1b2c3d4e5f6"
                job_dir = create_job_dir(plugin.workspace, job_id)
                report = job_dir / "outputs" / "report.txt"
                report.write_text("report", encoding="utf-8")
                payload = EncryptedJobPayload(
                    "生成报告",
                    "llbot-test:FriendMessage:1211000567",
                    "claude",
                    "project",
                    "replay_safe",
                )

                delivered = await plugin._deliver_recovered_files(
                    payload.scope,
                    job_id,
                    payload,
                    [Deliverable(report, "file")],
                )

                self.assertTrue(delivered)
                self.assertEqual(len(bot.actions), 1)
                action, kwargs = bot.actions[0]
                self.assertEqual(action, "upload_private_file")
                self.assertEqual(kwargs["user_id"], 1211000567)
                self.assertEqual(kwargs["name"], "report.txt")
                self.assertEqual(plugin.context.sent, [])

        asyncio.run(scenario())

    def test_replayed_job_commits_manifest_before_delivery_and_never_reexecutes(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))
                job_id = "b1c2d3e4f5a6"
                job_dir = create_job_dir(plugin.workspace, job_id)
                plugin._job_store.start(
                    job_id,
                    "1211000567",
                    "aiocqhttp:FriendMessage:1211000567",
                    "读取并解释项目",
                    "claude",
                    "",
                    state="running",
                    recovery="replay_safe",
                )
                plugin._payload_store.write(
                    job_id,
                    EncryptedJobPayload(
                        "读取并解释项目",
                        "aiocqhttp:FriendMessage:1211000567",
                        "claude",
                        "project",
                        "replay_safe",
                    ),
                )
                plugin._job_store.recover_interrupted()

                async def execute_with_file(
                    this, current_job_id, current_job_dir, task, backend, high_risk_approved
                ):
                    report = current_job_dir / "outputs" / "report.txt"
                    report.write_text("safe report", encoding="utf-8")
                    this.executed.append(current_job_id)
                    return "执行结束", [Deliverable(report, "file")], "completed", 0, ""

                plugin._execute = types.MethodType(execute_with_file, plugin)
                bot = plugin.context.platforms["aiocqhttp"].bot
                bot.fail_private = True
                await plugin._recover_jobs()

                first_record = plugin._job_store.get(job_id)
                self.assertRegex(first_record["delivery_digest"], r"^[0-9a-f]{64}$")
                self.assertEqual(plugin.executed, [job_id])
                self.assertTrue(plugin._payload_store.exists(job_id))

                plugin._job_store.recover_interrupted()

                async def must_not_execute(*_args, **_kwargs):
                    raise AssertionError("delivery restart must not execute the agent")

                plugin._execute = must_not_execute
                bot.fail_private = False
                await plugin._recover_jobs()

                self.assertEqual(plugin._job_store.get(job_id)["state"], "completed")
                self.assertFalse(plugin._payload_store.exists(job_id))

        asyncio.run(scenario())

    def test_replayed_group_job_uses_onebot_for_new_file(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))
                bot = FakeOneBot()
                plugin.context.platforms["llbot-test"] = FakePlatform(bot)
                job_id = "c1d2e3f4a5b6"
                job_dir = create_job_dir(plugin.workspace, job_id)
                scope = "llbot-test:GroupMessage:945598390"
                plugin._job_store.start(
                    job_id,
                    "1211000567",
                    scope,
                    "读取并解释项目",
                    "claude",
                    "",
                    state="running",
                    recovery="replay_safe",
                )
                plugin._payload_store.write(
                    job_id,
                    EncryptedJobPayload(
                        "读取并解释项目", scope, "claude", "project", "replay_safe"
                    ),
                )
                plugin._job_store.recover_interrupted()

                async def execute_with_file(
                    this, current_job_id, current_job_dir, task, backend, high_risk_approved
                ):
                    report = current_job_dir / "outputs" / "report.txt"
                    report.write_text("safe report", encoding="utf-8")
                    this.executed.append(current_job_id)
                    return "执行结束", [Deliverable(report, "file")], "completed", 0, ""

                plugin._execute = types.MethodType(execute_with_file, plugin)
                await plugin._recover_jobs()

                self.assertEqual(len(bot.actions), 1)
                self.assertEqual(bot.actions[0][0], "upload_group_file")
                self.assertEqual(plugin._job_store.get(job_id)["state"], "completed")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
