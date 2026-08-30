# 小柠体验层 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在任务执行内核验收后，增加简短自然回复、低噪声进度、安全分层记忆和授权本地语音升级，不新增工具执行入口。

**Architecture:** 体验层只消费 `TaskEvent` 与最终结果；回复压缩、进度节流、记忆策略和语音选择均为独立纯模块。所有本机工具调用仍只能经过任务执行内核。

**Tech Stack:** Python 3.12、AstrBot 4.26.5、SQLite/DPAPI、DeepSeek、Gemini Flash、本地 MeloTTS、可选授权 GPT-SoVITS、`unittest`。

## Global Constraints

- 必须在任务执行内核 Task 7 真实 QQ 验收通过后开始。
- 默认回复 1 至 4 句；只播报开始、确认、完成、失败，超过 90 秒可增加一次阶段更新。
- 记忆不能提升权限，不保存路径、凭据、聊天正文、文件内容、健康隐私或未授权第三方信息。
- 默认文字 DeepSeek；语音输入/明确语音输出使用 Gemini Flash；未配置授权声线时保留 MeloTTS。
- 体验层不得导入 subprocess、调用 Agent CLI 或直接发送本机文件。
- 项目不是 Git 仓库；不执行 commit、branch 或 PR。

---

### Task 1: 简短回复策略

**Files:**
- Create: `astrbot/data/plugins/claude_code_agent/response_style.py`
- Test: `tests/test_response_style.py`

**Interfaces:**
- Produces: `format_task_reply(kind, evidence, detail) -> str`、`compact_text(text, max_sentences=4, max_chars=500)`。

- [ ] **Step 1: 写 RED 测试**

```python
def test_completed_reply_leads_with_outcome_and_is_bounded():
    reply = format_task_reply("completed", "测试 12/12 通过", "很长详情。" * 100)
    assert reply.startswith("已完成")
    assert len(reply) <= 500
    assert reply.count("。") <= 4

def test_reply_redacts_paths_and_secrets_before_compaction():
    reply = compact_text(r"文件在 D:\private\a.txt，token=abcdef1234567890")
    assert "D:\\private" not in reply
    assert "abcdef1234567890" not in reply
```

- [ ] **Step 2: 运行 RED**

Run: `python -m unittest discover -s tests -p 'test_response_style.py' -v`

Expected: FAIL，模块尚不存在。

- [ ] **Step 3: 实现先脱敏、后截句的格式器**

```python
def compact_text(text, max_sentences=4, max_chars=500):
    safe = redact_sensitive_text(str(text or "").strip())
    sentences = [item for item in re.split(r"(?<=[。！？!?])", safe) if item.strip()]
    return "".join(sentences[:max_sentences])[:max_chars]

def format_task_reply(kind, evidence="", detail=""):
    prefix = {"started": "已开始。", "approval_required": "需要你确认。", "completed": "已完成。", "failed": "未完成。"}[kind]
    return compact_text(" ".join(part for part in (prefix, evidence, detail) if part))
```

- [ ] **Step 4: 运行 GREEN**

Run: `python -m unittest discover -s tests -p 'test_response_style.py' -v`

Expected: PASS。

---

### Task 2: 低噪声进度节流

**Files:**
- Create: `astrbot/data/plugins/claude_code_agent/progress_policy.py`
- Modify: `astrbot/data/plugins/claude_code_agent/main.py`
- Test: `tests/test_progress_policy.py`

**Interfaces:**
- Consumes: `TaskEvent`。
- Produces: `ProgressPolicy.should_emit(event, now) -> bool`。

- [ ] **Step 1: 写重复阶段和 90 秒 RED 测试**

```python
def test_duplicate_stage_is_suppressed():
    policy = ProgressPolicy()
    assert policy.should_emit(event("started", "job"), now=0)
    assert not policy.should_emit(event("started", "job"), now=10)

def test_long_task_allows_one_stage_update_after_90_seconds():
    policy = ProgressPolicy()
    policy.should_emit(event("started", "job"), now=0)
    assert not policy.should_emit(event("executing", "job"), now=89)
    assert policy.should_emit(event("executing", "job"), now=90)
    assert not policy.should_emit(event("executing", "job"), now=180)
```

- [ ] **Step 2: 运行 RED**

Run: `python -m unittest discover -s tests -p 'test_progress_policy.py' -v`

Expected: FAIL，节流模块尚不存在。

