# 小柠任务执行内核 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Pro 自然语言任务升级为可审计的计划、逐步策略检查、后端路由、实际执行、独立验证和幂等交付闭环。

**Architecture:** 新增纯函数计划器、步骤策略、后端路由、验证器和编排器；`main.py` 只负责 AstrBot 事件适配与发送。现有 AccessPolicy、ApprovalRegistry、JobStore、DPAPI、DLP、有限进程输出和恢复交付继续作为唯一安全底座。

**Tech Stack:** Python 3.12、AstrBot 4.26.5、SQLite、Windows DPAPI/ACL、OneBot v11、Claude Code/Codex/WorkBuddy CLI、`unittest`。

## Global Constraints

- Pro allowlist 当前仅 QQ `1211000567`，聊天、人格和记忆不能提升权限。
- 群聊必须真实 @；普通用户不能进入计划、执行、状态、审批、恢复和文件入口。
- 工作目录内创建新文件和修改任务产物可自动执行；删除、覆盖原文件、外发、安装、部署、登录、系统/权限/凭据和未知动作必须确认。
- 每个步骤重新分类；原始请求通过不代表后续步骤自动通过。
- 只有严格只读步骤可自动恢复；已提交交付摘要的任务只补发。
- 不记录任务原文、结果、绝对路径、确认码、异常正文或敏感内容。
- 项目不是 Git 仓库；不执行 commit、branch 或 PR，以测试证据和计划勾选记录变更。

---

### Task 1: 不可变任务计划模型与保守计划器

**Files:**
- Create: `astrbot/data/plugins/claude_code_agent/task_planner.py`
- Test: `tests/test_task_planner.py`

**Interfaces:**
- Consumes: `action_policy.classify_action(task)`。
- Produces: `TaskRequest`、`TaskStep`、`ExecutionPlan`、`plan_task(request) -> ExecutionPlan`。

- [x] **Step 1: 写计划器 RED 测试**

```python
def test_plan_is_bounded_and_preserves_order():
    request = TaskRequest("job123", "生成报告，然后运行测试，再总结结果", "codex")
    plan = plan_task(request)
    assert 1 <= len(plan.steps) <= 8
    assert [step.index for step in plan.steps] == list(range(len(plan.steps)))
    assert all(step.task_id == "job123" for step in plan.steps)

def test_simple_task_stays_one_step_and_unknown_is_not_relabelled_safe():
    plan = plan_task(TaskRequest("job124", "处理一下这个项目", "claude"))
    assert len(plan.steps) == 1
    assert plan.steps[0].action_class is ActionClass.UNKNOWN
```

- [x] **Step 2: 运行 RED**

Run: `python -m unittest discover -s tests -p 'test_task_planner.py' -v`

Expected: FAIL，`task_planner` 尚不存在。

- [x] **Step 3: 实现最小计划器**

```python
@dataclass(frozen=True)
class TaskRequest:
    task_id: str
    goal: str
    preferred_backend: str

@dataclass(frozen=True)
class TaskStep:
    task_id: str
    index: int
    instruction: str
    action_class: ActionClass
    expected_artifact: bool

@dataclass(frozen=True)
class ExecutionPlan:
    task_id: str
    steps: tuple[TaskStep, ...]

_SEPARATOR = re.compile(r"(?:，?然后|，?再|；|;|\n)+")

def plan_task(request: TaskRequest) -> ExecutionPlan:
    goal = validate_task(request.goal)
    clauses = [item.strip(" ，") for item in _SEPARATOR.split(goal) if item.strip(" ，")]
    clauses = (clauses or [goal])[:8]
    steps = tuple(
        TaskStep(
            request.task_id,
            index,
            clause,
            classify_action(clause).action_class,
            bool(re.search(r"生成|创建|导出|报告|文件|图片|表格|压缩包", clause)),
        )
        for index, clause in enumerate(clauses)
    )
    return ExecutionPlan(request.task_id, steps)
```

- [x] **Step 4: 运行 GREEN 与动作分类回归**

Run: `python -m unittest discover -s tests -p 'test_task_planner.py' -v`

Run: `python -m unittest discover -s tests -p 'test_agent_action_policy.py' -v`

Expected: 全部 PASS。

---

### Task 2: 逐步骤策略与审批绑定

