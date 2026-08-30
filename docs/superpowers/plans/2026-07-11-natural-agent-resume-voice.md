# 小柠自然语言 Agent、断线恢复与本地语音 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 QQ `<configured-qq-id>` 用自然语言直接执行本机 Agent 任务，安全恢复可重放任务，并在明确要求时返回全本地生成的 QQ 语音。

**Architecture:** 保留 `claude_code_agent` 为唯一执行入口，自然语言只转换为统一的 Agent 请求；DPAPI 密文保存最小恢复载荷，SQLite 只保存元数据；语音输入继续由 Gemini Flash 理解，输出通过本机环回 TTS 服务生成并由 QQ `Record` 发送。

**Tech Stack:** Python 3.12、AstrBot Star API、SQLite、Windows DPAPI/ACL、asyncio、OneBot/NapCat、FFmpeg、GPT-SoVITS-CPUFast、MeloTTS、unittest。

## Global Constraints

- 只有 QQ `<configured-qq-id>` 可执行、确认、查询和取消本机任务；群聊必须真实 @ 当前机器人。
- 自然语言入口必须复用现有鉴权、风险判定、一次性审批、工作目录、审计、隐私脱敏和交付过滤。
- 不修改 Claude Code、Codex、WorkBuddy 或 cc-connect 的全局配置。
- 可安全重放任务才允许自动恢复；高风险或无法证明幂等的任务进入 `recovery_blocked`。
- DPAPI 密文目录仅当前 Windows 用户、SYSTEM、Administrators 可访问；SQLite 和日志不得保存任务正文、绝对路径、聊天目标或模型原始输出。
- 默认文字使用 DeepSeek；语音输入使用 Gemini Flash；只有明确要求语音才生成语音输出。
- 不向 Edge TTS 或其他新增云端 TTS 发送文本，不克隆未授权真人。
- 当前目录不是 Git 仓库；每个任务用 `progress.md` 检查点替代 commit，不初始化 Git。

## File Map

- Create `astrbot/data/plugins/claude_code_agent/natural_router.py`: 纯函数自然语言意图识别和群聊真实 @ 提取。
- Create `astrbot/data/plugins/claude_code_agent/recovery_policy.py`: 风险之外的可重放等级和恢复决策。
- Create `astrbot/data/plugins/claude_code_agent/encrypted_payload_store.py`: DPAPI 密文载荷、原子写入、清理和 ACL。
- Modify `astrbot/data/plugins/claude_code_agent/job_store.py`: 状态迁移、阶段、可恢复记录和交付摘要元数据。
- Modify `astrbot/data/plugins/claude_code_agent/agent_core.py`: 最新审批消费、恢复提示和交付摘要工具。
- Modify `astrbot/data/plugins/claude_code_agent/main.py`: 自然语言统一入口、队列/恢复、阶段更新和主动交付。
- Modify `astrbot/data/plugins/claude_code_agent/_conf_schema.json`: 自然语言、恢复和进度配置。
- Create `astrbot/data/plugins/voice_model_router/voice_reply_core.py`: 明确语音输出意图、朗读脱敏和分段。
- Create `astrbot/data/plugins/voice_model_router/local_tts_client.py`: 环回 TTS 客户端、超时和引擎降级。
- Modify `astrbot/data/plugins/voice_model_router/main.py`: 输出装饰器、QQ `Record` 发送和文字回退。
- Create `services/local_tts/server.py`: 本地 TTS HTTP 服务，MeloTTS 默认、GPT-SoVITS 可选。
- Create `services/local_tts/requirements-melo.txt`: 隔离环境依赖。
- Create `services/local_tts/start_local_tts.ps1`: 仅环回、隐藏窗口的启动脚本。
- Modify `astrbot/data/plugins/claude_code_agent/README.md`: 自然语言、恢复和隐私说明。
- Modify `astrbot/data/plugins/voice_model_router/metadata.yaml`: 输出语音能力说明和版本。
- Test `tests/test_natural_agent_router.py`, `tests/test_agent_recovery_policy.py`, `tests/test_encrypted_payload_store.py`, `tests/test_agent_job_store.py`, `tests/test_agent_core.py`, `tests/test_agent_integration.py`, `tests/test_voice_reply_core.py`, `tests/test_local_tts_client.py`, `tests/test_voice_router.py`.

