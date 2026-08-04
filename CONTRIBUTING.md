# 贡献指南

## 环境

- Python 3.12（PATH 默认 `python` 是 3.10，无 astrbot 包——请用 3.12 解释器跑测试）
- `pip install astrbot==4.26.5`（与线上版本一致）

## 跑测试

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

全部通过再提 PR。新增插件必须同时注册到 `astrbot/data/cmd_config.json` 的 `plugin_set`（文件是 UTF-8 BOM），否则消息处理器不会被调用。

## 权限契约（重要）

- 2026-08-04 起为全员 X 开放：`draw_command/pro_access.py` 的 `get_tier` 恒返回 X，`use_agent` 无限。
- 群聊无真实 @ 永不执行本机任务（安全边界，禁止放宽）。
- 高风险/隐私任务仍需一次性审批码、目录隔离、脱敏与审计（见 `findings.md` 威胁模型）。
- 密钥、绝对路径、私聊数据禁止进 Git。

## 提交与 PR 要求

- conventional commits（feat/fix/test/docs/ci/chore）。
- 相关测试随改动更新；改动行为时先改测试（测试=规格）。
- CI 必须绿（GitHub Actions 跑全量测试 + ruff 致命规则）。
- 提 PR 前自查：`git diff` 无密钥/路径泄漏。

## 仓库结构

```
astrbot/data/plugins/     # 全部 AstrBot 插件（40+）
gemini-proxy.py           # Vertex Gemini 代理（127.0.0.1:3000）
services/local_tts/       # 本地 TTS（127.0.0.1:8766）
tests/                    # 回归测试（Py3.12，560+）
docs/superpowers/         # 设计规格与实施计划
scripts/                  # 运维脚本（RAG 维护等）
```