**Files:**
- Create: `astrbot/data/plugins/claude_code_agent/step_policy.py`
- Modify: `astrbot/data/plugins/claude_code_agent/agent_core.py`
- Test: `tests/test_step_policy.py`
- Modify: `tests/test_agent_core.py`

**Interfaces:**
- Consumes: `TaskStep`、`ActionClass`、工作目录、任务输出目录。
- Produces: `StepDecision`、`assess_step(step, work_dir, output_dir)`；扩展 `PendingApproval.task_id`、`step_digest`。

- [x] **Step 1: 写越权步骤与审批作用域 RED 测试**

```python
def test_high_impact_step_needs_approval_even_when_goal_looked_safe():
    step = TaskStep("job123", 1, "把报告发送给好友", ActionClass.HIGH_IMPACT, False)
    decision = assess_step(step, WORK_DIR, OUTPUT_DIR)
    assert decision.requires_approval
    assert decision.code == "high_impact"

def test_approval_cannot_cross_task_or_step():
    pending = registry.issue("1211000567", "private", "job123", "digest-a", "发送报告", "claude", WORK_DIR)
    assert registry.consume(pending.token, "1211000567", "private", "job124", "digest-a") is None
    assert registry.consume(pending.token, "1211000567", "private", "job123", "digest-b") is None
```

- [x] **Step 2: 运行 RED**

Run: `python -m unittest discover -s tests -p 'test_step_policy.py' -v`

Expected: FAIL，步骤策略和审批字段尚不存在。

- [x] **Step 3: 实现 fail-closed 步骤决策**

```python
@dataclass(frozen=True)
class StepDecision:
    allowed: bool
    requires_approval: bool
    code: str

def assess_step(step: TaskStep, work_dir: Path, output_dir: Path) -> StepDecision:
    if step.action_class in {ActionClass.HIGH_IMPACT, ActionClass.UNKNOWN}:
        return StepDecision(False, True, step.action_class.value)
    if not directory_within(work_dir, DEFAULT_WORK_DIR):
        return StepDecision(False, True, "outside_work_root")
    if not directory_within(output_dir, DEFAULT_WORKSPACE):
        return StepDecision(False, False, "invalid_output_root")
    return StepDecision(True, False, "allowed")
```

审批摘要使用 `sha256(f"{task_id}:{step.index}:{step.instruction}")`；`issue` 与 `consume` 必须同时绑定 owner、scope、task ID、step digest、backend 和 work dir。

- [x] **Step 4: 运行策略、审批和普通/Pro 回归**

Run: `python -m unittest discover -s tests -p 'test_step_policy.py' -v`

Run: `python -m unittest discover -s tests -p 'test_agent_core.py' -v`

Run: `python -m unittest discover -s tests -p 'test_agent_access_policy.py' -v`

Expected: 全部 PASS。

---

### Task 3: 后端路由与有限切换

**Files:**
- Create: `astrbot/data/plugins/claude_code_agent/backend_router.py`
- Test: `tests/test_backend_router.py`

**Interfaces:**
- Consumes: `TaskStep`、用户偏好后端、可用后端集合、已尝试后端集合。
- Produces: `BackendRoute`、`route_backend(...) -> BackendRoute`、`can_retry(step, started_side_effect, attempts)`。

- [x] **Step 1: 写路由和重试 RED 测试**

```python
def test_code_routes_to_codex_then_claude():
    step = make_step("修复 Python 测试")
    first = route_backend(step, "claude", {"codex", "claude"}, set())
    second = route_backend(step, "claude", {"codex", "claude"}, {"codex"})
    assert first.backend == "codex"
    assert second.backend == "claude"

def test_side_effect_step_never_switches_after_start():
    assert not can_retry(make_high_impact_step(), started_side_effect=True, attempts=1)
```

- [x] **Step 2: 运行 RED**

Run: `python -m unittest discover -s tests -p 'test_backend_router.py' -v`

Expected: FAIL，路由模块尚不存在。

- [x] **Step 3: 实现确定性路由**

