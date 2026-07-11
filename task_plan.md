# 小柠 Agent 安全与可靠性升级

## 目标
在不暴露本机路径、密钥、私人文件和聊天数据的前提下，让 QQ Agent 能可靠执行并证明任务完成。

## 成功标准
- 只有 QQ `1211000567` 可启动、审批和取消本机任务；群聊仍要求真实 @。
- 删除、外发、安装、系统/权限/凭据相关任务必须二次确认，确认一次性且限时。
- 回复、错误、审计记录和交付物均不泄露绝对路径、密钥、令牌或敏感文件。
- 任务状态持久化；重启后运行中任务标记为中断，不伪报完成。
- 结果需要退出码与交付物校验；回归测试全部通过，QQ 重新连接。

## 阶段
- [complete] 1. 威胁模型与现有边界审计
- [complete] 2. 风险分类、一次性审批与隐私脱敏（测试先行）
- [complete] 3. SQLite 任务账本与重启恢复（测试先行）
- [complete] 4. 退出码验收与安全交付物过滤
- [complete] 5. 全量验证、重启和运行态检查

## 范围外
- 本轮不接入 Gmail/Calendar，不触发 OAuth 或对外发送。
- 本轮不开放其他 QQ 用户权限。
- 不改 Claude Code、Codex、WorkBuddy 的全局配置。

## 错误记录
- 当前目录不是 Git 仓库，无法创建分支或提交；保留最小补丁和完整测试证据。
- 2026-07-11：组合读取与文件枚举命令因 `rg` 的返回码 1 被工具标记失败，但前置文件内容和清单已成功输出；后续改用限定目录的精确检索，不重复原命令。
- 2026-07-11：运行态综合查询在 `Win32_Process` 枚举阶段超时；此前代码与配置输出已取得，设计阶段不重复该重查询，最终实施验证改用已知 PID/端口的定向检查。
- 2026-07-11：首次 TTS 核心检索包含不存在的 `astrbot/astrbot` 路径，其他目标正常返回；已确认当前 AstrBot 安装仅保留数据目录，后续以已安装插件 API 用法为准。
- 2026-07-11：Python 依赖探测在检查不存在的 `google.cloud` 父包时抛出 `ModuleNotFoundError`，但已先确认 `edge_tts=True`；不重复该检查，Google Cloud TTS 不作为当前落地依赖。
- 2026-07-11：首轮 GitHub 检索的 `gh --jq` 表达式被 PowerShell 误解析为函数，且自然语言仓库搜索过窄返回空；GitHub 登录本身正常。改用 `gh api`/`ConvertFrom-Json` 获取已知候选的结构化元数据。
- 2026-07-11：Task 1 首次 RED 使用 `python -m unittest tests.test_*`，因 `tests` 不是 package 导致导入错误，未证明功能缺失；改用 `unittest discover -s tests -p <file>`，并同步修正后续实施命令。
- 2026-07-11：Task 4 首次恢复 RED 的测试夹具使用非 12 位十六进制 job ID，被既有目录安全校验正确拒绝；已改为合法测试编号，再定位缺失恢复方法。
- 2026-07-11：Task 4 恢复夹具暴露 DPAPI 相对工作区校验不接受恢复根标识 `.`；该标识符合规格，已转为独立 RED 测试后修复，不在集成测试中绕过。
- 2026-07-11：Task 4 恢复未执行的根因是把仅接受普通文件的 `is_within_allowed_roots` 用于目录；新增专用目录边界校验，要求真实目录、拒绝符号链接并在 resolve 后保持根内。一次诊断脚本还因中文绝对路径传入 stdin 导入失败，改用相对 `tests` 路径后取得数据库证据。
- 2026-07-11：MeloTTS 隔离安装成功后，启动脚本因当前 .NET 不支持静态 `RandomNumberGenerator.GetBytes(32)` 失败；依赖和安装标记均完整，改为实例式 CSPRNG 填充 32 字节，不重复安装。
- 2026-07-11：当前 Windows PowerShell 的 .NET 同样缺少 `Convert.ToHexString`；改用 `BitConverter.ToString(...)-replace '-'` 进行十六进制编码，CSPRNG 随机字节不变。
- 2026-07-11：首次 TTS HTTP smoke 返回 422 `request` query 缺失，定位为 `from __future__ import annotations` 下局部 Pydantic 模型无法被 FastAPI 正确解析；补 HTTP 失败测试后把 schema 移到模块级。原 smoke 未设置错误即停，产生了空路径/ffprobe 连带噪声，后续改为 `$ErrorActionPreference='Stop'`。

## 新阶段：自然语言 Agent + 任务恢复 + 完整语音
- [complete] 6. 核对现有入口、任务账本、语音路由和运行配置
- [complete] 7. 完成方案澄清与设计确认
- [complete] 8. 写入规格并由用户复核
- [in_progress] 9. 制定实施计划并按测试驱动执行（仅剩 owner 实际 QQ 文件回传验收）
- [complete] 10. 完成普通/Pro 权限、恢复幂等、DLP、日志 ACL、有限输出和语音清理的代码与运行态验收
- [pending] 11. 由真实 QQ 完成 Pro 文件回传、普通账号拒绝、高风险仅提示确认的三项对照验收
- [complete] 12. 部署任务执行内核：计划、逐步策略、路由、独立验证、步骤恢复和隔离门禁
- [in_progress] 13. 真实 QQ 验收任务执行内核；通过后实施小柠体验层