---

### Task 1: 自然语言意图路由

**Files:**
- Create: `astrbot/data/plugins/claude_code_agent/natural_router.py`
- Create: `tests/test_natural_agent_router.py`
- Modify: `astrbot/data/plugins/claude_code_agent/agent_core.py`
- Modify: `tests/test_agent_core.py`

**Interfaces:**
- Produces: `NaturalAgentIntent(action: str, task: str = "", backend: str = "")`。
- Produces: `extract_natural_agent_text(text, components, self_id, group_id) -> str`。
- Produces: `route_natural_agent(text: str) -> NaturalAgentIntent | None`。
- Produces: `ApprovalRegistry.consume_latest(owner_id, scope, now=None) -> PendingApproval | None`。

- [x] **Step 1: 写自然语言路由失败测试**

```python
def test_explicit_owner_task_routes_without_slash():
    intent = route_natural_agent("帮我用 Codex 检查项目并生成报告")
    assert intent == NaturalAgentIntent("run", "检查项目并生成报告", "codex")

def test_chat_and_ambiguous_question_do_not_execute():
    assert route_natural_agent("你觉得这个项目怎么样") is None
    assert route_natural_agent("能不能写代码") is None

def test_status_cancel_and_confirm_are_supported():
    assert route_natural_agent("任务进度怎么样").action == "status"
    assert route_natural_agent("取消刚才的任务").action == "cancel"
    assert route_natural_agent("确认执行").action == "confirm"
```

- [x] **Step 2: 运行失败测试**

Run: `python -m unittest discover -s tests -p "test_natural_agent_router.py" -v`

Expected: FAIL with `ModuleNotFoundError: natural_router`。

- [x] **Step 3: 实现保守路由和真实 @ 提取**

```python
@dataclass(frozen=True)
class NaturalAgentIntent:
    action: str
    task: str = ""
    backend: str = ""

def route_natural_agent(text: str) -> NaturalAgentIntent | None:
    normalized = str(text or "").strip()
    if normalized in {"任务进度怎么样", "看看任务进度", "任务状态"}:
        return NaturalAgentIntent("status")
    if normalized in {"取消刚才的任务", "取消任务", "停止任务"}:
        return NaturalAgentIntent("cancel")
    if normalized in {"确认执行", "我确认执行", "确认这个任务"}:
        return NaturalAgentIntent("confirm")
    match = re.match(r"^(?:小柠[，, ]*)?(?:帮我|请你|麻烦你)\s*(.+)$", normalized)
    if not match:
        return None
    task = match.group(1).strip()
    backend, task = extract_backend_prefix(task)
    return NaturalAgentIntent("run", validate_task(task), backend)
```

`extract_natural_agent_text` 复用 `extract_agent_command` 的组件扫描规则：私聊返回纯文本；群聊只有组件中存在指向 `self_id` 的真实 `At` 才返回文本。

- [x] **Step 4: 增加最近审批消费并验证作用域**

```python
def consume_latest(self, owner_id: str, scope: str, now: float | None = None):
    valid = [p for p in self._pending.values() if p.owner_id == owner_id and p.scope == scope]
    if not valid:
        return None
    newest = max(valid, key=lambda item: item.expires_at)
    return self.consume(newest.token, owner_id, scope, now=now)
```

- [x] **Step 5: 运行目标测试和现有核心测试**

Run: `python -m unittest discover -s tests -p "test_natural_agent_router.py" -v`，随后运行 `python -m unittest discover -s tests -p "test_agent_core.py" -v`

