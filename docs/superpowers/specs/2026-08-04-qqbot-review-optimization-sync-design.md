# 小柠 QQBot 审查·优化·GitHub 同步 — 设计规格

日期：2026-08-04
状态：已获用户分节批准

## 背景与现状

- 4 服务全部运行（Gemini Proxy:3000 / Local TTS:8766 / AstrBot:6185+6199 / NapCat WS 已连接）。
- Git：分支 `codex/sync-claudecode-codex`，领先 origin 5 commits，**95 文件未提交**（+2411/−1916）。
- 测试（Python 3.12）：564 跑，19 破（15 fail + 4 error）。
  - HEAD 基线（stash 对比验证）：只坏 2 个（`test_second_task_waits_in_bounded_queue_and_then_executes`、`test_unhealthy_preferred_backend_falls_back_before_execution`，均在 agent_integration）。
  - 其余 ~17 个为 95 文件 WIP 引入的回归。
- 无 `.github/`（无 CI）。
- 环境坑：PATH 默认 `python` 是 3.10（无 astrbot 包），测试必须用 Python 3.12（`C:\Users\liu\AppData\Local\Programs\Python\Python312\python.exe`）。
- 仓库 `tomerose/qqbot-private-backup` 为 **PRIVATE**。

## 目标

1. 修复全部测试失败，恢复可验证的健康基线。
2. 按主题分组提交 95 文件 WIP，推送、合并 main、清理旧分支。
3. 建立 GitHub Actions CI 与仓库规范（README/CONTRIBUTING/模板/版本标签）。
4. 按谷歌生态（服务接入 + 工程实践）与 GitHub 优秀项目标准优化，谷歌深化本轮只出清单不实施。

## 阶段 0：回归修复（先决门禁）

原则：**测试 = 规格**。每个失败判定根因两类：
- R1 WIP 改坏了代码 → 修代码。
- R2 WIP 改了接口/行为，测试未跟上 → 修测试（仅在契约合理时）。

分组修复（同根因一起修）：

| 组 | 失败 | 说明 |
|----|------|------|
| ① | pro_access ×7 | 成员签名/DB 逻辑 vs 测试冲突，查 WIP diff 定 R1/R2 |
| ② | draw_command ×4 | 每日额度/上限/vertex 模型 |
| ③ | emotional_chat ×4 | relationship identity |
| ④ | agent_integration ×3 | 2 个 HEAD 遗留（queue/backend fallback）+ 1 个 WIP |
| ⑤ | ERROR ×4 | sing/new_feature/video/emotional — 查 import 还是 runtime |

约束：
- 最小 diff，修完跑该文件 → 再全量 564。
- 不改变运行中 bot 的对外行为，除非测试契约要求。
- 修复后重启 4 服务验证（3000 → 8766 → 6185/6199 → NapCat WS ESTABLISHED）。

验收：Py3.12 全量 564 测试 **0 失败**。

## 阶段 1：Git 提交与分支流程

主题分组提交（约 8 组），conventional commits：

| 组 | 内容 |
|----|------|
| g1 | qqadmin/群管理审核 |
| g2 | proactive_chat 主动关怀 |
| g3 | xiaoning_memory 记忆 |
| g4 | draw/video/media 媒体链路 |
| g5 | friend_core/emotional/persona 人格 |
| g6 | claude_code_agent agent |
| g7 | tests |
| g8 | .gitignore/元数据 |

删除确认：`group_brief`、`temp_broadcast` 已删 — 检查 `astrbot/data/cmd_config.json` `plugin_set` 是否有残留引用，有则同步清理（否则加载报错）。

分支流程：
1. 推送 `codex/sync-claudecode-codex`。
2. 合并到 `main`，推送 `main`。
3. 删除 `origin/codex/public-deploy` + `origin/codex/sync-claudecode-codex`。
4. 历史保留，不重写。

push 前：`git diff --stat` + 扫描无密钥/路径泄漏（token/key/secret 模式）。

## 阶段 2：CI（GitHub Actions）

`.github/workflows/ci.yml`：
- 触发：push + PR → main。
- job1：windows-latest + Python 3.12 → `pip install astrbot==4.26.5` → `unittest discover -s tests` 全量。
  - 若测试依赖本机服务（3000/8766），加 skip 守卫（端口检测），CI 不因无服务红。
- job2：ruff — 先只开致命规则 `E9,F63,F7,F82`（真正错误），全量风格 lint 日后扩展。
  - 理由：40+ 插件全量 ruff 千条告警，CI 永红没意义。
- README 加 CI 徽章。

## 阶段 3：仓库规范

- README 补目录结构树 + 架构概览（保留现有安装说明）。
- CONTRIBUTING.md：Python 3.12、跑测试命令、PR 要求。
- issue_template.md + pull_request_template.md。
- 版本标签 v0.1.0（修复后打）。

## 阶段 4：谷歌深化（只审查出清单，本轮不实施）

审查范围：
- gemini-proxy.py：模型路由、thinking、grounding、finish_reason、重试、限速。
- Firestore 记忆 / embedding 用法。
- Google Search grounding（群关怀）。
- AstrBot 模型 Provider 路由现状。

产出 `docs/google-ecosystem-roadmap.md`：每条标影响 / 工作量 / 优先级。用户点单后下一轮实施。

## 错误处理与验收总纲

- 阶段 0 前已 stash 快照验证过可回滚，工作区随时可恢复。
- push 前扫描无密钥泄漏。
- 最终验收清单：
  1. 564 测试绿（Py3.12）。
  2. 4 服务重启验证正常。
  3. CI 绿（jobs 通过）。
  4. main 合并完成，分支清理完成。
  5. 版本标签 v0.1.0 已打。
  6. 谷歌深化清单文档已产出。

## 范围外

- 不重写 git 历史。
- 不改 AstrBot/Codex/WorkBuddy 全局配置。
- 谷歌服务接入不在本轮实施（只出清单）。