- [ ] **Step 3: 实现每任务内存态节流并接入事件适配**

```python
class ProgressPolicy:
    def __init__(self):
        self._state = {}

    def should_emit(self, event, now):
        state = self._state.setdefault(event.task_id, {"started": now, "kinds": set(), "stage_sent": False})
        if event.kind in {"started", "approval_required", "completed", "failed"}:
            if event.kind in state["kinds"]:
                return False
            state["kinds"].add(event.kind)
            return True
        if now - state["started"] >= 90 and not state["stage_sent"]:
            state["stage_sent"] = True
            return True
        return False
```

`main.py` 只将允许事件交给 `format_task_reply`；不发送步骤全文、命令、路径或异常正文。

- [ ] **Step 4: 运行进度与集成回归**

Run: `python -m unittest discover -s tests -p 'test_progress_policy.py' -v`

Run: `python -m unittest discover -s tests -p 'test_agent_integration.py' -v`

Expected: 全部 PASS。

---

### Task 3: 安全分层记忆

**Files:**
- Create: `astrbot/data/plugins/claude_code_agent/experience_memory.py`
- Test: `tests/test_experience_memory.py`

**Interfaces:**
- Produces: `MemoryKind`、`MemoryEntry`、`validate_memory_entry(entry)`、`ExperienceMemoryStore`。

- [ ] **Step 1: 写敏感拒绝与权限隔离 RED 测试**

```python
def test_sensitive_memory_is_rejected():
    for value in (r"目录 D:\private", "token=abcdef1234567890", "保存我们的私聊全文"):
        assert not validate_memory_entry(MemoryEntry(MemoryKind.PREFERENCE, "reply_style", value)).allowed

def test_memory_cannot_grant_pro():
    store.put(owner="99999", entry=MemoryEntry(MemoryKind.PREFERENCE, "reply_length", "short"))
    assert AccessPolicy(("<configured-qq-id>",)).resolve_tier("99999") is AccessTier.ORDINARY

def test_relationship_fact_requires_explicit_owner_request():
    entry = MemoryEntry(MemoryKind.RELATIONSHIP, "relationship_fact", "小姚是小江的宝")
    assert not validate_memory_entry(entry, explicit_request=False, is_pro=True).allowed
    assert validate_memory_entry(entry, explicit_request=True, is_pro=True).allowed
```

- [ ] **Step 2: 运行 RED**

Run: `python -m unittest discover -s tests -p 'test_experience_memory.py' -v`

Expected: FAIL，安全记忆模块尚不存在。

- [ ] **Step 3: 实现字段白名单与 DPAPI 存储**

允许键固定为：`reply_length`、`tone`、`preferred_backend`、`voice_enabled`、`relationship_fact`、`task_outcome`。偏好和低敏感关系事实最大 120 字；关系事实必须由 Pro 明确说“请记住”，任务经验只保存后端、匿名任务类别和成功/失败枚举。

```python
ALLOWED_KEYS = {"reply_length", "tone", "preferred_backend", "voice_enabled", "relationship_fact", "task_outcome"}

def validate_memory_entry(entry, *, explicit_request=False, is_pro=False):
    if entry.key not in ALLOWED_KEYS or len(entry.value) > 120:
        return MemoryDecision(False, "field_not_allowed")
    if entry.key == "relationship_fact" and not (explicit_request and is_pro):
        return MemoryDecision(False, "explicit_owner_request_required")
    if redact_sensitive_text(entry.value) != entry.value or re.search(r"私聊|聊天记录|病历|身份证|银行卡", entry.value):
        return MemoryDecision(False, "sensitive_memory")
    return MemoryDecision(True, "allowed")
```

存储文件使用当前用户 DPAPI 与私有 ACL；索引只保存 owner HMAC 和固定字段名。该模块不得被 `access_policy.py` 导入。

- [ ] **Step 4: 运行记忆、权限和隐私回归**

Run: `python -m unittest discover -s tests -p 'test_experience_memory.py' -v`

Run: `python -m unittest discover -s tests -p 'test_agent_access_policy.py' -v`

Run: `python -m unittest discover -s tests -p 'test_runtime_privacy_config.py' -v`

Expected: 全部 PASS。

---

### Task 4: 授权本地声线选择与安全降级

