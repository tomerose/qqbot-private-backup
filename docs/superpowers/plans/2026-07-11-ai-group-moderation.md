# 小柠 AI 自主群管 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 小柠仅在拥有群管理权限时，使用匿名化最小上下文进行 AI 判断，并自主执行撤回和最高 1800 秒禁言，同时永久禁用踢人、拉黑和批量清理成员。

**Architecture:** 在现有 `astrbot_plugin_qqadmin` 内新增三个隔离模块：纯策略、隐私账本、运行处理器。模型只输出结构化建议，本地策略负责置信度、豁免、升级和动作上限；OneBot 层只暴露撤回与禁言。失败时不处罚，审计只保存 HMAC 匿名标识和固定枚举。

**Tech Stack:** Python 3.12、AstrBot 4.26.5、OneBot v11/NapCat、DeepSeek Chat provider、SQLite 标准库、`unittest`。

## Global Constraints

- 仅 QQ `1211000567` 可开关或查询 AI 群管。
- 小柠、小姚、群主和其他管理员永不自动处罚。
- 置信度阈值固定为 `0.90`；禁言阶梯固定为 `0/60/300/1800` 秒，永不超过 `1800` 秒。
- 不保存消息正文、上下文、群名、昵称、原始 QQ/群号、模型原始回答或本机路径。
- 模型输入最多 8 条上下文和 3000 字；说话者匿名化为成员字母，URL 查询参数和疑似秘密必须移除。
- 模型超时 8 秒；模型异常、JSON 无效、角色查询失败或 OneBot 权限不足时不处罚、不盲目重试。
- 代码中不得存在自动踢人、拉黑或批量清理成员的可达路径。
- 当前项目不是 Git 仓库：每个任务以 RED/GREEN 测试证据和 `progress.md` 记录代替 commit，不初始化 Git。

---

### Task 1: 纯策略与匿名化输入

**Files:**
- Create: `astrbot/data/plugins/astrbot_plugin_qqadmin/core/ai_moderation_policy.py`
- Create: `tests/test_ai_moderation_policy.py`

**Interfaces:**
- Produces: `ModerationDecision`, `parse_decision(raw: str)`, `is_candidate(text: str, recent_same: int)`, `sanitize_message(text: str)`, `build_anonymous_context(messages)`, `resolve_action(decision, offense_count)`。

- [x] **Step 1: 写失败测试**

```python
def test_invalid_low_confidence_and_unclear_context_never_punish():
    assert parse_decision("not-json").decision == "none"
    assert parse_decision('{"decision":"recall_and_mute","category":"spam","confidence":0.89,"reason_code":"repeated_spam"}').decision == "none"

def test_action_escalation_is_bounded():
    d = ModerationDecision("recall_and_mute", "spam", 0.99, "repeated_spam")
    assert [resolve_action(d, n).mute_seconds for n in range(4)] == [0, 60, 300, 1800]

def test_context_removes_identifiers_paths_queries_and_secrets():
    text = sanitize_message("QQ1211000567 C:\\Users\\liu token=abc https://x.test/a?q=private")
    assert "1211000567" not in text and "liu" not in text and "private" not in text
```

- [x] **Step 2: 验证 RED**

Run: `python -m unittest discover -s tests -p 'test_ai_moderation_policy.py' -v`

Expected: FAIL，模块尚不存在。

- [x] **Step 3: 最小实现**

```python
@dataclass(frozen=True)
class ModerationDecision:
    decision: Literal["none", "recall", "recall_and_mute"]
    category: str
    confidence: float
    reason_code: str

ALLOWED_CATEGORIES = {"spam", "advertising", "fraud", "harassment", "severe_abuse"}
ALLOWED_REASONS = {"repeated_spam", "bulk_ad", "fraud_lure", "targeted_harassment", "severe_personal_attack"}
MUTE_STEPS = (0, 60, 300, 1800)

def resolve_action(decision: ModerationDecision, offense_count: int) -> ModerationAction:
    if decision.decision == "none" or decision.confidence < 0.90:
        return ModerationAction(False, 0, "none")
    index = min(max(int(offense_count), 0), len(MUTE_STEPS) - 1)
    return ModerationAction(True, MUTE_STEPS[index], decision.reason_code)
```

`parse_decision` 必须只接受对象 JSON、固定枚举和 `0 <= confidence <= 1`；异常统一返回 `none`。`sanitize_message` 删除 Windows 绝对路径、9-12 位数字标识、秘密赋值和 URL query/fragment，并把结果截断到 500 字。`build_anonymous_context` 最多取最后 8 条，总长 3000 字。

- [x] **Step 4: 验证 GREEN**

Run: `python -m unittest discover -s tests -p 'test_ai_moderation_policy.py' -v`

Expected: PASS。

