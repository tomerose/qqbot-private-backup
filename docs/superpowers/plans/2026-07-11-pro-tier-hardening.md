# 小柠普通版 / Pro 分层与 Agent 安全修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 Agent 真实注册、安全分类、恢复幂等、交付隐私、输出边界、日志 ACL 和语音残留，并建立不可由聊天提升的普通版 / Pro 双层权限。

**Architecture:** 以纯函数策略模块作为所有权限和动作分类的唯一入口；Agent 首次执行与恢复执行共享交付提交路径；所有外发文件先经过本地 DLP。普通版继续使用 AstrBot 对话与语音链路，只有本机 allowlist 中的 Pro 才能进入 Agent 状态机。

**Tech Stack:** Python 3.12、AstrBot 4.26.5、SQLite、Windows DPAPI/ACL、OneBot v11、FastAPI、本地 MeloTTS、`unittest`。

## Global Constraints

- 当前 Pro allowlist 只包含 QQ `<configured-qq-id>`，不得通过聊天、人格、记忆或群身份修改。
- 群聊 Pro Agent 必须存在真实 `At` 组件。
- 普通用户不能访问本机 Agent、任务状态、取消、恢复或本机文件。
- 未知动作默认二次确认；自动恢复只允许严格只读动作。
- 任何失败不得向 QQ 或日志写入任务正文、本机绝对路径、文件内容、令牌或确认码。
- 项目不是 Git 仓库，不执行 commit、branch 或 PR；保留最小补丁和完整验证证据。

---

### Task 1: 双层权限与真实 AstrBot 注册

**Files:**
- Create: `astrbot/data/plugins/claude_code_agent/access_policy.py`
- Modify: `astrbot/data/plugins/claude_code_agent/main.py`
- Modify: `astrbot/data/plugins/claude_code_agent/_conf_schema.json`
- Test: `tests/test_agent_access_policy.py`
- Test: `tests/test_agent_plugin_registration.py`

**Interfaces:**
- Produces: `AccessTier`, `Capability`, `AccessPolicy.resolve_tier(sender_id)`, `AccessPolicy.authorize(sender_id, capability)`。
- Produces: AstrBot `AdapterMessageEvent` 注册的 `on_message` handler。

- [x] **Step 1: 写权限矩阵和注册表 RED 测试**

覆盖普通用户的命令、自然语言、状态、取消均被拒绝；Pro 可进入；聊天文本不能修改 allowlist；导入插件后 registry 必须包含 `on_message`。

- [x] **Step 2: 运行 RED 测试**

Run: `python -m unittest discover -s tests -p 'test_agent_access_policy.py' -v`

Run: `python -m unittest discover -s tests -p 'test_agent_plugin_registration.py' -v`

Expected: FAIL，原因分别为策略模块缺失和 `on_message` 未注册。

- [x] **Step 3: 实现最小权限策略并注册 handler**

`AccessPolicy` 构造时冻结 allowlist；`main.py` 使用 `@filter.platform_adapter_type(filter.PlatformAdapterType.ALL)`；所有 `/agent` 与自然语言分支在解析任务前统一检查 `Capability.LOCAL_AGENT`。

- [x] **Step 4: 运行 Task 1 测试与现有自然路由测试**

Run: `python -m unittest discover -s tests -p 'test_agent_access_policy.py' -v`

Run: `python -m unittest discover -s tests -p 'test_agent_plugin_registration.py' -v`

Run: `python -m unittest discover -s tests -p 'test_natural_agent_router.py' -v`

Expected: PASS。

---

### Task 2: Fail-closed 动作分类与恢复白名单

**Files:**
- Create: `astrbot/data/plugins/claude_code_agent/action_policy.py`
- Modify: `astrbot/data/plugins/claude_code_agent/agent_core.py`
- Modify: `astrbot/data/plugins/claude_code_agent/recovery_policy.py`
- Modify: `astrbot/data/plugins/claude_code_agent/main.py`
- Test: `tests/test_agent_action_policy.py`
- Test: `tests/test_agent_recovery_policy.py`

**Interfaces:**
- Produces: `ActionClass`, `ActionAssessment`, `classify_action(task)`。
- Changes: `assess_task_risk()` 对 `HIGH_IMPACT`、`UNKNOWN` 要求审批。
- Changes: `assess_recovery()` 仅对 `READ_ONLY` 返回 resumable。

- [x] **Step 1: 写已知绕过表达 RED 测试**

“覆盖旧配置”“把文件挪走”“分享报告”“实现登录”“运行部署脚本”不得自动恢复；前三类高影响/未知动作必须确认。只读“检查并解释代码”可恢复。

- [x] **Step 2: 运行 RED 测试**

Run: `python -m unittest discover -s tests -p 'test_agent_action_policy.py' -v`

Run: `python -m unittest discover -s tests -p 'test_agent_recovery_policy.py' -v`

Expected: FAIL，现有 blacklist 会把绕过表达判成 replay-safe。

- [x] **Step 3: 实现动作分类和默认拒绝恢复**

