# Pro 审批与成员记录加固 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement task-by-task with tests first.

**Goal:** 加入二次确认、可恢复验证码补发、脱敏审计和 Pro 成员记录签名校验。

**Architecture:** `ProStore` 是状态机、审计和签名的唯一写入点；QQ 插件只负责命令与私聊投递；作图插件只读验证已签名的 active 记录。

**Tech Stack:** Python 3、SQLite、标准库 `hmac`/`hashlib`/`secrets`、unittest。

## Global Constraints

- 公开 Pro 不得获得本机 Agent 权限；审核人与可信 Agent 用户只允许 `1211000567`。
- 不向群聊、审计或 Git 输出验证码、邮箱正文、QQ 号、机器路径或签名密钥。
- 不新增依赖；数据库迁移兼容现有库；签名异常 fail closed。

---

### Task 1: 二次确认状态机

**Files:**
- Modify: `astrbot/data/plugins/pro_application/pro_store.py`
- Test: `tests/test_pro_store.py`

**Interfaces:**
- `request_approval(application_id, reviewer_id, days, *, now) -> None`
- `confirm_approval(application_id, reviewer_id, *, now) -> str`
- `resend_verification(application_id, reviewer_id, *, now) -> str`

- [ ] Step 1: 先加入失败测试：`approve` 前必须进入 `approval_pending_confirm`；五分钟未确认恢复 `awaiting_review`；补发后旧码无效且 60 秒内再次补发抛出 `resend_rate_limited`。
- [ ] Step 2: 运行 `python tests\\test_pro_store.py`，确认因上述方法不存在而失败。
- [ ] Step 3: 增加迁移列 `approval_confirm_expires_at` 和 `last_code_sent_at`；实现确认超时清理、三种方法和事件 `approval_requested`、`approval_confirmed`、`verification_resent`。确认后才生成哈希验证码；补发原子替换哈希并重置失败次数。
- [ ] Step 4: 再运行 `python tests\\test_pro_store.py`，确认通过。
- [ ] Step 5: `git add astrbot/data/plugins/pro_application/pro_store.py tests/test_pro_store.py; git commit -m "feat: require confirmed Pro approvals"`。

### Task 2: QQ 管理命令和最小审计

**Files:**
- Modify: `astrbot/data/plugins/pro_application/pro_store.py`
- Modify: `astrbot/data/plugins/pro_application/main.py`
- Test: `tests/test_pro_store.py`
- Test: `tests/test_pro_application_plugin.py`

**Interfaces:**
- `audit_for(application_id, reviewer_id, *, now, limit=20) -> tuple[AuditEvent, ...]`
- `AuditEvent(event_type: str, event_at: float)` 不含 QQ、验证码或邮箱字段。

- [ ] Step 1: 先加入失败测试：仅审核人可读审计；`/pro approve` 不发验证码且只提示 `/pro confirm APP-ID`；确认/补发后的验证码只出现在申请人的私聊链。
- [ ] Step 2: 运行 `python tests\\test_pro_store.py; python tests\\test_pro_application_plugin.py`，确认新接口/命令失败。
- [ ] Step 3: 实现 `audit_for`（最多 20 条事件类型和时间）；将 `/pro approve` 改为请求确认；新增 `/pro confirm APP-ID`、`/pro resend APP-ID`、`/pro audit APP-ID`。确认投递失败重置到待审核；补发投递失败保留 `awaiting_verify`，供稍后重发。
- [ ] Step 4: 运行两组测试确认通过。
- [ ] Step 5: `git add astrbot/data/plugins/pro_application/pro_store.py astrbot/data/plugins/pro_application/main.py tests/test_pro_store.py tests/test_pro_application_plugin.py; git commit -m "feat: add auditable Pro approval delivery"`。

### Task 3: HMAC 成员完整性

**Files:**
- Modify: `astrbot/data/plugins/pro_application/pro_store.py`
- Modify: `astrbot/data/plugins/draw_command/pro_access.py`
- Test: `tests/test_pro_store.py`
- Test: `tests/test_pro_access.py`

**Interfaces:**
- active 记录写入 `membership_signature`。
- `draw_command.pro_access.is_active_pro(qq_id, db_path, now=None) -> bool` 在签名缺失或不匹配时返回 `False`。

- [ ] Step 1: 先加入失败测试：激活记录有签名；直接修改 `pro_expires_at` 后动态 Pro 失败；签名缺失/错误也失败。
- [ ] Step 2: 运行 `python tests\\test_pro_store.py; python tests\\test_pro_access.py`，确认测试失败。
- [ ] Step 3: 密钥优先读取 `XIAONING_PRO_SIGNING_KEY`，否则创建并复用 Git 忽略的 `pro_members.key`；以 `application_id|qq_id|state|pro_expires_at` 生成 HMAC-SHA256。激活写入签名，撤销/到期清除签名，读侧使用 `hmac.compare_digest` 验证；验证码比较同样改为常量时间比较。
- [ ] Step 4: 运行 `python tests\\test_pro_store.py; python tests\\test_pro_access.py; python tests\\test_draw_plugin.py; python tests\\test_agent_access_policy.py`，确认通过。
- [ ] Step 5: `git add astrbot/data/plugins/pro_application/pro_store.py astrbot/data/plugins/draw_command/pro_access.py tests/test_pro_store.py tests/test_pro_access.py; git commit -m "fix: verify signed Pro memberships"`。

### Task 4: 部署与私有备份

**Files:**
- Modify: `docs/superpowers/specs/2026-07-12-pro-approval-hardening-design.md`
- Modify: `docs/superpowers/plans/2026-07-12-pro-approval-hardening-implementation.md`

- [ ] Step 1: 运行 `python -m unittest discover -s tests -p 'test_*.py'`，确认全绿。
- [ ] Step 2: 运行 `git diff --check; git status --short`，确认没有密钥、验证码或运行数据被暂存。
- [ ] Step 3: 重启 `D:\Claudecoda学习\qqbot\astrbot` 中的 `python -m astrbot.cli run`，验证 6185/6199 为回环监听、QQ WebSocket 已连通且无插件异常。
- [ ] Step 4: 提交文档、执行 `git push origin main`，并用 `gh repo view tomerose/qqbot-private-backup --json isPrivate,nameWithOwner,url` 确认仓库私有。