- [x] **Step 5: 保存点**

在 `progress.md` 记录策略测试数量、命令和结果，不执行 Git 操作。

---

### Task 2: 隐私账本与 24 小时升级状态

**Files:**
- Create: `astrbot/data/plugins/astrbot_plugin_qqadmin/core/ai_moderation_store.py`
- Create: `tests/test_ai_moderation_store.py`

**Interfaces:**
- Produces: `AIModerationStore(db_path: Path, salt_path: Path)`, `set_enabled(group_id: str, enabled: bool)`, `is_enabled(group_id: str) -> bool`, `offense_count(group_id: str, user_id: str, now: float) -> int`, `record_action(group_id: str, user_id: str, action: str, reason_code: str, confidence: float, success: bool, now: float) -> None`, `prune(now: float) -> int`。

- [x] **Step 1: 写失败测试**

```python
def test_store_never_contains_raw_ids_or_message_text():
    store = AIModerationStore(db, salt)
    store.set_enabled("945598390", True)
    store.record_action("945598390", "123456789", "recall", "repeated_spam", 0.96, True, now=1000)
    raw = db.read_bytes()
    assert b"945598390" not in raw and b"123456789" not in raw

def test_offenses_expire_after_24_hours():
    store.record_action("g", "u", "recall", "repeated_spam", 0.95, True, now=1000)
    assert store.offense_count("g", "u", now=1001) == 1
    assert store.offense_count("g", "u", now=1000 + 86401) == 0
```

- [x] **Step 2: 验证 RED**

Run: `python -m unittest discover -s tests -p 'test_ai_moderation_store.py' -v`

Expected: FAIL，模块尚不存在。

- [x] **Step 3: 最小实现**

```python
def _fingerprint(self, raw: str) -> str:
    return hmac.new(self._salt, str(raw).encode(), hashlib.sha256).hexdigest()

CREATE TABLE settings(group_hash TEXT PRIMARY KEY, enabled INTEGER NOT NULL);
CREATE TABLE offenses(group_hash TEXT, user_hash TEXT, occurred_at REAL NOT NULL);
CREATE TABLE audit(
  occurred_at REAL NOT NULL, group_hash TEXT NOT NULL, user_hash TEXT NOT NULL,
  action TEXT NOT NULL, reason_code TEXT NOT NULL, confidence_bucket TEXT NOT NULL,
  success INTEGER NOT NULL
);
```

盐文件首次启动使用 `secrets.token_bytes(32)` 创建；目录和文件 ACL 仅保留当前用户、SYSTEM、Administrators。数据库只能使用参数化 SQL；置信度只保存 `90-94/95-97/98-100` 区间，不保存原始模型响应。

- [x] **Step 4: 验证 GREEN 与原始字节扫描**

Run: `python -m unittest discover -s tests -p 'test_ai_moderation_store.py' -v`

Expected: PASS，数据库原始字节不包含测试中的群号、QQ号或正文。

- [x] **Step 5: 保存点**

在 `progress.md` 记录数据库字段白名单和 ACL 验证结果。

---

### Task 3: AI 判断处理器与 OneBot 安全执行

**Files:**
- Create: `astrbot/data/plugins/astrbot_plugin_qqadmin/core/ai_moderation_handler.py`
- Create: `tests/test_ai_moderation_handler.py`

**Interfaces:**
- Consumes: Task 1 policy API、Task 2 store API、AstrBot `Context`、`AiocqhttpMessageEvent`。
- Produces: `AIModerationHandler.handle(event)`, `AIModerationHandler.terminate()`。

- [x] **Step 1: 使用 FakeBot/FakeProvider 写失败测试**

