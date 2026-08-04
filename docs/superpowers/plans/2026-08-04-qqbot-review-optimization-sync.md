# 小柠 QQBot 审查·优化·GitHub 同步 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 19 个测试失败（对齐"全员 X 开放"新契约），按主题提交 95 文件 WIP，合并 main 清理分支，建立 CI 与仓库规范，产出谷歌生态深化清单。

**Architecture:** 5 阶段。阶段 0 修复测试（R1 修代码 / R2 修测试，测试=规格）；阶段 1 Git 同步（8 组提交→合并 main→删分支）；阶段 2 CI（windows-latest + Py3.12 全量测试 + ruff 致命规则）；阶段 3 仓库规范；阶段 4 谷歌深化审查出清单。每阶段独立验收。

**Tech Stack:** Python 3.12（`C:\Users\liu\AppData\Local\Programs\Python\Python312\python.exe`）、unittest、AstrBot 4.26.5、GitHub Actions、ruff

## Global Constraints

- 测试解释器必须是 Python 3.12（PATH 默认 `python` 是 3.10，无 astrbot 包，会报 46 个假失败）。测试命令统一：`"/c/Users/liu/AppData/Local/Programs/Python/Python312/python.exe" -m unittest discover -s tests -p '<file.py>'`
- 全员 X 开放为已确认契约：`get_tier()` 恒返回 X、`use_agent()` 恒 True、`is_active_pro()` 恒 True（用户 2026-08-04 批准 WIP 方向）
- 群聊无真实 @ 永不执行（安全边界，不可因开放而放宽）
- 修复不改变运行中 bot 的对外行为，除非测试契约要求
- `sing_command` 及其测试在 `.gitignore`（本地实验，不提交）；修复只为本地 564 全绿
- 仓库私有（tomerose/qqbot-private-backup）；push 前扫描密钥
- 不重写 git 历史
- **提交原子性（T2-T5 修订）**：每个测试更新任务必须把该测试依赖的 WIP 生产文件（工作区版本）一起提交，保证中间提交单独 checkout 也全绿。验证法：提交后 `git worktree add` 到该提交跑相关测试，缺文件则补进同一提交，直到绿。T2 至少含 `draw_command/*` + `pro_application/*` + `xiaoning_runtime.py` + `xiaoning_capabilities.py`（draw 测试 import 链）；T3 含 `emotional_chat/*`；T4 含 `pdf_analysis/*` + `video_command/*`；T5 含 `claude_code_agent/*` + `contact_pro_info/*`。T8 剩余 g1-g3 与散件照常。

---

### Task 1: 阶段0a — pro_access 测试重写（对齐开放契约）

**Files:**
- Modify: `tests/test_pro_access.py`（整体重写）
- 不碰 `astrbot/data/plugins/draw_command/pro_access.py`（WIP 已按契约实现）

**Interfaces:**
- Consumes: `draw_command.pro_access.{Tier, get_tier, agent_available, use_agent, is_active_pro}` — 均为全员开放实现
- Produces: 5 个新测试，断言新契约

现状：7 个失败全是旧分级断言（签名校验、DB 篡改、申请人=ORDINARY）。WIP 已移除分级逻辑，这些测试失去意义。重写为开放契约测试：

- [ ] **Step 1: 重写 `tests/test_pro_access.py`**