```python
CODE_HINT = re.compile(r"代码|项目|测试|构建|编译|修复|重构|python|typescript|next\.js", re.I)
DESKTOP_HINT = re.compile(r"桌面|窗口|点击|软件|浏览器界面", re.I)

def route_backend(step, preferred, available, attempted):
    order = ["workbuddy", "codex", "claude"] if DESKTOP_HINT.search(step.instruction) else (
        ["codex", "claude", "workbuddy"] if CODE_HINT.search(step.instruction)
        else [preferred, "claude", "codex", "workbuddy"]
    )
    backend = next((name for name in order if name in available and name not in attempted), None)
    return BackendRoute(backend, "no_backend" if backend is None else "selected")

def can_retry(step, started_side_effect, attempts):
    return attempts < 2 and not started_side_effect and step.action_class is ActionClass.READ_ONLY
```

- [x] **Step 4: 运行路由测试**

Run: `python -m unittest discover -s tests -p 'test_backend_router.py' -v`

Expected: PASS。

---

### Task 4: 独立验证器

**Files:**
- Create: `astrbot/data/plugins/claude_code_agent/task_verifier.py`
- Test: `tests/test_task_verifier.py`

**Interfaces:**
- Consumes: 步骤、退出码、交付物、工作目录、可选验证命令结果。
- Produces: `VerificationEvidence`、`select_verification_command(...)`、`verify_step(...)`。

- [ ] **Step 1: 写禁止伪完成 RED 测试**

```python
def test_zero_exit_without_required_artifact_is_not_complete():
    step = TaskStep("job", 0, "生成 report.txt", ActionClass.WORKSPACE_WRITE, True)
    evidence = verify_step(step, exit_code=0, deliverables=[], verification_exit=None)
    assert not evidence.verified
    assert evidence.code == "artifact_missing"

def test_failed_test_command_blocks_completion():
    evidence = verify_step(make_code_step(), 0, [], verification_exit=1)
    assert not evidence.verified
    assert evidence.code == "verification_failed"
```

- [ ] **Step 2: 运行 RED**

Run: `python -m unittest discover -s tests -p 'test_task_verifier.py' -v`

Expected: FAIL，验证器尚不存在。

- [ ] **Step 3: 实现验证规则与命令选择**

```python
def select_verification_command(work_dir: Path) -> list[str] | None:
    if (work_dir / "package.json").is_file():
        package = json.loads((work_dir / "package.json").read_text(encoding="utf-8"))
        if "test" in package.get("scripts", {}):
            return ["npm", "test"]
    if (work_dir / "tests").is_dir():
        return [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
    return None

def verify_step(step, exit_code, deliverables, verification_exit):
    if exit_code != 0:
        return VerificationEvidence(False, "execution_failed")
    if step.expected_artifact and not deliverables:
        return VerificationEvidence(False, "artifact_missing")
    if verification_exit not in {None, 0}:
        return VerificationEvidence(False, "verification_failed")
    return VerificationEvidence(True, "verified")
```

验证命令必须沿用 `capture_bounded_process`，使用只读/测试超时上限，输出不得直接写日志或聊天。

- [ ] **Step 4: 运行验证器和 DLP 回归**

Run: `python -m unittest discover -s tests -p 'test_task_verifier.py' -v`

Run: `python -m unittest discover -s tests -p 'test_delivery_dlp.py' -v`

Expected: 全部 PASS。

---

### Task 5: 执行编排器与状态机

**Files:**
- Create: `astrbot/data/plugins/claude_code_agent/task_orchestrator.py`
- Modify: `astrbot/data/plugins/claude_code_agent/job_store.py`
- Modify: `astrbot/data/plugins/claude_code_agent/encrypted_payload_store.py`
- Modify: `astrbot/data/plugins/claude_code_agent/main.py`
- Test: `tests/test_task_orchestrator.py`
- Modify: `tests/test_agent_job_store.py`
- Modify: `tests/test_encrypted_payload_store.py`

**Interfaces:**
- Consumes: Tasks 1-4 的计划、步骤策略、路由和验证接口。
- Produces: `TaskEvent(kind, task_id, step_index, code)`、`TaskOutcome`、`TaskOrchestrator.run(plan)`。

- [ ] **Step 1: 写完整闭环 RED 测试**