**Files:**
- Modify: `services/local_tts/server.py`
- Modify: `services/local_tts/start_local_tts.ps1`
- Modify: `astrbot/data/plugins/voice_model_router/local_tts_client.py`
- Modify: `astrbot/data/plugins/voice_model_router/_conf_schema.json`
- Test: `tests/test_local_tts_service.py`
- Modify: `tests/test_local_tts_client.py`

**Interfaces:**
- Produces: `/health` 返回固定引擎可用性枚举；只有 `authorized_voice=true` 且私有模型目录通过边界检查时启用 GPT-SoVITS。

- [ ] **Step 1: 写未授权禁用和降级 RED 测试**

```python
def test_gpt_voice_is_disabled_without_explicit_authorization():
    service = TTSService(root, "token", melo_engine=melo, gpt_engine=gpt, authorized_voice=False)
    result = service.synthesize("你好", "gpt_sovits", "token")
    assert result["engine"] == "melo"
    assert gpt.calls == []

async def test_client_falls_back_to_melo_when_primary_health_is_unavailable():
    path = await client.synthesize("你好")
    assert path == safe_wav
    assert calls == ["gpt_sovits", "melo"]
```

- [ ] **Step 2: 运行 RED**

Run: `python -m unittest discover -s tests -p 'test_local_tts_service.py' -v`

Run: `python -m unittest discover -s tests -p 'test_local_tts_client.py' -v`

Expected: FAIL，授权字段和健康枚举尚不存在。

- [ ] **Step 3: 实现授权开关与固定健康响应**

```python
@app.get("/health")
async def health():
    return {"status": "ok", "engines": service.available_engines()}

def available_engines(self):
    engines = ["melo"] if self.melo_engine is not None else []
    if self.authorized_voice and self.gpt_engine is not None:
        engines.insert(0, "gpt_sovits")
    return engines
```

启动脚本默认 `authorized_voice=false`。没有用户自有、授权或合成参考声线时不安装、不加载、不模仿真人；MeloTTS 继续作为稳定默认。

- [ ] **Step 4: 运行语音全链回归和真实 WAV smoke**

Run: `python -m unittest discover -s tests -p 'test_local_tts_service.py' -v`

Run: `python -m unittest discover -s tests -p 'test_local_tts_client.py' -v`

Run: `python -m unittest discover -s tests -p 'test_voice_model_plugin.py' -v`

Expected: 全部 PASS；固定无隐私测试句生成可解析 WAV，发送后/烟测后文件删除。

---

### Task 5: 体验层集成、全量验收与部署

**Files:**
- Modify: `astrbot/data/plugins/claude_code_agent/main.py`
- Modify: `astrbot/data/plugins/claude_code_agent/README.md`
- Modify: `astrbot/data/plugins/voice_model_router/README.md`
- Modify: `progress.md`
- Modify: `findings.md`

- [ ] **Step 1: 写体验层不拥有工具权限的静态测试**

在 `tests/test_experience_boundaries.py` 断言 `response_style.py`、`progress_policy.py`、`experience_memory.py` 不包含 `subprocess`、`build_backend_command`、`upload_group_file`，且 `access_policy.py` 不导入记忆模块。

- [ ] **Step 2: 运行全量测试和编译**

Run: `python -m unittest discover -s tests -p 'test_*.py' -v`

Run: `python -m compileall -q astrbot/data/plugins/claude_code_agent astrbot/data/plugins/voice_model_router services/local_tts/server.py`

Expected: 全部 PASS，编译 exit 0。

- [ ] **Step 3: 重启并验证运行态**

重启 TTS 与 AstrBot，确认 6185/6199/8766 仅环回、8192 关闭、OneBot 已连接、启动错误 0、ACL 不安全项 0、本地 TTS health 正常。

- [ ] **Step 4: 真实 QQ 体验验收**

普通文字任务只出现必要节点且最终回复不超过 4 句；超过 90 秒只增加一次阶段更新；明确要求语音收到本地语音；未要求语音始终是文字；普通用户权限不变；回复、日志、记忆与音频目录无路径、凭据或任务正文残留。

## Plan Self-Review

- Spec coverage: 回复、进度、记忆、语音、错误回退和安全边界均有对应 Task。
- Dependency order: 体验层只消费任务执行内核的 `TaskEvent`，不反向依赖或拥有工具执行能力。
- Activation boundary: GPT-SoVITS 没有授权声线时保持禁用，计划不会以“高质量”为由绕过真人声音授权。