Expected: PASS。

- [x] **Step 6: 在 `progress.md` 记录 Task 1 检查点**

记录测试命令、通过数量和修改文件；不执行 Git 操作。

### Task 2: 恢复策略与任务状态机

**Files:**
- Create: `astrbot/data/plugins/claude_code_agent/recovery_policy.py`
- Create: `tests/test_agent_recovery_policy.py`
- Modify: `astrbot/data/plugins/claude_code_agent/job_store.py`
- Modify: `tests/test_agent_job_store.py`

**Interfaces:**
- Produces: `RecoveryAssessment(resumable: bool, reason: str)`。
- Produces: `assess_recovery(task: str, work_dir: Path, base_dir: Path) -> RecoveryAssessment`。
- Produces: `JobStore.transition(job_id, state, stage, error_code="", now=None)`。
- Produces: `JobStore.list_interrupted() -> list[dict]` and `JobStore.record_delivery(job_id, digest)`。

- [x] **Step 1: 写恢复矩阵和状态迁移失败测试**

```python
def test_only_replay_safe_tasks_resume():
    assert assess_recovery("读取项目并生成新报告", work, base).resumable
    assert not assess_recovery("删除旧文件", work, base).resumable
    assert not assess_recovery("登录网站并发送消息", work, base).resumable
    assert not assess_recovery("修改项目", outside, base).resumable

def test_illegal_state_transition_is_rejected():
    store.start("job", "owner", "scope", "task", "claude", "", state="queued")
    with self.assertRaises(ValueError):
        store.transition("job", "completed", "delivering")
```

- [x] **Step 2: 运行失败测试**

Run: 分别以 `python -m unittest discover -s tests -p "test_agent_recovery_policy.py" -v` 和 `python -m unittest discover -s tests -p "test_agent_job_store.py" -v` 运行。

Expected: FAIL because recovery module and transition API do not exist。

- [x] **Step 3: 实现确定性恢复策略**

```python
BLOCK_REPLAY = re.compile(
    r"删除|覆盖|安装|卸载|权限|注册表|系统服务|登录|支付|提交|发送给|上传到|"
    r"delete|overwrite|install|uninstall|permission|registry|login|pay|submit|upload",
    re.I,
)

def assess_recovery(task: str, work_dir: Path, base_dir: Path) -> RecoveryAssessment:
    if BLOCK_REPLAY.search(task):
        return RecoveryAssessment(False, "non_idempotent")
    try:
        work_dir.resolve().relative_to(base_dir.resolve())
    except ValueError:
        return RecoveryAssessment(False, "outside_recovery_root")
    return RecoveryAssessment(True, "replay_safe")
```

- [x] **Step 4: 迁移 SQLite schema 并实现状态约束**

新增 `stage TEXT NOT NULL DEFAULT ''`、`recovery TEXT NOT NULL DEFAULT 'blocked'`、`delivery_digest TEXT NOT NULL DEFAULT ''`。构造函数用 `PRAGMA table_info` 后逐列 `ALTER TABLE`，兼容现有数据库。允许迁移表限定为规格中的合法迁移，不允许直接从 `running` 跳到 `completed`。

- [x] **Step 5: 运行目标测试**

Run: 分别以 `python -m unittest discover -s tests -p "test_agent_recovery_policy.py" -v` 和 `python -m unittest discover -s tests -p "test_agent_job_store.py" -v` 运行。

Expected: PASS，且数据库原始字节不包含测试任务正文或路径。

- [x] **Step 6: 在 `progress.md` 记录 Task 2 检查点**

记录迁移字段、恢复矩阵和测试结果。

### Task 3: Windows DPAPI 加密载荷

**Files:**
- Create: `astrbot/data/plugins/claude_code_agent/encrypted_payload_store.py`
- Create: `tests/test_encrypted_payload_store.py`