```python
VALID_VIOLATION_JSON = '{"decision":"recall_and_mute","category":"fraud","confidence":0.98,"reason_code":"fraud_lure"}'

class FakeBot:
    def __init__(self, bot_role="admin", sender_role="member"):
        self.bot_role = bot_role
        self.sender_role = sender_role
        self.calls = []

    async def get_group_member_info(self, group_id, user_id, no_cache=True):
        role = self.bot_role if user_id == 3806573022 else self.sender_role
        return {"role": role, "user_id": user_id}

    async def delete_msg(self, message_id):
        self.calls.append(("delete_msg", message_id))

    async def set_group_ban(self, group_id, user_id, duration):
        self.calls.append(("set_group_ban", group_id, user_id, duration))

class FakeProvider:
    def __init__(self, outcome):
        self.outcome = outcome

    async def text_chat(self, system_prompt, prompt):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return SimpleNamespace(completion_text=self.outcome)

class FakeContext:
    def __init__(self, provider):
        self.provider = provider

    def get_provider_by_id(self, provider_id):
        return self.provider if provider_id == "deepseek-chat" else None

class FakeStore:
    def __init__(self, prior_offenses=0):
        self.prior_offenses = prior_offenses
        self.records = []

    def is_enabled(self, group_id):
        return True

    def offense_count(self, group_id, user_id, now):
        return self.prior_offenses

    def record_action(self, group_id, user_id, action, reason_code, confidence, success, now):
        self.records.append((action, reason_code, success))

class FakeEvent:
    def __init__(self, bot, sender_id):
        self.bot = bot
        self.message_str = "加群领取返现，点击 https://example.test/pay?q=secret"
        self.message_obj = SimpleNamespace(message_id=10001)
        self._sender_id = sender_id

    def get_group_id(self): return "945598390"
    def get_sender_id(self): return str(self._sender_id)
    def get_self_id(self): return "3806573022"

def build_handler(bot, model_json=None, model_outcome=None, prior_offenses=0):
    outcome = model_json if model_outcome is None else model_outcome
    return AIModerationHandler(
        context=FakeContext(FakeProvider(outcome)),
        store=FakeStore(prior_offenses),
        provider_id="deepseek-chat",
        timeout_seconds=8,
        context_messages=8,
    )

async def test_non_admin_bot_and_exempt_sender_never_call_actions():
    bot = FakeBot(bot_role="member")
    handler = build_handler(bot, model_json=VALID_VIOLATION_JSON)
    await handler.handle(FakeEvent(bot=bot, sender_id=123456789))
    assert bot.calls == []

async def test_valid_decision_recalls_then_uses_local_mute_duration():
    bot = FakeBot()
    handler = build_handler(bot, model_json=VALID_VIOLATION_JSON, prior_offenses=1)
    await handler.handle(FakeEvent(bot=bot, sender_id=123456789))
    assert bot.calls[0][0] == "delete_msg"
    assert bot.calls[1] == ("set_group_ban", 945598390, 123456789, 60)

async def test_timeout_invalid_json_and_provider_error_fail_open():
    for outcome in (asyncio.TimeoutError(), "not-json", RuntimeError("provider down")):
        bot = FakeBot()
        handler = build_handler(bot, model_outcome=outcome)
        await handler.handle(FakeEvent(bot=bot, sender_id=123456789))
        assert bot.calls == []

async def test_handler_has_no_kick_action():
    assert "set_group_kick" not in inspect.getsource(AIModerationHandler)
```

测试必须断言发送给 FakeProvider 的 prompt 不包含原始 QQ/群号、昵称、本机路径、URL query 或 token。

- [x] **Step 2: 验证 RED**

Run: `python -m unittest discover -s tests -p 'test_ai_moderation_handler.py' -v`

Expected: FAIL，处理器尚不存在。

- [x] **Step 3: 实现最小处理器**

```python
class AIModerationHandler:
    async def handle(self, event):
        if not await self._eligible(event):
            return
        self._remember_in_memory(event)
        if not is_candidate(event.message_str, self._recent_same_count(event)):
            return
        async with self._group_lock(event.get_group_id()):
            raw = await asyncio.wait_for(self._judge(event), timeout=8)
            decision = parse_decision(raw)
            count = self.store.offense_count(event.get_group_id(), event.get_sender_id(), time.time())
            action = resolve_action(decision, count)
            if not action.recall:
                return
            success = await self._recall_then_optional_mute(event, action.mute_seconds)
            self.store.record_action(
                str(event.get_group_id()),
                str(event.get_sender_id()),
                "mute" if action.mute_seconds else "recall",
                action.reason_code,
                decision.confidence,
                success,
                time.time(),
            )
```

`_eligible` 必须实时查询 bot 和 sender 的群角色；任何查询异常返回 false。上下文只保存在 `deque(maxlen=8)` 内存中，按群隔离，`terminate()` 清空。模型使用 `context.get_provider_by_id("deepseek-chat")`，不存在时不回退到聊天人格 provider。

- [x] **Step 4: 验证 GREEN**

Run: `python -m unittest discover -s tests -p 'test_ai_moderation_handler.py' -v`

Expected: PASS；FakeBot 只收到 `delete_msg` 和 `set_group_ban`。

- [x] **Step 5: 保存点**

记录模型超时、无效 JSON、无权限和正常处罚四条测试证据。

---

### Task 4: QQAdmin 集成、开关命令与永久禁踢

**Files:**
- Modify: `astrbot/data/plugins/astrbot_plugin_qqadmin/main.py`
- Modify: `astrbot/data/plugins/astrbot_plugin_qqadmin/core/__init__.py`
- Modify: `astrbot/data/plugins/astrbot_plugin_qqadmin/_conf_schema.json`
- Modify: `astrbot/data/config/astrbot_plugin_qqadmin_config.json`
- Create: `tests/test_qqadmin_ai_integration.py`