```python
import sys
import tempfile
import unittest
from pathlib import Path


PLUGINS_DIR = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS_DIR))

from draw_command.pro_access import (  # noqa: E402
    Tier,
    agent_available,
    get_tier,
    is_active_pro,
    use_agent,
)


class ProAccessTests(unittest.TestCase):
    """Open-access contract: every QQ user is treated as Tier.X (unified access)."""

    def test_get_tier_returns_x_for_everyone(self):
        self.assertEqual(get_tier("2000000000", Path("missing.db")), Tier.X)

    def test_tier_ignores_missing_or_corrupt_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.db"
            broken = Path(tmp) / "broken.db"
            broken.write_text("not sqlite", encoding="utf-8")
            self.assertEqual(get_tier("2000000000", missing), Tier.X)
            self.assertEqual(get_tier("2000000000", broken), Tier.X)

    def test_agent_available_to_everyone(self):
        self.assertEqual(agent_available("2000000000", Path("missing.db")), (True, ""))

    def test_agent_usage_is_unlimited_for_all(self):
        self.assertTrue(use_agent("2000000000", Path("missing.db")))
        self.assertTrue(use_agent("2000000000", Path("missing.db")))

    def test_is_active_pro_is_true_for_all(self):
        self.assertTrue(is_active_pro("2000000000", Path("missing.db")))
        self.assertTrue(is_active_pro("2000000000", Path("missing.db")))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行验证**

Run: `PYTHONIOENCODING=utf-8 "/c/Users/liu/AppData/Local/Programs/Python/Python312/python.exe" -m unittest discover -s tests -p 'test_pro_access.py' -v`
Expected: 5 tests, OK（0 failures）

- [ ] **Step 3: 提交**

```bash
git add tests/test_pro_access.py
git commit -m "test: align pro_access tests with open-access contract"
```

---

### Task 2: 阶段0b — draw 测试更新（新额度/文案/模型名）

**Files:**
- Modify: `tests/test_draw_plugin.py`

**Interfaces:**
- Consumes: `draw_command.main.DrawCommand` 行为：开始文案统一含"（Imagen 3）"、`DRAW_DAILY = 3`、限额文案 `"作图次数已用完（今日 {used}/{limit}）。明天自动重置。"`、模型 `gemini-3-pro-image`
- Produces: 4 处断言更新

现状 4 失败：
1. `test_proxy_request_uses_current_vertex_image_model:95` — 期望 `gemini-3.1-flash-image`，WIP 改为 `gemini-3-pro-image`（契约=代码）
2. `test_ordinary_user_can_draw_with_the_daily_allowance:280` — 期望无"（Imagen 3）"文案，WIP 统一为含"（Imagen 3）"
3. `test_ordinary_user_uses_the_daily_cap:297` — 旧限额 1/1 + X 资格提示，新契约 3/3 + "明天自动重置"
4. `test_revoked_member_falls_back_to_ordinary_draw_allowance:261` — 同 2

- [ ] **Step 1: 更新 4 处断言**

```python
# test_proxy_request_uses_current_vertex_image_model (line 95):
        self.assertEqual(post.call_args.kwargs["json"]["model"], "gemini-3-pro-image")

# test_ordinary_user_can_draw_with_the_daily_allowance (line 280):
                self.assertEqual(replies[0], ("plain", "我开始画了（Imagen 3），预计 30–120 秒。"))

# test_ordinary_user_uses_the_daily_cap (lines 292-297):
                day = time.strftime("%Y%m%d")
                plugin._daily_usage[f"{sender}:{day}"] = 3
                plugin._request_image = lambda *_args: self.fail("quota should stop before proxy")

                replies = await collect(plugin.on_message(FakeEvent("/draw a cat", sender)))

                self.assertEqual(replies, [("plain", "作图次数已用完（今日 3/3）。明天自动重置。")])

# test_revoked_member_falls_back_to_ordinary_draw_allowance (line 261):
                self.assertEqual(replies[0], ("plain", "我开始画了（Imagen 3），预计 30–120 秒。"))
```

- [ ] **Step 2: 运行验证**

Run: `PYTHONIOENCODING=utf-8 "/c/Users/liu/AppData/Local/Programs/Python/Python312/python.exe" -m unittest discover -s tests -p 'test_draw_plugin.py'`
Expected: OK（0 failures，~15 tests）

- [ ] **Step 3: 提交**

```bash
git add tests/test_draw_plugin.py
git commit -m "test: align draw tests with unified quota and model naming"
```

---

### Task 3: 阶段0c — emotional_chat 测试更新（关系身份/舞台动作）

**Files:**
- Modify: `tests/test_emotional_chat.py`

**Interfaces:**
- Consumes: `emotional_chat.main` 行为：WIP 已删除硬编码"小柠是单身哦…"回复（隐私强化：不编现实身份，身份由人格 prompt 处理）；/talk 已删除舞台动作"（放下手边的事，认真听你说…）"（2026-07-19 人格强化）
- Produces: 4 处更新（其中两个关系测试改名）

现状 4 失败：
1. `test_relationship_identity_is_single_and_privacy_safe:80` — 期望回复含"单身"，WIP 无硬编码回复 → 改为断言无回复（身份交给人格层）
2. `test_talk_claims_the_event_and_returns_one_conversation:96` — 期望含舞台动作 → 删掉
3. `test_relationship_reply_never_contains_qq:153` — 同 1
4. `test_relationship_identity_is_single_and_privacy_safe` 同 1

- [ ] **Step 1: 更新 3 处（两个关系测试改名）**

```python
    def test_relationship_query_gets_no_hardcoded_identity_claim(self):
        async def scenario():
            plugin = EmotionalChat.__new__(EmotionalChat)
            replies = await collect(plugin.on_message(FakeEvent("小柠的对象是谁？", "2000000000")))
            # 隐私契约：不硬编码"单身"等现实身份，身份问题交给人格 prompt 处理
            self.assertEqual(replies, [])

        asyncio.run(scenario())

    def test_talk_claims_the_event_and_returns_one_conversation(self):
        async def scenario():
            plugin = EmotionalChat.__new__(EmotionalChat)
            event = FakeEvent("/talk 今天有点累")
            with patch(
                "emotional_chat.main.asyncio.to_thread",
                new=AsyncMock(return_value="哎，听着就挺累的。"),
            ):
                replies = await collect(plugin.on_message(event))
            self.assertTrue(event.stopped)
            self.assertEqual(
                replies,
                ["哎，听着就挺累的。"],  # 人格强化后无舞台动作
            )

        asyncio.run(scenario())

    def test_relationship_query_never_leaks_qq(self):
        async def scenario():
            plugin = EmotionalChat.__new__(EmotionalChat)
            replies = await collect(plugin.on_message(FakeEvent("小柠的对象是谁？", "2000000000")))
            self.assertEqual(replies, [])
            # 即使未来加了回复，也绝不允许泄露 QQ 号
            for reply in replies:
                self.assertNotIn("3424575956", str(reply))

        asyncio.run(scenario())