**Interfaces:**
- Produces: `EncryptedJobPayload(task, scope, backend, work_dir_relative, recovery, delivery_cursor=())`。
- Produces: `EncryptedPayloadStore.write(job_id, payload)`, `.read(job_id)`, `.delete(job_id)`, `.exists(job_id)`。

- [x] **Step 1: 写密文往返、无明文、篡改和 ACL 失败测试**

```python
@unittest.skipUnless(os.name == "nt", "DPAPI is Windows-only")
def test_payload_is_dpapi_encrypted_and_tamper_fails():
    store.write("abc123", payload)
    raw = (root / "abc123.bin").read_bytes()
    self.assertNotIn(payload.task.encode("utf-8"), raw)
    self.assertEqual(store.read("abc123"), payload)
    (root / "abc123.bin").write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
    with self.assertRaises(PayloadIntegrityError):
        store.read("abc123")
```

- [x] **Step 2: 运行失败测试**

Run: `python -m unittest discover -s tests -p "test_encrypted_payload_store.py" -v`

Expected: FAIL with missing module。

- [x] **Step 3: 用 `CryptProtectData`/`CryptUnprotectData` 实现当前用户 DPAPI**

```python
def _protect(data: bytes, entropy: bytes) -> bytes:
    in_blob = _blob(data)
    entropy_blob = _blob(entropy)
    out_blob = DATA_BLOB()
    if not crypt32.CryptProtectData(byref(in_blob), None, byref(entropy_blob), None, None, 0, byref(out_blob)):
        raise OSError(ctypes.get_last_error(), "CryptProtectData failed")
    return _take_blob(out_blob)
```

使用固定格式头 `XNJ1`、JSON schema version `1` 和 `job_id` 派生的 additional entropy。写入采用同目录临时文件、`os.replace` 原子替换；读取失败统一抛 `PayloadIntegrityError`，异常文本不得包含载荷。

- [x] **Step 4: 收紧目录 ACL 并 fail closed**

创建目录后调用 `icacls.exe <dir> /inheritance:r /grant:r <user>:(OI)(CI)F SYSTEM:(OI)(CI)F Administrators:(OI)(CI)F`；返回码非 0 时构造函数失败，不允许任务启动。

- [x] **Step 5: 运行 DPAPI 与 ACL 测试**

Run: `python -m unittest discover -s tests -p "test_encrypted_payload_store.py" -v`

Expected: PASS；`icacls` 输出中没有继承的 `Authenticated Users` 或 `Users` 修改权限。

- [x] **Step 6: 在 `progress.md` 记录 Task 3 检查点**

只记录固定测试结果，不记录密文、任务或本机用户名。

### Task 4: 统一执行入口、队列和断线恢复

**Files:**
- Modify: `astrbot/data/plugins/claude_code_agent/main.py`
- Modify: `astrbot/data/plugins/claude_code_agent/agent_core.py`
- Modify: `astrbot/data/plugins/claude_code_agent/_conf_schema.json`
- Create: `tests/test_agent_integration.py`

**Interfaces:**
- Consumes: `NaturalAgentIntent`, `RecoveryAssessment`, `EncryptedPayloadStore`, expanded `JobStore`。
- Produces: all slash commands and natural-language requests enter `ClaudeCodeAgent._handle_agent_request(...)`。
- Produces: `_recover_jobs()` schedules only replay-safe interrupted jobs after startup。

- [x] **Step 1: 写自然语言真实执行、审批、恢复和去重集成失败测试**

