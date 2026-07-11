import asyncio
import hashlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

os.environ["ASTRBOT_ROOT"] = r"D:\Claudecoda学习\qqbot\astrbot"

from astrbot.api.message_components import At, File, Plain

PLUGINS_DIR = Path(r"D:\Claudecoda学习\qqbot\astrbot\data\plugins")
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
    def __init__(self, text, *, sender="1211000567", group_id="", at_self=False):
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

    def get_message_text(self):
        return self._text

    def get_sender_id(self):
        return self._sender

    def get_group_id(self):
        return self._group_id

    def reply(self, component):
        return component

    def chain_result(self, chain):
        return chain[0] if len(chain) == 1 else chain


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
    def __init__(self, *, fail_upload=False):
        self.actions = []
        self.fail_upload = fail_upload

    async def call_action(self, action, **kwargs):
        self.actions.append((action, kwargs))
        if self.fail_upload:
            raise RuntimeError("upload unavailable")


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

                await _collect(plugin.on_message(FakeEvent("帮我分析这份报告")))

                self.assertEqual(len(plugin.executed), 1)
                self.assertEqual(plugin.executed[0][2], "codex")

        asyncio.run(scenario())

    def test_pro_membership_alone_does_not_grant_trusted_host_execution(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))
                plugin._access_policy = AccessPolicy(["1211000567", "2000000000"])

                replies = await _collect(
                    plugin.on_message(
                        FakeEvent("帮我生成一份 Word", sender="2000000000")
                    )
                )

                self.assertEqual(plugin.executed, [])
                self.assertTrue(any("Trusted Pro" in text for text in _plain_texts(replies)))

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

    def test_later_high_risk_step_pauses_after_safe_step_and_resumes_at_cursor(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))

                first = await _collect(
                    plugin.on_message(FakeEvent("帮我读取项目，然后发送报告"))
                )

                self.assertEqual([item[1] for item in plugin.executed], ["读取项目"])
                self.assertTrue(any("确认" in text for text in _plain_texts(first)))
                job_id = plugin.executed[0][0]
                self.assertEqual(plugin._job_store.get(job_id)["state"], "awaiting_approval")
                self.assertTrue(plugin._payload_store.exists(job_id))

                second = await _collect(plugin.on_message(FakeEvent("确认执行")))

                self.assertEqual(
                    [item[1] for item in plugin.executed], ["读取项目", "发送报告"]
                )
                self.assertEqual(plugin._job_store.get(job_id)["state"], "completed")
                self.assertFalse(plugin._payload_store.exists(job_id))
                self.assertTrue(any("执行结束" in text for text in _plain_texts(second)))

        asyncio.run(scenario())

    def test_unknown_step_stays_blocked_when_isolation_adapter_is_not_ready(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))

                first = await _collect(
                    plugin.on_message(FakeEvent("帮我处理一下这个项目"))
                )
                self.assertEqual(plugin.executed, [])
                self.assertTrue(any("确认" in text for text in _plain_texts(first)))

                second = await _collect(plugin.on_message(FakeEvent("确认执行")))

                self.assertEqual(plugin.executed, [])
                self.assertTrue(
                    any("隔离" in text for text in _plain_texts(second)),
                    _plain_texts(second),
                )

        asyncio.run(scenario())

    def test_high_risk_approval_is_bound_to_the_routed_backend(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))
                plugin.backend = "claude"

                await _collect(plugin.on_message(FakeEvent("帮我部署代码")))

                pending = next(iter(plugin._approvals._pending.values()))
                self.assertEqual(pending.backend, "codex")

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
                self.assertTrue(any("Pro" in text for text in _plain_texts(ordinary)))
                self.assertEqual(
                    await _collect(
                        plugin.on_message(FakeEvent("帮我生成报告", group_id="123", at_self=False))
                    ),
                    [],
                )
                self.assertEqual(plugin.executed, [])

        asyncio.run(scenario())

    def test_ordinary_natural_agent_request_gets_pro_boundary_without_execution(self):
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
                    any("Pro" in text for text in _plain_texts(replies)),
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
                    safe_id, "1211000567", "safe-scope", "读取项目并生成报告",
                    "claude", "", state="running", recovery="replay_safe"
                )
                plugin._job_store.start(
                    blocked_id, "1211000567", "blocked-scope", "删除旧目录",
                    "claude", "删除", state="running", recovery="blocked"
                )
                plugin._payload_store.write(
                    safe_id,
                    EncryptedJobPayload(
                        "读取项目并生成报告", "safe-scope", "claude", "project", "replay_safe"
                    ),
                )
                plugin._payload_store.write(
                    blocked_id,
                    EncryptedJobPayload(
                        "删除旧目录", "blocked-scope", "claude", ".", "blocked"
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
                    {"safe-scope", "blocked-scope"},
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
                    if task == "生成第一个报告":
                        first_started.set()
                        await release_first.wait()
                    return f"任务 {job_id} 执行结束（退出码 0）", [], "completed", 0, ""

                plugin._execute = types.MethodType(controlled_execute, plugin)
                first = asyncio.create_task(
                    _collect(plugin.on_message(FakeEvent("帮我生成第一个报告")))
                )
                await asyncio.wait_for(first_started.wait(), timeout=2)
                second = asyncio.create_task(
                    _collect(plugin.on_message(FakeEvent("帮我生成第二个报告")))
                )
                await asyncio.sleep(0.05)
                self.assertFalse(second.done())
                release_first.set()
                first_replies, second_replies = await asyncio.gather(first, second)

                self.assertEqual(
                    [item[1] for item in plugin.executed],
                    ["生成第一个报告", "生成第二个报告"],
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
                    "queued-scope",
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
                        "queued-scope",
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

    def test_planned_read_only_recovery_resumes_only_from_step_cursor(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                plugin = self._plugin(Path(tmp))
                job_id = "d1e2f3a4b5c6"
                create_job_dir(plugin.workspace, job_id)
                task = "读取项目，然后总结结果"
                plan = plan_task(TaskRequest(job_id, task, "claude"))
                plugin._job_store.start(
                    job_id,
                    "1211000567",
                    "aiocqhttp:FriendMessage:1211000567",
                    task,
                    "claude",
                    "planned",
                    state="executing",
                    recovery="replay_safe",
                    step_index=1,
                    step_count=2,
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
                        step_cursor=1,
                    ),
                )
                plugin._job_store.recover_interrupted()

                await plugin._recover_jobs()

                self.assertEqual([item[1] for item in plugin.executed], ["总结结果"])
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

                plugin._job_store.start(
                    job_id,
                    "1211000567",
                    "delivery-scope",
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
                        "delivery-scope",
                        "claude",
                        "project",
                        "replay_safe",
                        (first_digest,),
                    ),
                )
                plugin._job_store.recover_interrupted()

                await plugin._recover_jobs()

                self.assertEqual(plugin.executed, [])
                sent_names = []
                for _scope, chain in plugin.context.sent:
                    for component in getattr(chain, "chain", []):
                        name = getattr(component, "name", "")
                        if name:
                            sent_names.append(name)
                self.assertNotIn("first.txt", sent_names)
                self.assertEqual(sent_names.count("second.txt"), 1)
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

    def test_group_file_delivery_failure_never_reports_completed(self):
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

                self.assertFalse(any(text.startswith("已完成") for text in texts), texts)
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
                plugin.context.fail_next_file = True
                await plugin._recover_jobs()

                first_record = plugin._job_store.get(job_id)
                self.assertRegex(first_record["delivery_digest"], r"^[0-9a-f]{64}$")
                self.assertEqual(plugin.executed, [job_id])
                self.assertTrue(plugin._payload_store.exists(job_id))

                plugin._job_store.recover_interrupted()

                async def must_not_execute(*_args, **_kwargs):
                    raise AssertionError("delivery restart must not execute the agent")

                plugin._execute = must_not_execute
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