```

删除旧的 `test_relationship_identity_is_single_and_privacy_safe` 和 `test_relationship_reply_never_contains_qq`（被上面两个替代）。

- [ ] **Step 2: 运行验证**

Run: `PYTHONIOENCODING=utf-8 "/c/Users/liu/AppData/Local/Programs/Python/Python312/python.exe" -m unittest discover -s tests -p 'test_emotional_chat.py'`
Expected: OK（0 failures）

- [ ] **Step 3: 提交**

```bash
git add tests/test_emotional_chat.py
git commit -m "test: align emotional_chat tests with privacy-hardened persona"
```

---

### Task 4: 阶段0d — pdf_analysis 门禁残留清理（R1）+ new_feature/video 测试更新（R2）

**Files:**
- Modify: `astrbot/data/plugins/pdf_analysis/main.py`（R1：删残留门禁）
- Modify: `tests/test_new_feature_plugins.py`（pdf 段）
- Modify: `tests/test_video_command.py`（开放契约）

**Interfaces:**
- Consumes: 开放契约 — pdf 分析不再按 tier/限额/冷却门禁
- Produces: pdf 无未定义常量；new_feature 测试断言无拒绝；video 测试断言生成流程启动

现状：
- pdf WIP 删了 `REQUIRED_MSG`/`PRO_DAILY_LIMIT`/`PRO_MSG`/`COOLDOWN_SECONDS` 但门禁代码块（main.py:76-96）仍引用 → 运行时 NameError。删除整个门禁块。
- new_feature 测试 pdf 段期望 `REQUIRED_MSG` 拒绝 → 改断言无回复（无文件+群聊非 @ → 直接 return）
- video 测试 `VideoCommand.__new__` 缺 `_daily_usage`/`_cooldowns`/`_generation_lock` → AttributeError；且期望"X 或 Pro"拒绝 → 开放契约下改为生成流程启动

- [ ] **Step 1: 删除 pdf 门禁块（R1）**

`astrbot/data/plugins/pdf_analysis/main.py` 三处修改：

```python
# 1) 顶部 docstring（line 1）：
"""Document analysis — pypdf for text PDFs, Gemini vision fallback for scanned. Open access."""

# 2) 删 import：第 9 行 `import time` 删除；第 21-24 行整块删除：
try:
    from draw_command.pro_access import get_tier, is_active_pro_group, Tier
except ImportError:
    from data.plugins.draw_command.pro_access import get_tier, is_active_pro_group, Tier

# 3) __init__ 删两个属性（line 44-45）：
        self._cooldowns: dict[str, float] = {}
        self._daily_usage: dict[str, int] = {}

# 4) on_message 门禁块（line 76-96）整块删除：
        tier = get_tier(sender_id, self._pro_db)
        today = time.strftime("%Y%m%d")
        dk = f"{sender_id}:{today}"
        used = self._daily_usage.get(dk, 0)
        group_id = str(getattr(event, "get_group_id", lambda: "")() or "")
        in_pro_group = bool(group_id) and is_active_pro_group(group_id, self._pro_db)
        if tier < Tier.X and not in_pro_group:
            yield event.plain_result(REQUIRED_MSG)
            return
        limit = PRO_DAILY_LIMIT
        if used >= limit:
            yield event.plain_result(PRO_MSG.format(used=used, limit=limit))
            return

        now = time.time()
        last = self._cooldowns.get(sender_id, 0)
        if now - last < COOLDOWN_SECONDS:
            remain = int(COOLDOWN_SECONDS - (now - last))
            yield event.plain_result(f"请 {remain} 秒后再试。")
            return
        self._cooldowns[sender_id] = now
```

改后 `on_message` 流程：`if not files: return`（line 73-74）之后直接 `content = ""`（line 98）。

- [ ] **Step 2: 确认无残留引用**

Run: `grep -n "REQUIRED_MSG\|PRO_DAILY_LIMIT\|PRO_MSG\|COOLDOWN_SECONDS\|get_tier\|is_active_pro_group" astrbot/data/plugins/pdf_analysis/main.py`
Expected: 无输出

- [ ] **Step 3: 更新 new_feature 测试 pdf 段**

`tests/test_new_feature_plugins.py`（line 120-129）替换为：

```python
            pdf = pdf_module.PdfAnalysis.__new__(pdf_module.PdfAnalysis)
            pdf._pro_db = Path("unused.db")
            group_event = FakeEvent("/analysis summarize", origin="test:GroupMessage:12345678")
            # 开放契约：无文件+群聊非 @ 直接 return，不再有 tier 拒绝
            replies = await collect(pdf.on_message(group_event))
            self.assertEqual(replies, [])