**Interfaces:**
- Adds config: `ai_moderation.enabled=false`, `provider_id="deepseek-chat"`, `confidence_threshold=0.90`, `timeout_seconds=8`, `context_messages=8`。
- Adds commands: `/AI群管开`, `/AI群管关`, `/AI群管状态`，仅 `OWNER_ID="1211000567"`。

- [x] **Step 1: 写失败集成测试**

```python
def test_only_owner_can_toggle_ai_moderation():
    source = Path(PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
    assert 'OWNER_ID = "1211000567"' in source
    assert "get_sender_id" in source and "AI群管开" in source

def test_kick_block_and_member_cleanup_always_return_disabled():
    source = Path(PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
    assert source.count("该能力已永久关闭") >= 1
    for method in ("set_group_kick", "set_group_block", "clear_group_member"):
        body = source.split(f"async def {method}", 1)[1].split("async def ", 1)[0]
        assert "set_group_kick(" not in body

def test_ai_config_defaults_are_safe_and_disabled():
    schema = json.loads(Path(PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
    items = schema["ai_moderation"]["items"]
    assert items["enabled"]["default"] is False
    assert items["confidence_threshold"]["default"] == 0.90
    assert items["timeout_seconds"]["default"] == 8
```

- [x] **Step 2: 验证 RED**

Run: `python -m unittest discover -s tests -p 'test_qqadmin_ai_integration.py' -v`

Expected: FAIL，新命令和禁踢保护尚不存在。

- [x] **Step 3: 接入处理器和命令**

```python
OWNER_ID = "1211000567"
PERMANENTLY_DISABLED = "该能力已永久关闭。小柠只能撤回和有限时长禁言。"

@filter.event_message_type(EventMessageType.GROUP_MESSAGE)
async def ai_moderate(self, event):
    await self.ai_moderation.handle(event)

async def set_group_kick(self, event):
    await event.send(event.plain_result(PERMANENTLY_DISABLED))

async def set_group_block(self, event):
    await event.send(event.plain_result(PERMANENTLY_DISABLED))

async def clear_group_member(self, event, inactive_days=None, under_level=None):
    await event.send(event.plain_result(PERMANENTLY_DISABLED))
```

开关命令必须先检查 sender ID，再检查群聊；回复不显示群号、成员数或路径。默认配置保持关闭，避免重启后未经确认自动处罚；部署验证后只为小柠确有管理员权限的群启用。

- [x] **Step 4: 验证 GREEN 与全量回归**

Run: `python -m unittest discover -s tests -p 'test_*.py' -v`

Expected: 全部 PASS，且现有 31 项测试无回归。

- [x] **Step 5: 保存点**

更新 `progress.md` 和 QQAdmin README，记录新命令、隐私字段白名单和永久禁踢边界。

---

### Task 5: 上线与运行态验证

**Files:**
- Modify: `astrbot/data/config/astrbot_plugin_qqadmin_config.json`（仅在测试通过后启用 AI 群管总开关）
- Verify: `astrbot-startup.stdout.log`、本机监听端口、OneBot WebSocket。

- [x] **Step 1: 完成前安全扫描**

Run: `rg -n 'set_group_kick|set_group_block|clear_group_member|message_str|completion_text|group_id|sender_id' astrbot/data/plugins/astrbot_plugin_qqadmin/core/ai_moderation_*.py`

Expected: 处理器不存在踢人调用；正文和模型输出只在内存变量中出现，不进入 SQL 或 logger。

- [x] **Step 2: 语法、测试和插件导入**

Run: `python -m py_compile astrbot/data/plugins/astrbot_plugin_qqadmin/main.py astrbot/data/plugins/astrbot_plugin_qqadmin/core/ai_moderation_*.py`

Run: `python -m unittest discover -s tests -p 'test_*.py' -v`

Expected: exit 0，零失败。

- [x] **Step 3: 重启 AstrBot**

终止 AstrBot 完整进程树，使用隐藏窗口在 `astrbot` 工作目录重新启动；不重启或修改 NapCat 登录态。

- [x] **Step 4: 运行态检查**

确认：`6185`、`6199` 仅监听 `127.0.0.1`；`8192` 未监听；日志出现 QQAdmin 插件加载、`AstrBot started` 和 OneBot 适配器连接，且没有 traceback/error。

- [x] **Step 5: 无副作用模拟**

使用 FakeBot 或独立测试事件验证：普通玩笑无动作；高置信违规只产生撤回；重复违规禁言时长遵循阶梯；非管理员 bot 无动作。不得在真实群发送测试违规内容或处罚真实成员。

- [x] **Step 6: 最终记录**

将测试总数、监听端口、连接状态、ACL 和未执行真实群处罚写入 `progress.md`；只有证据齐全后才报告完成。