```python
async def test_orchestrator_executes_verifies_and_delivers_in_order():
    outcome = await orchestrator.run(plan_with_two_safe_steps())
    assert [event.kind for event in outcome.events] == [
        "started", "step_completed", "step_completed", "completed"
    ]
    assert outcome.verified

async def test_unapproved_step_stops_before_executor():
    outcome = await orchestrator.run(plan_with_high_impact_second_step())
    assert outcome.events[-1].kind == "approval_required"
    assert executor.calls == [0]
```

- [ ] **Step 2: 运行 RED**

Run: `python -m unittest discover -s tests -p 'test_task_orchestrator.py' -v`

Expected: FAIL，编排器尚不存在。

- [ ] **Step 3: 扩展加密载荷与账本**

`EncryptedJobPayload` 新增 `plan: tuple[dict, ...]`、`step_cursor: int = 0`；验证时限制最多 8 步、每步正文最大 1000 字。账本新增 `step_index INTEGER NOT NULL DEFAULT 0`、`step_count INTEGER NOT NULL DEFAULT 1`，状态加入 `planned` 与 `executing`，迁移路径保持旧库可读。

```python
_TRANSITIONS["planned"] = {"awaiting_approval", "queued", "executing", "cancelled"}
_TRANSITIONS["queued"] |= {"executing"}
_TRANSITIONS["executing"] = {"awaiting_approval", "verifying", "failed", "cancelled", "timeout", "interrupted"}
```

- [ ] **Step 4: 实现依赖注入编排器并接入 `main.py`**

```python
class TaskOrchestrator:
    def __init__(self, policy, router, executor, verifier, approval_check):
        self.policy = policy
        self.router = router
        self.executor = executor
        self.verifier = verifier
        self.approval_check = approval_check

    async def run(self, plan):
        events = [TaskEvent("started", plan.task_id, 0, "started")]
        for step in plan.steps:
            decision = self.policy(step)
            if decision.requires_approval and not self.approval_check(step):
                events.append(TaskEvent("approval_required", plan.task_id, step.index, decision.code))
                return TaskOutcome(False, tuple(events), "awaiting_approval")
            execution = await self.executor(step, self.router(step))
            evidence = await self.verifier(step, execution)
            if not evidence.verified:
                events.append(TaskEvent("failed", plan.task_id, step.index, evidence.code))
                return TaskOutcome(False, tuple(events), "failed")
            events.append(TaskEvent("step_completed", plan.task_id, step.index, "verified"))
        events.append(TaskEvent("completed", plan.task_id, len(plan.steps) - 1, "completed"))
        return TaskOutcome(True, tuple(events), "completed")
```

`on_message` 仅构造 `TaskRequest`、保存加密计划、调用编排器并把事件转换为安全回复；现有 `_execute`、DLP 与交付方法通过回调复用。

- [ ] **Step 5: 运行编排器、账本、载荷和现有集成回归**

Run: `python -m unittest discover -s tests -p 'test_task_orchestrator.py' -v`

Run: `python -m unittest discover -s tests -p 'test_agent_job_store.py' -v`

Run: `python -m unittest discover -s tests -p 'test_encrypted_payload_store.py' -v`

Run: `python -m unittest discover -s tests -p 'test_agent_integration.py' -v`

Expected: 全部 PASS。

---

### Task 6: 恢复、隔离决策与幂等交付

**Files:**
- Create: `astrbot/data/plugins/claude_code_agent/isolation_policy.py`
- Modify: `astrbot/data/plugins/claude_code_agent/recovery_policy.py`
- Modify: `astrbot/data/plugins/claude_code_agent/main.py`
- Test: `tests/test_isolation_policy.py`
- Modify: `tests/test_agent_recovery_policy.py`
- Modify: `tests/test_agent_integration.py`

**Interfaces:**
- Produces: `IsolationDecision(mode, code)`、`choose_isolation(step, sandbox_available)`。
- Changes: 恢复从任务级改为当前步骤级；未知动作在隔离器不可用时不静默使用主机权限。

- [ ] **Step 1: 写隔离与逐步恢复 RED 测试**

```python
def test_unknown_step_requires_a_validated_isolation_adapter():
    assert choose_isolation(unknown_step(), sandbox_ready=False).code == "isolation_unavailable"
    assert choose_isolation(unknown_step(), sandbox_ready=True).mode == "windows_sandbox"

def test_only_read_only_current_step_is_replay_safe():
    assert assess_step_recovery(read_step()).resumable
    assert not assess_step_recovery(write_step()).resumable
```