```

- [ ] **Step 4: 更新 video 测试（开放契约）**

`tests/test_video_command.py` 的 `test_ordinary_users_can_search_but_cannot_generate`（line 270-303）整体替换为：

```python
    def test_ordinary_users_can_search_but_cannot_generate(self):
        class Event:
            is_at_or_wake_command = False

            def get_message_str(self):
                return "帮我生成一只猫的视频"

            def is_private_chat(self):
                return True

            def get_sender_id(self):
                return "123456789"

            def plain_result(self, text):
                return text

            def stop_event(self):
                self.stopped = True

        async def scenario():
            plugin = VideoCommand.__new__(VideoCommand)
            plugin._daily_usage = {}
            plugin._cooldowns = {}
            plugin._generation_lock = asyncio.Lock()
            event = Event()
            event.stopped = False
            with patch("data.plugins.video_command.main.mirror_runtime_task_status", new=AsyncMock()) as mirror:
                with patch("data.plugins.video_command.main.get_tier", return_value=Tier.ORDINARY):
                    replies = [reply async for reply in plugin.on_message(event)]
            # 开放契约：普通用户也能生成；无 tier 拒绝
            self.assertTrue(event.stopped)
            self.assertTrue(all("X 或 Pro" not in str(r) for r in replies))
            mirror.assert_awaited()
```

注意：`mirror_runtime_task_status` 是否已在测试文件顶部 import 过——若 on_message 中途还会调用代理/网络，先跑测试看是否还需要 patch 生成段内部的 `asyncio.to_thread`（视频生成走 `_call_veo`/代理），按实际错误逐个补 mock。

- [ ] **Step 5: 运行验证**

Run: `PYTHONIOENCODING=utf-8 "/c/Users/liu/AppData/Local/Programs/Python/Python312/python.exe" -m unittest discover -s tests -p 'test_new_feature_plugins.py' -p 'test_video_command.py'`（一次一个文件）
Expected: 两个文件各自 OK

- [ ] **Step 6: 提交**

```bash
git add astrbot/data/plugins/pdf_analysis/main.py tests/test_new_feature_plugins.py tests/test_video_command.py
git commit -m "fix: drop stale pdf gating leftovers; align tests with open access"
```

---

### Task 5: 阶段0e — agent 集成测试更新（开放契约）

**Files:**
- Modify: `tests/test_agent_integration.py`

**Interfaces:**
- Consumes: `claude_code_agent` 行为：非 owner 用户自然语言请求 → 澄清式追问（如"你是想让我查资料后直接给结论，还是做成 Word、PPT 这类文件发你？"），不再有"X 或 PRO"拒绝；X 成员可执行；群聊无 @ 仍永不执行
- Produces: 3 处测试更新（1 改名）

现状 3 失败：
1. `test_non_owner_and_group_without_real_at_never_execute:700` — 期望"X 或 PRO"拒绝 → 改为断言无拒绝 + executed 仍空
2. `test_ordinary_natural_agent_request_gets_pro_boundary_without_execution:723` — 期望"X 或 PRO" → 改为断言澄清追问 + executed 空
3. `test_pro_membership_alone_does_not_grant_trusted_host_execution:528` — 期望拒绝 → 改为断言 X 成员自然任务可执行

- [ ] **Step 1: 更新 3 处**

```python
# 1) test_non_owner_and_group_without_real_at_never_execute (line 700):
                self.assertFalse(any("X 或 PRO" in text for text in _plain_texts(ordinary)))
                # 开放契约：普通用户得到澄清追问而非 tier 拒绝，但仍不直接执行
                self.assertEqual(plugin.executed, [])
                self.assertTrue(any("还是做成" in text for text in _plain_texts(ordinary)))

# 2) test_ordinary_natural_agent_request_gets_pro_boundary_without_execution → 改名为
#    test_ordinary_natural_agent_request_gets_clarification_without_execution (line 723-726):
                self.assertEqual(plugin.executed, [])
                self.assertTrue(
                    any("还是做成" in text for text in _plain_texts(replies)),
                    _plain_texts(replies),
                )

# 3) test_pro_membership_alone_does_not_grant_trusted_host_execution → 改名为
#    test_x_member_can_execute_natural_task (line 528-529):
                # 开放契约：allowlist 成员（非 owner）可执行自然语言任务
                self.assertEqual(len(plugin.executed), 1)