```python
async def test_natural_run_calls_same_execute_path():
    plugin = build_plugin(fake_execute)
    replies = await collect(plugin.on_message(owner_private("帮我生成 report.txt")))
    self.assertEqual(fake_execute.tasks, ["生成 report.txt"])
    self.assertTrue(any("已启动" in text for text in replies))

async def test_high_risk_natural_confirm_is_one_time():
    await collect(plugin.on_message(owner_private("帮我删除旧目录")))
    self.assertEqual(fake_execute.tasks, [])
    await collect(plugin.on_message(owner_private("确认执行")))
    self.assertEqual(fake_execute.tasks, ["删除旧目录"])

async def test_restart_resumes_safe_job_but_blocks_delete():
    safe_id = seed_interrupted("读取项目并生成新报告", recovery="replay_safe")
    risky_id = seed_interrupted("删除旧目录", recovery="blocked")
    await plugin._recover_jobs()
    self.assertIn(safe_id, fake_execute.job_ids)
    self.assertNotIn(risky_id, fake_execute.job_ids)
```

- [x] **Step 2: 运行失败测试**

Run: `python -m unittest discover -s tests -p "test_agent_integration.py" -v`

Expected: FAIL because unified handler and recovery scheduler are absent。

- [x] **Step 3: 抽取统一请求处理并接入自然语言**

```python
async def on_message(self, ctx):
    request = self._request_from_slash(ctx)
    if request is None and self._is_owner(ctx):
        request = self._request_from_natural(ctx)
    if request is None:
        return
    async for reply in self._handle_agent_request(ctx, request):
        yield reply
```

`confirm` 使用 `consume_latest`；`run` 始终先执行 `validate_task`、`assess_task_risk`、`assess_recovery`，再写 SQLite 和 DPAPI。任何审计或加密写入失败都不启动进程。

- [x] **Step 4: 实现有界队列和阶段消息**

配置新增 `max_queued_jobs`，默认 `3`、范围 `1..5`；单个活动任务保持现有限制。队列满时拒绝。阶段写入顺序为 `running -> verifying -> delivering -> completed`。超过 3 分钟且阶段变化时最多主动发送一次进度。

- [x] **Step 5: 实现启动恢复和交付去重**

`__init__` 只完成存储初始化；首次事件或 AstrBot 注册任务后调用 `_recover_jobs()`。恢复读取 DPAPI，重新验证工作目录和任务摘要；安全任务复用原 job 目录，执行提示增加“检查已有输出后继续”。`delivering` 恢复时先比较交付文件 SHA-256 和 `delivery_digest`，相同则不重复发送。

- [x] **Step 6: 终态清理密文并保持审计最小化**

完成、失败、取消、超时和 `recovery_blocked` 均调用 `payload_store.delete(job_id)`；删除失败时记录固定错误码，不在回复中暴露文件路径。

- [x] **Step 7: 运行 Agent 全部测试**

Run: `python -m unittest discover -s tests -p "test_agent*.py" -v`，再运行 `python -m unittest discover -s tests -p "test_natural_agent_router.py" -v`

Expected: PASS。

- [x] **Step 8: 在 `progress.md` 记录 Task 4 检查点**

记录自然语言、审批、恢复、去重和测试结果。

### Task 5: 语音输出意图与本地 TTS 客户端

**Files:**
- Create: `astrbot/data/plugins/voice_model_router/voice_reply_core.py`
- Create: `astrbot/data/plugins/voice_model_router/local_tts_client.py`
- Create: `tests/test_voice_reply_core.py`
- Create: `tests/test_local_tts_client.py`
- Modify: `tests/test_voice_router.py`

**Interfaces:**
- Produces: `wants_voice_reply(text: str) -> bool`。
- Produces: `prepare_spoken_chunks(text: str, max_chars=600, max_chunks=3) -> list[str]`。
- Produces: `async LocalTTSClient.synthesize(text: str) -> Path | None` with primary/fallback behavior。

- [x] **Step 1: 写语音意图、脱敏分段和降级失败测试**

```python
def test_voice_requires_explicit_request():
    assert wants_voice_reply("请发语音告诉我")
    assert not wants_voice_reply("我发了一条语音")

def test_spoken_chunks_hide_paths_and_secrets():
    chunks = prepare_spoken_chunks(r"完成了，文件在 D:\private\a.txt，token=abcdef123456")
    spoken = "".join(chunks)
    assert "D:\\" not in spoken
    assert "abcdef" not in spoken
    assert len(chunks) <= 3

async def test_primary_timeout_uses_fallback():
    client = LocalTTSClient(primary=failing_transport, fallback=ok_transport)
    path = await client.synthesize("你好")
    assert path.name.endswith(".wav")
```