分类顺序固定为高影响→工作区写入→严格只读→未知；未知不允许恢复。审批原因使用固定枚举文案，不回显任务内容。

- [x] **Step 4: 运行 Task 2 与 Agent 核心测试**

Run: `python -m unittest discover -s tests -p 'test_agent_action_policy.py' -v`

Run: `python -m unittest discover -s tests -p 'test_agent_recovery_policy.py' -v`

Run: `python -m unittest discover -s tests -p 'test_agent_core.py' -v`

Expected: PASS。

---

### Task 3: 恢复幂等与统一群文件交付

**Files:**
- Modify: `astrbot/data/plugins/claude_code_agent/main.py`
- Modify: `astrbot/data/plugins/claude_code_agent/job_store.py`
- Test: `tests/test_agent_integration.py`
- Test: `tests/test_agent_job_store.py`

**Interfaces:**
- Produces: 单一 `_commit_and_deliver(...)` 完成路径。
- Guarantees: `delivery_digest` 写入后永不重新执行；`delivery_cursor` 每个文件成功后原子更新。

- [x] **Step 1: 写二次重启和恢复群文件 RED 测试**

模拟恢复执行完成、写摘要后再次中断；第二次启动必须 `executed == []`，只补发未交付文件。恢复执行产生的群文件必须调用 `upload_group_file`。

- [x] **Step 2: 运行 RED 测试**

Run: `python -m unittest discover -s tests -p 'test_agent_integration.py' -v`

Expected: FAIL，恢复执行分支缺少摘要和游标提交。

- [x] **Step 3: 统一首次执行与恢复执行交付状态机**

执行成功后先计算并写入 manifest，再进入 delivering；恢复分支复用 `_deliver_recovered_files`，禁止复制第二套发送循环。

- [x] **Step 4: 运行 Task 3 测试**

Run: `python -m unittest discover -s tests -p 'test_agent_integration.py' -v`

Run: `python -m unittest discover -s tests -p 'test_agent_job_store.py' -v`

Expected: PASS。

---

### Task 4: 交付物本地 DLP

**Files:**
- Create: `astrbot/data/plugins/claude_code_agent/delivery_dlp.py`
- Modify: `astrbot/data/plugins/claude_code_agent/agent_core.py`
- Modify: `astrbot/data/plugins/claude_code_agent/main.py`
- Test: `tests/test_delivery_dlp.py`
- Modify: `tests/test_agent_core.py`

**Interfaces:**
- Produces: `DLPDecision`, `inspect_deliverable(path, root, limits) -> DLPDecision`。
- Consumes: existing sensitive-name and allowed-root validation。

- [x] **Step 1: 写文本、ZIP、路径穿越和普通文件名敏感内容 RED 测试**

覆盖 `report.txt` 含 API key、本机绝对路径、ZIP 内 `.env`、`../`、嵌套 ZIP、超量成员；干净源码和普通图片通过。

- [x] **Step 2: 运行 RED 测试**

Run: `python -m unittest discover -s tests -p 'test_delivery_dlp.py' -v`

Expected: FAIL，模块尚不存在。

- [x] **Step 3: 实现有界 DLP 并接入全部发送入口**

文本最多扫描 5 MB；ZIP 最多 100 个成员、总解压上限 20 MB、单成员 5 MB；不解压到磁盘；DLP 阻止原因只保留固定代码。图片剥离 EXIF 后发送。

- [x] **Step 4: 运行 Task 4 测试**

Run: `python -m unittest discover -s tests -p 'test_delivery_dlp.py' -v`

Run: `python -m unittest discover -s tests -p 'test_agent_core.py' -v`

Expected: PASS。

---

### Task 5: 有界进程输出和真实错误摘要

**Files:**
- Create: `astrbot/data/plugins/claude_code_agent/bounded_process_io.py`
- Modify: `astrbot/data/plugins/claude_code_agent/main.py`
- Test: `tests/test_bounded_process_io.py`
- Modify: `tests/test_agent_integration.py`

**Interfaces:**
- Produces: `capture_bounded_process(proc, stdout_limit, stderr_limit, timeout)`。
- Returns: bounded stdout/stderr, return code and terminal reason。

- [x] **Step 1: 写 stdout/stderr 超限、超时和脱敏错误 RED 测试**

真实子进程持续输出时必须提前终止；stderr 中路径和 token 不得进入回复；非零退出返回有用但有界的原因。

- [x] **Step 2: 运行 RED 测试**

Run: `python -m unittest discover -s tests -p 'test_bounded_process_io.py' -v`

Expected: FAIL，模块尚不存在。

- [x] **Step 3: 实现并替换 `communicate()`**

并发读取两个 stream；达到任一硬上限立即调用既有进程树终止；仅保留尾部脱敏错误摘要。

- [x] **Step 4: 运行 Task 5 测试**

Run: `python -m unittest discover -s tests -p 'test_bounded_process_io.py' -v`

Run: `python -m unittest discover -s tests -p 'test_agent_integration.py' -v`

Expected: PASS。

---

### Task 6: 日志隐私与 ACL