集成测试必须模拟：步骤 0 已验证、步骤 1 未执行时重启；只读步骤 1 可继续，写入步骤 1 进入 `recovery_blocked`。已有 `delivery_digest` 时 executor 调用数必须为 0。

- [ ] **Step 2: 运行 RED**

Run: `python -m unittest discover -s tests -p 'test_isolation_policy.py' -v`

Run: `python -m unittest discover -s tests -p 'test_agent_integration.py' -v`

Expected: FAIL，隔离决策和步骤游标恢复尚未实现。

- [ ] **Step 3: 实现 fail-closed 隔离决策**

```python
def choose_isolation(step, sandbox_ready):
    if step.action_class is not ActionClass.UNKNOWN:
        return IsolationDecision("host", "known_action")
    if sandbox_ready:
        return IsolationDecision("windows_sandbox", "isolated")
    return IsolationDecision("blocked", "isolation_unavailable")
```

`sandbox_ready` 只有在独立隔离执行适配器完成启动、目录映射、Agent 可用性和结果回收自检后才能为真；仅存在 `WindowsSandbox.exe` 不算可用。本轮未配置该适配器时返回确认后的固定阻塞提示，不伪造隔离执行，也不静默降级。

- [ ] **Step 4: 接入步骤游标恢复与原交付游标**

恢复时重新计算当前步骤分类；只有 `READ_ONLY` 且工作目录仍在恢复根内时调用 executor。若 `delivery_digest` 已存在，直接进入 `delivering` 并复用 `_deliver_recovered_files`。

- [ ] **Step 5: 运行恢复和交付回归**

Run: `python -m unittest discover -s tests -p 'test_isolation_policy.py' -v`

Run: `python -m unittest discover -s tests -p 'test_agent_recovery_policy.py' -v`

Run: `python -m unittest discover -s tests -p 'test_agent_integration.py' -v`

Expected: 全部 PASS。

---

### Task 7: 全量验收、文档和部署

**Files:**
- Modify: `astrbot/data/plugins/claude_code_agent/README.md`
- Modify: `progress.md`
- Modify: `findings.md`
- Modify: `task_plan.md`

- [ ] **Step 1: 更新能力说明**

README 明确计划上限 8 步、逐步审批、后端路由、独立验证、一次安全切换、未知动作隔离阻塞和恢复规则；不写本机路径、密钥或真实任务内容。

- [ ] **Step 2: 运行全量回归与编译**

Run: `python -m unittest discover -s tests -p 'test_*.py' -v`

Run: `python -m compileall -q astrbot/data/plugins/claude_code_agent astrbot/data/plugins/voice_model_router services/local_tts/server.py`

Expected: 全部 PASS，编译 exit 0。

- [ ] **Step 3: 运行隐私与运行态检查**

检查目标 JSON、动态日志拼接、ACL、6185/6199/8766 环回监听、8192 关闭、OneBot 连接和启动错误。运行 `services/harden_runtime_privacy.ps1` 后 ACL 不安全项必须为 0。

- [ ] **Step 4: 重启 AstrBot 并做真实 QQ 验收**

Pro 发送“帮我生成一个只含 hello 的 txt”；必须出现开始、验证完成和文件。Pro 发送“帮我覆盖旧配置”只能进入确认。普通账号同类请求不得产生任务。群聊未真实 @ 保持静默。

## Plan Self-Review

- Spec coverage: Intake、Planner、Policy、Router、Executor、Verifier、Deliverer、状态、恢复、错误与真实 QQ 验收均有对应 Task。
- Type consistency: `TaskRequest -> ExecutionPlan -> TaskStep -> StepDecision/BackendRoute -> VerificationEvidence -> TaskOutcome` 单向流动；后续 Task 只使用前序定义接口。
- Scope: Windows Sandbox 本轮实现适配器就绪门禁和 fail-closed 决策；没有通过完整自检的沙箱内 Agent 环境一律视为不可用。

## Execution Status

- Tasks 1-6：已按 RED -> GREEN 完成。
- Task 7 Steps 1-3：文档、156 项全量回归、编译、隐私检查、重启和运行态检查已完成。
- Task 7 Step 4：等待 Pro QQ 真实文件回传和高风险确认验收；通过前不启用体验层。