```

- [ ] **Step 2: 运行验证**

Run: `PYTHONIOENCODING=utf-8 "/c/Users/liu/AppData/Local/Programs/Python/Python312/python.exe" -m unittest discover -s tests -p 'test_agent_integration.py'`
Expected: 无 FAIL/ERROR（除下面 Task 6 的两个顺序污染测试——先看是否还在）

- [ ] **Step 3: 提交**

```bash
git add tests/test_agent_integration.py
git commit -m "test: align agent tests with open-access execution contract"
```

---

### Task 6: 阶段0f — queue/backend-fallback 顺序污染修复（R1，HEAD 遗留）

**Files:**
- Modify: `tests/test_agent_integration.py` 或 `astrbot/data/plugins/claude_code_agent/*`（按诊断结果）

**Interfaces:**
- Consumes: `test_second_task_waits_in_bounded_queue_and_then_executes`（line 774）、`test_unhealthy_preferred_backend_falls_back_before_execution`（line 428）
- Produces: 全文件运行稳定

现状：两个测试**单跑通过、全文件跑失败** → 测试间状态泄漏（共享单例/全局 patch 未清理/模块级状态）。这是 HEAD 就存在的遗留问题。

- [ ] **Step 1: 复现 + 定位污染源**

Run: `PYTHONIOENCODING=utf-8 "/c/Users/liu/AppData/Local/Programs/Python/Python312/python.exe" -m unittest discover -s tests -p 'test_agent_integration.py'`
Expected: 能看到 queue 或 fallback 测试失败（记录失败时它前面的测试序号）

- [ ] **Step 2: 二分定位污染测试**

Run: `PYTHONIOENCODING=utf-8 "/c/Users/liu/AppData/Local/Programs/Python/Python312/python.exe" -m unittest discover -s tests -p 'test_agent_integration.py' -k <前一半测试名子串>`
逐步缩小到某个先行测试。常见污染源检查清单：
- `mirror_runtime_task_status` 被 patch 后未 restore（跨测试残留 AsyncMock）
- `draw_command.pro_access._clients` 缓存（其它测试文件不共享，但本文件内若有共享 DB 单例要查）
- `claude_code_agent` 模块级全局（如 `_queue`、`TaskRegistry`、`time.sleep` 被 patch）
- `unittest.mock.patch` 嵌套未退出

- [ ] **Step 3: 修复**

按污染源处理（二选一，修测试隔离优先）：
- 若是 patch 未清理 → 在污染测试的 `addCleanup` 中补 restore
- 若是模块级共享状态 → 在 `setUp` 里重置（参照 test_draw_plugin.py:73-75 的 `_clients.clear()` 模式）

- [ ] **Step 4: 全文件连续跑 2 次验证稳定**

Run: 同一命令连续两次
Expected: 两次均 OK，无顺序依赖

- [ ] **Step 5: 提交**

```bash
git add tests/test_agent_integration.py
git commit -m "fix: remove cross-test state pollution in agent integration tests"
```

---

### Task 7: 阶段0g — sing 本地实验正则修复（不提交）+ 全量门禁

**Files:**
- Modify: `astrbot/data/plugins/sing_command/main.py`（git-ignored，不提交）

**Interfaces:**
- Consumes: `tests/test_sing_command.py:51` 契约：`_SING_COVER.match("唱一首测试歌").group("query") == "测试歌"`；场景消息"小柠，唱一首测试歌"
- Produces: 本地 564 全绿

现状：`_SING_COVER` 只匹配 `/sing X`，自然语言"唱一首X"不匹配 → None。

- [ ] **Step 1: 扩展正则**

`astrbot/data/plugins/sing_command/main.py` line 17-20 替换：

```python
_SING_COVER = re.compile(
    r"^\s*(?:/sing\s+|(?:小柠[,，]?\s*)?唱(?:一首|个|一下)?)(?P<query>.+?)\s*$",
    re.I,
)
```

- [ ] **Step 2: 运行验证**

Run: `PYTHONIOENCODING=utf-8 "/c/Users/liu/AppData/Local/Programs/Python/Python312/python.exe" -m unittest discover -s tests -p 'test_sing_command.py'`
Expected: OK（1 test）

- [ ] **Step 3: 全量回归门禁（阶段 0 验收）**

Run: `PYTHONIOENCODING=utf-8 "/c/Users/liu/AppData/Local/Programs/Python/Python312/python.exe" -m unittest discover -s tests -p 'test_*.py'`
Expected: `Ran 564 tests`（或 564±新增）且 `OK`，0 failures 0 errors

- [ ] **Step 4: 不提交**（sing 在 .gitignore，`git status` 应无此文件）

---

### Task 8: 阶段1 — 主题提交 + 推送 + 合并 main + 删分支

**Files:** 95 个未提交文件 + 之前各 Task 的提交

**Interfaces:**
- Consumes: 阶段 0 完成（564 全绿）
- Produces: main 分支包含全部内容，旧分支删除

- [ ] **Step 1: 密钥扫描（push 前必做）**

Run: `git diff --cached HEAD | grep -aiE "token|secret|password|api[_-]?key|bearer" | grep -v "test_\|_conf_schema" | head -20`
Expected: 无真实密钥（只允许测试夹具/占位符）

- [ ] **Step 2: 按主题分组提交剩余文件**

Run: `git diff --name-only HEAD | sort`
按目录前缀归组（每组一个 commit，conventional commits）：
- g1 `astrbot_plugin_qqadmin/` → `feat: harden group moderation via ai moderation handler`
- g2 `astrbot_plugin_proactive_chat/` → `feat: rework proactive care scheduling and session config`
- g3 `astrbot_plugin_xiaoning_memory/` + `astrbot_plugin_context_aware/` → `feat: scope-aware memory extraction and recall`
- g4 `draw_command/` + `video_*/` + `astrbot_plugin_meme_manager/` + `douyin_source/` + `music_command/` → `feat: unify media pipelines and open access`
- g5 `friend_core/` + `emotional_chat/` + `chat_router/` → `feat: privacy-hardened persona and router`
- g6 `claude_code_agent/` + `contact_pro_info/` + `search_command/` + `deep_think/` + `ai_*/` → `feat: agent and tooling improvements`
- g7 `tests/` → `test: sync suite with open-access contract`
- g8 `.gitignore` + `xiaoning.plugins.json` + 删除 `group_brief/`、`temp_broadcast/` → `chore: prune retired plugins and metadata`
其余散文件（`like_back`、`link_summary`、`pdf_analysis`、`pro_application`、`smart_translate`、`voice_model_router`、`welcome_card`、`xiaoning_runtime*` 等）就近并入最相关组。提交信息用 `git add <files> && git commit -m "..."`。

- [ ] **Step 3: 检查 plugin_set 残留**

Run: `grep -a "group_brief\|temp_broadcast" astrbot/data/cmd_config.json`
若有 → 从 `plugin_set` 移除这两个名字（文件是 UTF-8 BOM，用 Python 处理）：

```python
# 用 PYTHONIOENCODING=utf-8 python 执行
import json
p = r"astrbot/data/cmd_config.json"
raw = open(p, "rb").read().decode("utf-8-sig")
data = json.loads(raw)
data["plugin_set"] = [n for n in data["plugin_set"] if n not in ("group_brief", "temp_broadcast")]
open(p, "wb").write(b"\xef\xbb\xbf" + json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
```
加入 g8 提交。注意：cmd_config.json 在 .gitignore（line 62），改完只留在本地，不提交。

- [ ] **Step 4: 推送 + 合并 main**

```bash
git push origin codex/sync-claudecode-codex
git checkout main
git pull origin main
git merge codex/sync-claudecode-codex --no-ff -m "merge: sync claudecode/codex improvements into main"
git push origin main
```

- [ ] **Step 5: 删旧分支**

```bash
git push origin --delete codex/sync-claudecode-codex codex/public-deploy
git branch -D codex/sync-claudecode-codex codex/public-deploy
```

- [ ] **Step 6: 验证**

Run: `git fetch --prune && git branch -a && git log --oneline -3 origin/main`
Expected: 只剩 `main`，origin/main 含全部提交

---

### Task 9: 阶段2 — GitHub Actions CI

**Files:**
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: 仓库全部测试（Py3.12 + astrbot==4.26.5）
- Produces: PR/push 到 main 的自动测试 + lint 门禁

- [ ] **Step 1: 写 workflow**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install AstrBot
        run: pip install astrbot==4.26.5
      - name: Run tests
        env:
          PYTHONIOENCODING: utf-8
        run: python -m unittest discover -s tests -p "test_*.py"

  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install ruff
        run: pip install ruff
      - name: Ruff fatal rules only
        run: ruff check astrbot/data/plugins --select E9,F63,F7,F82
```

- [ ] **Step 2: 本地预演测试 job**

Run: `PYTHONIOENCODING=utf-8 "/c/Users/liu/AppData/Local/Programs/Python/Python312/python.exe" -m unittest discover -s tests -p 'test_*.py' 2>&1 | tail -3`
Expected: OK —— 但**注意**：本地有 4 个服务在跑，CI 没有。CI 里依赖端口 3000/8766/6199 的测试会失败。Step 3 处理。

- [ ] **Step 3: 服务依赖测试加 skip 守卫**

先找哪些测试依赖本机服务：

Run: `grep -l "127.0.0.1:3000\|127.0.0.1:8766\|127.0.0.1:6199\|get_using_tts\|local_tts" tests/*.py`
对命中的测试文件，在文件顶部加端口探测装饰器（创建 `tests/_ci_guards.py`）：

```python
"""CI guard helpers — skip tests that need local services (absent on runners)."""
import socket
import unittest
from functools import wraps


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def skip_if_no_service(port: int):
    def decorator(test):
        @wraps(test)
        def wrapper(self):
            if not port_open(port):
                self.skipTest(f"local service on :{port} not running")
            return test(self)
        return wrapper
    return decorator
```

在命中文件的测试类方法上按需加 `@skip_if_no_service(3000)` 等。**在本地（服务在跑）跳过逻辑不生效**，行为不变。

- [ ] **Step 4: ruff 本地验证**

Run: `"/c/Users/liu/AppData/Local/Programs/Python/Python312/python.exe" -m ruff check astrbot/data/plugins --select E9,F63,F7,F82 2>&1 | tail -5`
Expected: 0 errors（若有真错误——未定义名/未用变量——顺手修掉，这是 CI 要抓的）

- [ ] **Step 5: README 加徽章 + 提交**

```markdown
<!-- README.md 顶部标题下加一行 -->
![CI](https://github.com/tomerose/qqbot-private-backup/actions/workflows/ci.yml/badge.svg)
```

```bash
git add .github/workflows/ci.yml tests/_ci_guards.py README.md
git commit -m "ci: add GitHub Actions test and lint gates"
git push origin main
```

- [ ] **Step 6: 验证 CI 绿**

Run: `gh run list --limit 3` 然后 `gh run view <run_id> --log` 看结果
Expected: 两个 job 全绿（有失败按日志修，直到绿）

---

### Task 10: 阶段3 — 仓库规范

**Files:**
- Create: `CONTRIBUTING.md`、`.github/ISSUE_TEMPLATE.md`、`.github/PULL_REQUEST_TEMPLATE.md`
- Modify: `README.md`（目录结构 + 架构概览）

**Interfaces:**
- Produces: 仓库对外规范文档

- [ ] **Step 1: 写 CONTRIBUTING.md**

```markdown
# 贡献指南

## 环境
- Python 3.12（PATH 默认 python 是 3.10，请用 3.12 解释器跑测试）
- `pip install astrbot==4.26.5`（与线上一致）

## 跑测试
```powershell
python -m unittest discover -s tests -p "test_*.py"
```
全部通过再提 PR。新增插件必须同时注册到 `astrbot/data/cmd_config.json` 的 `plugin_set`（UTF-8 BOM），否则消息处理器不会被调用。

## 权限契约（重要）
- 2026-08-04 起为全员 X 开放：`draw_command/pro_access.py` 的 `get_tier` 恒返回 X。
- 群聊无真实 @ 永不执行本机任务（安全边界，禁止放宽）。
- 密钥/路径/私聊数据禁止进 Git。

## PR 要求
- conventional commits（feat/fix/test/docs/ci/chore）
- 相关测试随改动更新
- CI 必须绿
```

- [ ] **Step 2: 写 issue/PR 模板**

`.github/ISSUE_TEMPLATE.md`：

```markdown
### 问题描述
### 复现步骤
1.
2.
### 期望行为
### 实际行为
### 环境（QQ 版本 / AstrBot 版本 / 触发插件）
```

`.github/PULL_REQUEST_TEMPLATE.md`：

```markdown
## 改动内容
## 测试
- [ ] 本地全量 unittest 通过
- [ ] 相关插件测试更新
- [ ] 无密钥/路径泄漏
## 关联 issue
```

- [ ] **Step 3: README 补结构 + 架构概览**

在 `README.md` 的 `## 一键初始化` 前插入：

```markdown
## 结构概览

```
astrbot/data/plugins/     # 全部 AstrBot 插件（40+）
├─ chat_router/           # 消息路由（模型分级）
├─ friend_core/           # 人格注入 + 主动关怀
├─ draw_command/          # 绘图（Vertex Imagen）+ 会员访问
├─ video_command/         # AI 视频生成/搜索
├─ claude_code_agent/     # 本机 Agent 执行（安全边界）
└─ ...                    # 其余见各插件 README
gemini-proxy.py           # Vertex Gemini 代理（127.0.0.1:3000）
services/local_tts/       # 本地 TTS（127.0.0.1:8766）
tests/                    # 回归测试（Py3.12，564+）
docs/superpowers/         # 设计规格与实施计划
```

## 架构概览

消息流：NapCat(QQ) → AstrBot(6185/6199) → 插件链 → chat_router 分流 → Gemini 代理(3000) / DeepSeek / 本地 TTS(8766)。本地 Agent 执行受 `findings.md` 所述安全边界约束（审批码、目录隔离、脱敏、审计）。
```

- [ ] **Step 4: 版本标签**

```bash
git tag v0.1.0
git push origin v0.1.0
```

- [ ] **Step 5: 提交**

```bash
git add CONTRIBUTING.md .github/ISSUE_TEMPLATE.md .github/PULL_REQUEST_TEMPLATE.md README.md
git commit -m "docs: add contributing guide, issue/PR templates, structure overview"
git push origin main
```

---

### Task 11: 阶段4 — 谷歌生态深化审查清单

**Files:**
- Create: `docs/google-ecosystem-roadmap.md`

**Interfaces:**
- Consumes: gemini-proxy.py、模型路由、Firestore 记忆、grounding 现状
- Produces: 优先级排序的深化清单（本轮不实施）

- [ ] **Step 1: 审查 gemini-proxy.py**

Run: `grep -n "model\|thinking\|grounding\|finish_reason\|retry\|rate" gemini-proxy.py | head -30`
记录：当前支持的模型列表、thinking 路径、重试逻辑、安全拦截（finish_reason 感知）、限速。特别核对 Task 2 中 `gemini-3-pro-image` 是否在 Vertex 支持清单（对照 `google-cloud-aiplatform` 模型端点或代理内模型映射表）。

- [ ] **Step 2: 审查模型路由**

Run: `grep -rn "gemini\|deepseek\|model" astrbot/data/plugins/chat_router/main.py | head -20`
记录：tier→模型映射（开放契约后 X=全员默认 flash/pro 路由是否还合理）。

- [ ] **Step 3: 审查 Firestore/记忆 + grounding**

Run: `grep -rln "firestore\|firebase\|google_search\|grounding" astrbot/data/plugins/ | head -10`
逐个记录用法与缺口（embedding 配额、搜索落地、记忆写入频率）。

- [ ] **Step 4: 写清单**

`docs/google-ecosystem-roadmap.md`，每条格式：

```markdown
## N. <标题>
- 现状：<从代码/日志看到的实际行为>
- 影响：<用户可感知收益>
- 工作量：S/M/L
- 优先级：P0/P1/P2
- 风险：<模型名/配额/隐私>
```

- [ ] **Step 5: 提交**

```bash
git add docs/google-ecosystem-roadmap.md
git commit -m "docs: add google ecosystem deepening roadmap"
git push origin main
```

---

### Task 12: 阶段收尾 — 服务重启验证 + 最终验收

**Files:** 无

**Interfaces:**
- Consumes: 全部阶段产物
- Produces: 验收报告

- [ ] **Step 1: 重启 4 服务（需用户确认，会中断在线 bot）**

按 `qqbot-services-startup` memory 顺序：3000 → 8766 → 6185/6199 → NapCat WS ESTABLISHED。先征求用户同意再执行（或用户自己跑 `start_all_services.bat`）。

- [ ] **Step 2: 运行态验证**

Run: `netstat -ano | grep -E ":(3000|8766|6185|6199)\s.*LISTENING"`
Expected: 4 个 LISTENING

- [ ] **Step 3: 最终验收清单核对**

- [ ] 564+ 测试全绿（Task 7 已验）
- [ ] CI 两 job 绿（Task 9）
- [ ] main 合并完成、分支清理（Task 8）
- [ ] v0.1.0 标签（Task 10）
- [ ] 谷歌清单产出（Task 11）
- [ ] 4 服务重启后正常

- [ ] **Step 4: 汇报**

向用户输出最终报告：修复了什么（R1/R2 分类）、开放契约落地范围、CI/规范新增、谷歌清单要点、剩余风险。

---

## Self-Review

**Spec 覆盖核对：**
- 阶段 0（19 测试修复）→ Task 1-7 ✓（pro_access 7 / draw 4 / emotional 4 / pdf+new_feature+video 4 / agent 3 / queue+fallback 2 顺序污染 / sing 1 本地）
- 阶段 1（8 组提交+合并+删分支）→ Task 8 ✓
- 阶段 2（CI）→ Task 9 ✓
- 阶段 3（README/CONTRIBUTING/模板/标签）→ Task 10 ✓
- 阶段 4（谷歌清单）→ Task 11 ✓
- 收尾验收 → Task 12 ✓
- plugin_set 残留清理 → Task 8 Step 3 ✓
- push 前密钥扫描 → Task 8 Step 1 ✓

**占位符扫描：** Task 6 的修复代码按污染源二分定位后写（无法静态预知是哪个测试泄漏状态，已给出定位方法+两类修复模式+检查清单）。Task 9 Step 3 的服务依赖测试清单需运行 `grep` 确定（已给命令+守卫代码）。Task 4 Step 4 的 video mock 按实际错误补（已给主 patch + 兜底说明）。均为调查性步骤，非空洞占位。

**类型/命名一致性：** `_SING_COVER` 命名与现有代码一致；`skip_if_no_service` 仅新文件内使用；`mirror_runtime_task_status` 为 video_command 现有 import（已确认存在于 main.py）；`DRAW_DAILY=3`、`gemini-3-pro-image` 均以 WIP 代码为准。