**Files:**
- Create: `services/harden_runtime_privacy.ps1`
- Modify: `astrbot/data/cmd_config.json`
- Modify: `astrbot/data/plugins/claude_code_agent/main.py`
- Test: `tests/test_runtime_privacy_config.py`

**Interfaces:**
- Produces: 幂等 PowerShell ACL 收紧脚本。
- Changes: AstrBot 生产日志级别为 `WARNING`。

- [x] **Step 1: 写日志配置和 ACL 脚本静态 RED 测试**

配置不得为 INFO；脚本必须移除继承并只授予当前 SID、SYSTEM、Administrators；不得删除历史日志。

- [x] **Step 2: 运行 RED 测试**

Run: `python -m unittest discover -s tests -p 'test_runtime_privacy_config.py' -v`

Expected: FAIL，当前配置为 INFO 且无脚本。

- [x] **Step 3: 实现配置和脚本并在本机执行**

脚本范围仅包含项目启动日志、`astrbot/data/logs`、Agent state 和 TTS state/audio；先验证所有解析后的目标都在项目根内。

- [x] **Step 4: 运行测试并读取 ACL 验证**

Run: `python -m unittest discover -s tests -p 'test_runtime_privacy_config.py' -v`

Run: `icacls astrbot-startup.stdout.log` and `icacls astrbot\data\logs`

Expected: 无 `Users`、`Authenticated Users`、`Everyone`。

---

### Task 7: 语音生命周期清理

**Files:**
- Modify: `astrbot/data/plugins/voice_model_router/main.py`
- Modify: `services/local_tts/server.py`
- Test: `tests/test_voice_model_plugin.py`
- Test: `tests/test_local_tts_service.py`

**Interfaces:**
- Produces: 发送完成后音频清理 hook；服务后台周期清理任务。

- [x] **Step 1: 写发送完成和空闲清理 RED 测试**

只有标记为本插件生成、位于私有音频根且为普通文件的 WAV 可删除；任意外部路径和符号链接不得删除。

- [x] **Step 2: 运行 RED 测试**

Run: `python -m unittest discover -s tests -p 'test_voice_model_plugin.py' -v`

Run: `python -m unittest discover -s tests -p 'test_local_tts_service.py' -v`

Expected: FAIL，当前仅在下一次合成前清理。

- [x] **Step 3: 实现安全清理**

记录本次生成音频的规范路径；消息发送后删除；FastAPI lifespan 启动周期任务清理超过 10 分钟的 WAV，关闭时取消任务。

- [x] **Step 4: 运行 Task 7 测试和真实本地 WAV smoke**

Run: `python -m unittest discover -s tests -p 'test_voice_model_plugin.py' -v`

Run: `python -m unittest discover -s tests -p 'test_local_tts_service.py' -v`

Expected: PASS；固定无隐私测试句生成可解析 WAV。

---

### Task 8: 文档、全量回归和运行态部署

**Files:**
- Modify: `astrbot/data/plugins/claude_code_agent/README.md`
- Modify: `astrbot/data/plugins/voice_model_router/README.md`
- Modify: `progress.md`
- Modify: `findings.md`
- Modify: `task_plan.md`

**Interfaces:**
- Consumes: Tasks 1-7 全部行为。
- Produces: 可验证的运行态和用户验收说明。

- [x] **Step 1: 更新普通版 / Pro 能力说明和残余风险**

明确普通用户无本机能力、Pro allowlist、审批、恢复白名单、DLP、日志和语音清理；不写密钥和隐私路径。

- [x] **Step 2: 运行全量测试和语法编译**

Run: `python -m unittest discover -s tests -p 'test_*.py' -v`

Run: 对两个插件目录和 `services/local_tts/server.py` 执行 `py_compile`。

Expected: 全部 PASS，编译 exit 0。

- [x] **Step 3: 执行隐私与依赖检查**

Run: 代码日志拼接扫描、配置 JSON 解析、私有目录 ACL 检查、主环境与 TTS venv `pip check`。

Expected: 新增组件无敏感日志；TTS venv 无冲突。既有 AstrBot 全局依赖冲突作为残余风险记录，不在本轮强行升级。

- [x] **Step 4: 重启 TTS 与 AstrBot**

确认插件注册、OneBot 重连、6185/6199/8766 只监听 `127.0.0.1`、8192 未监听、启动错误为 0。

- [ ] **Step 5: 实际 QQ 验收**

owner 发送“帮我生成一个只含 hello 的 txt”；普通账号同类请求不得执行；owner 高风险测试只显示确认提示。不得执行真实删除、安装、外发或系统操作。

## Plan Self-Review

- Spec coverage: 权限、注册、分类、恢复、DLP、输出、日志、语音、测试和部署均有对应 Task。
- Placeholder scan: 无 TBD/TODO/“类似处理”；每个 Task 给出目标文件、接口、命令和预期结果。
- Interface consistency: `AccessPolicy`、`ActionAssessment`、`DLPDecision`、`capture_bounded_process` 均先定义后消费；首次与恢复交付统一为同一内部路径。
- Scope: 不迁移全局 AstrBot 环境，不新增付费或邀请码，不启用真人声线，符合设计非目标。