- [x] **Step 2: 运行失败测试**

Run: 分别使用 `unittest discover` 运行 `test_voice_reply_core.py` 和 `test_local_tts_client.py`。

Expected: FAIL with missing modules。

- [x] **Step 3: 实现显式意图和最多三段朗读**

只匹配“发语音、语音说、用语音回答、语音回复”；先调用现有 `redact_sensitive_text`，再按句号、问号、感叹号切分，累计不超过 600 字和 3 段。Agent 结果包含代码块时只保留代码块外摘要。

- [x] **Step 4: 实现环回客户端和路径验证**

客户端只允许 `http://127.0.0.1:<port>`，请求体为 `{"text": ..., "voice": "xiaoning"}`，使用内存令牌 header；响应路径必须位于配置的私有音频目录。主引擎 12 秒超时后调用 fallback；双失败返回 `None`。

- [x] **Step 5: 运行目标测试**

Run: `python -m unittest discover -s tests -p "test_voice*.py" -v`，随后运行 `python -m unittest discover -s tests -p "test_local_tts_client.py" -v`

Expected: PASS。

- [x] **Step 6: 在 `progress.md` 记录 Task 5 检查点**

记录意图、脱敏、分段和降级测试结果。

### Task 6: 本地 TTS 服务与 QQ `Record` 闭环

**Files:**
- Create: `services/local_tts/server.py`
- Create: `services/local_tts/requirements-melo.txt`
- Create: `services/local_tts/start_local_tts.ps1`
- Modify: `astrbot/data/plugins/voice_model_router/main.py`
- Modify: `astrbot/data/plugins/voice_model_router/metadata.yaml`
- Create: `tests/test_local_tts_service.py`

**Interfaces:**
- Produces: `POST /synthesize` on `127.0.0.1` returning a private WAV path。
- Consumes: `LocalTTSClient` and `Record.fromFileSystem(path)`。

- [x] **Step 1: 写服务鉴权、环回和临时文件清理失败测试**

```python
def test_service_rejects_missing_token():
    response = app_client.post("/synthesize", json={"text": "你好"})
    assert response.status_code == 401

def test_service_rejects_non_loopback_bind_config():
    with self.assertRaises(ValueError):
        validate_bind_host("0.0.0.0")
```

- [x] **Step 2: 运行失败测试**

Run: `python -m unittest discover -s tests -p "test_local_tts_service.py" -v`

Expected: FAIL because service module is absent。

- [x] **Step 3: 实现最小本地服务**

`server.py` 延迟加载 MeloTTS，输入上限 600 字，输出只写私有目录，文件名使用随机 UUID。服务不记录正文；日志只记录引擎、耗时和固定错误码。GPT-SoVITS endpoint 仅在授权参考声线存在时启用，否则直接走 MeloTTS。

- [x] **Step 4: 创建隔离环境启动脚本**

```powershell
$venv = Join-Path $PSScriptRoot '.venv'
if (-not (Test-Path $venv)) { python -m venv $venv }
& "$venv\Scripts\python.exe" -m pip install -r "$PSScriptRoot\requirements-melo.txt"
& "$venv\Scripts\python.exe" "$PSScriptRoot\server.py" --host 127.0.0.1 --port 8766
```

实际常驻启动使用 `Start-Process -WindowStyle Hidden`；令牌通过当前进程环境传入，不写入仓库。

- [x] **Step 5: 接入 AstrBot 输出装饰器**

在 `voice_model_router` 保存本次输入是否明确要求语音；在最终结果装饰阶段提取脱敏文本、调用本地客户端并用 `Record(file=audio_path)` 替换或附加语音。生成失败保持原文字链不变。

- [x] **Step 6: 运行服务与路由测试**

Run: `python -m unittest discover -s tests -p "test_*tts*.py" -v`，随后运行 `python -m unittest discover -s tests -p "test_voice*.py" -v`

Expected: PASS。

- [x] **Step 7: 在 `progress.md` 记录 Task 6 检查点**

记录服务绑定、鉴权、QQ Record 组件和回退测试结果。

### Task 7: 文档、全量回归与运行态部署

**Files:**
- Modify: `astrbot/data/plugins/claude_code_agent/README.md`
- Modify: `astrbot/data/plugins/voice_model_router/metadata.yaml`
- Modify: `task_plan.md`
- Modify: `progress.md`
- Modify: `findings.md`

**Interfaces:**
- Consumes all previous tasks。
- Produces verified running AstrBot/OneBot/local TTS state。

- [x] **Step 1: 更新用户可见说明**

README 明确自然语言示例、低风险自动恢复、高风险重新确认、默认文字/显式语音、DPAPI 残余风险和不暴露本机路径。不得写入真实群号、密钥、绝对隐私路径或参考声线文件名。

- [x] **Step 2: 运行全部测试和语法编译**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`

Expected: 现有 49 项及全部新增测试 PASS。

Run: `python -m py_compile astrbot/data/plugins/claude_code_agent/*.py astrbot/data/plugins/voice_model_router/*.py services/local_tts/server.py`

Expected: exit code 0。

- [x] **Step 3: 执行隐私扫描**

Run: `rg -n -g "*.py" -g "*.json" -g "*.md" "<configured-qq-id>|D:\\\\|C:\\\\Users|token=|Authorization: Bearer" astrbot/data/plugins/claude_code_agent astrbot/data/plugins/voice_model_router services/local_tts`

Expected: 仅允许 owner 常量、测试夹具、配置默认工作根和明确脱敏测试；运行日志代码不得拼接任务正文、路径或令牌。

- [x] **Step 4: 启动本地 TTS 并做脱敏语音 smoke test**

使用固定无隐私句子“小柠语音测试完成”调用环回接口，确认 WAV 存在、可由 FFmpeg 读取，然后删除测试音频。未安装或授权 GPT-SoVITS 声线时验证 MeloTTS 自动降级，不下载来源不明的声线。

- [x] **Step 5: 重启 AstrBot 并定向验证端口/连接**

停止旧 AstrBot 进程树后，用现有启动方式重启；确认插件加载、OneBot WebSocket 重连，6185/6199 仅绑定本机，8192 不监听，本地 TTS 仅绑定 `127.0.0.1:8766`。

- [ ] **Step 6: 实际 QQ 低风险验收**

由 owner 在私聊或群聊真实 @ 发送无敏感任务“帮我生成一个只含 hello 的 txt 并发回来”；确认收到开始消息、真实文件和经过验证的完成摘要。用模拟状态验证恢复，不使用真实删除、外发、禁言或未授权真人声线。

- [x] **Step 7: 完成文件化检查点**

将通过数量、进程 PID、端口和残余风险写入 `progress.md`；把 `task_plan.md` 新阶段全部标记 complete。当前目录不是 Git 仓库，不执行 commit/branch/PR。

## Plan Self-Review

- Spec coverage: 自然语言、审批、状态、DPAPI、恢复、幂等交付、语音输入、显式输出、本地双引擎、错误处理、测试、部署均有对应任务。
- Placeholder scan: 所有实现步骤均给出明确文件、接口、测试命令和预期结果；GPT-SoVITS 仅在授权声线存在时启用的行为已经明确。
- Interface consistency: `NaturalAgentIntent`、`RecoveryAssessment`、`EncryptedPayloadStore`、`JobStore.transition`、`LocalTTSClient.synthesize` 在生产者任务中定义，在后续任务中按相同名称消费。
