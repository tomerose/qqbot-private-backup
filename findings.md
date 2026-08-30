# Findings

## 现状
- 本机 Agent 入口仅允许 QQ `<configured-qq-id>`，群聊要求明确 @。
- Claude/Codex/WorkBuddy 当前都以完整权限参数启动；这是最高风险边界。
- 现有保护包括任务长度、单任务、超时、取消、输出目录限制、文件大小/数量限制和路径脱敏。
- 当前缺少持久化任务账本、一次性风险审批、秘密值脱敏与敏感交付物过滤。

## 威胁模型
- 身份冒用：QQ 账号或群消息伪造 -> 精确 sender ID + 群内真实 @ + 审批绑定所有者。
- 提示注入：网页/文档诱导 Agent 越权 -> 高风险动作代码级审批，提示词不作为唯一边界。
- 信息泄露：路径、密钥、浏览器凭据、私聊数据进入回复/文件/日志 -> 多层脱敏 + 敏感文件拒发。
- 抵赖与误报：Agent 口头声称完成 -> 状态账本、退出码和交付物校验。
- 资源滥用：超长任务/输出/并发 -> 保留长度、超时、输出与单任务限制。

## 已实施边界
- 高风险任务必须使用 5 分钟一次性确认码，且绑定所有者、当前会话、后端和工作目录。
- 审计库仅保存哈希、状态、后端、风险类别、退出码与交付物数量。
- 本地图片/文件必须位于任务工作区或当前会话工作区；敏感文件类型和名称拒绝发送。
- 文本在回复和错误日志前执行路径、Bearer、API Key、Token、Password、Secret 脱敏。
- 进程退出码 0 只表述为“执行结束”，不把模型自述当作语义完成证明。
- 审计目录及数据库 ACL 已收紧，仅当前用户、SYSTEM、Administrators 可访问。

## 残余风险
- Claude/WorkBuddy/Codex 仍是通用本机 Agent；高风险分类可拦截显式请求，但无法数学保证识别所有隐晦表达。
- 提示注入边界已加入执行提示，真正的强隔离仍需后续为默认任务引入 OS/容器级沙箱。

## 2026-07-11 三项升级初查
- 用户选择第 2、3、5 项：自然语言 Agent、任务进度/断线恢复、完整语音聊天。
- 现有 `claude_code_agent` 已有 owner/群聊真实 @、工作目录、审批、取消、状态和 SQLite 元数据账本，可作为安全执行底座。
- 仓库已有 `tests/test_voice_router.py`、`tests/test_gemini_audio.py` 和 Gemini 代理，说明语音输入路由已有实现基础；需核对输出 TTS 是否真正闭环。
- 设计原则：自然语言入口只能调用现有安全执行接口，不能另开绕过审批或目录限制的新执行路径。
- 当前任务账本只保存哈希和有限元数据；启动时会把 `running` 改成 `interrupted`，没有任务原文，因此从隐私边界上不能自动续跑原任务，只能提供安全的“中断状态 + 用户重新确认后重试”。
- 当前语音路由仅在收到 `Record` 时把 provider 切到 `gemini-2.5-flash`；文本仍走 `deepseek-chat`。现有测试只覆盖语音识别输入和 Gemini 音频能力声明，尚未覆盖 QQ 语音回复。
- 私人伴侣插件的 TTS 动作和自动语音当前均关闭，`tts_conversion_provider_id` 为空；不能据此宣称已有完整语音闭环。
- AstrBot 全局 `provider_tts_settings.enable=false` 且 `provider_id` 为空；已安装插件中没有独立 TTS 插件。现有主动消息代码证明可通过 `get_using_tts_provider().get_audio()` 后发送 `Record`，但必须先接入实际 TTS provider。
- 本轮应保持 DeepSeek 文本默认、Gemini Flash 处理语音输入；输出语音只在用户明确提出“语音说/发语音”时启用，失败时回落为脱敏文字。
- 用户明确要求 Agent 必须直接完成任务，而不是只给步骤或口头声称完成；最终设计需要执行、校验、交付三个闭环，并在断线后继续低风险任务。
- 当前机器已安装 `edge_tts` 和 FFmpeg，可低成本生成 QQ 可发送音频；本机 SAPI 未枚举出可用声音。Edge TTS 会把最终待朗读文本发送给 Microsoft，因此是否启用取决于用户对语音隐私边界的确认。

## GitHub 本地 TTS 调研结论
- 当前机器实测：Intel Core Ultra 5 225H、31.5 GB RAM、Intel Arc 130T，无 NVIDIA CUDA；已装 ONNX Runtime 和 FFmpeg。
- `QwenLM/Qwen3-TTS` 功能上限最高：0.6B/1.7B、中文、流式、预设女声、VoiceDesign/VoiceClone；但官方本地示例以 CUDA + FlashAttention 2 为主，不适合当前机器作为稳定常驻默认引擎。
- `RVC-Boss/GPT-SoVITS` 支持中文、5 秒零样本、Windows 与 CPU；官方仓库链接的 `baicai-1145/GPT-SoVITS-CPUFast` 专门优化 CPU，中文端到端基准从约 15.1 秒降到约 8.3 秒，适合“按要求才发语音”的非实时路径。
- `myshell-ai/MeloTTS` 明确支持中文夹英文、CPU 实时推理、MIT；适合作为快速本地降级，但人格音色与情绪控制弱于 GPT-SoVITS。
- `thewh1teagle/kokoro-onnx` 轻量（约 300 MB，量化约 80 MB）且 ONNX 适合当前硬件；但官方 Kokoro voice card 给普通话 4 女 4 男的综合等级均为 D，不符合“小柠高质量人格女声”的主引擎要求。
- 推荐：GPT-SoVITS-CPUFast 作为本地人格主声线 + MeloTTS 作为本地快速备用；禁止未经授权模仿真人，参考音频必须是用户自有、授权或合成声源。

## 实施发现
- `tests` 目录不是 Python package，定向测试必须使用 `python -m unittest discover -s tests -p '<pattern>' -v`。
- 自然语言路由采用显式“帮我/请你/麻烦你”动作前缀，知识问答和“能不能”类含糊问题不触发执行；群聊仍检查真实 `At` 组件。
- 自动恢复必须比普通风险分类更保守：允许读取、分析、测试和生成新输出；修改、修复、删除、安装、登录、提交、上传和对外发送一律暂停。
- DPAPI 载荷使用 `XNJ1` 版本头和 job-id 派生 additional entropy；目录/文件 ACL 每次写入后重新收紧，解密或 JSON 校验失败只返回固定通用错误。
- Windows PowerShell 5.1 的 `Invoke-RestMethod` 会把 FastAPI JSON 中的中文路径误按单字节编码解析，不能作为本链路的路径边界验收器；实际插件用 Python 明确按 UTF-8 解码并能正确验证私有目录。
- `mecab-python3` 会优先选择已安装但未下载词典的 `unidic`，即使 `unidic_lite` 完整存在；服务加载前必须把 `unidic.DICDIR` 指向 `unidic_lite.DICDIR`。
- MeloTTS 导入中文模型时仍会初始化 `g2p_en`，因此需要 `averaged_perceptron_tagger`。资源固定在服务私有 `nltk_data`，启动脚本对官方文件做 SHA-256 校验，避免依赖损坏的全局缓存和运行时下载。
- AstrBot 4.26.5 的真实 `AiocqhttpMessageEvent` 使用 `message_str/get_message_str`，测试夹具不能自造 `get_message_text` 掩盖接口漂移；语音路由已按真实事件 API 回归。
- 恢复态群文件不能使用 `context.send_message(File)`；必须从 unified message origin 解析平台实例与群号，继续调用 OneBot `upload_group_file`，否则会重现“文件生成但群里收不到”。

## 2026-07-11 普通版 / Pro 最终复核
- 普通用户仅保留聊天、图片和明确请求后的语音；本机 Agent、文件、状态、取消、恢复均由不可通过聊天修改的 Pro allowlist 控制，当前仅 QQ `<configured-qq-id>`。
- 高影响与未知动作必须在同一会话内一次性确认；自动恢复仅允许严格只读，交付摘要先提交后发送，重启不会重复执行已产出任务。
- 所有本机交付先做根目录、符号链接、文件名、内容、归档和图片元数据检查；PNG 文本块与 EXIF 均会阻止未经清理的图片外发。
- 运行日志只保留固定错误码、随机任务 ID、后端名与异常类型，不保存任务正文、提供商错误正文、路径、令牌或文件内容。
- 当前证据：123 项测试通过，目标配置 JSON 可解析，插件 handler 注册成功，ACL 不安全项 0，外部监听 0，旧 8192 端口 0，OneBot 已连接，本地 TTS WAV 生成与清理成功。

## 当前残余风险
- Claude/WorkBuddy/Codex 仍是通用本机 Agent；分类、审批和提示词无法替代 OS/容器级强沙箱。
- 隐私优先的 DLP 当前放行受控文本、代码、图片和 ZIP/Office 容器；PDF 与未知二进制默认拒绝发送，需要专用解析器后才能安全开放。
- AstrBot 主 Python 环境仍有 5 个既有依赖版本冲突；独立 TTS venv 的 `pip check` 正常。本轮未升级主环境，避免破坏当前机器人。

## 2026-07-11 任务执行内核
- 自然语言计划采用确定性保守切分，最多 8 步；复杂单句仍由 Agent 内部完成，但每个显式“然后/再/分号”步骤会单独重新授权。
- 代码步骤优先 Codex、桌面步骤优先 WorkBuddy；只读步骤失败可切换一次，任何已开始副作用的步骤禁止自动换后端重做。
- 确认绑定任务、步骤摘要、Pro QQ、会话、工作目录和实际路由后端；计划或路由变化后旧确认失效。
- 独立验证器不接受模型自述；检查退出码、必需交付物，并对明确代码/测试步骤运行有界的已知项目验证命令。
- 未知动作在隔离适配器未通过完整自检时保持阻塞，即使用户确认也不会静默落到主机完整权限。
- 加密载荷保存最多 8 步和步骤游标；只读任务从游标恢复，写入/高风险/未知步骤不自动重放，已提交交付摘要的任务只补发。

## 2026-07-12 小柠体验层
- 回复必须先脱敏再截句；公共脱敏器未覆盖裸 `token=`，体验层增加了赋值型 token/credential 遮罩。
- 进度节流按任务 ID 记录已发送事件；开始、确认、完成、失败各一次，非终态阶段只有运行满 90 秒后可额外发送一次。
- 体验记忆使用 owner HMAC 索引和当前用户 DPAPI 密文；固定字段白名单，关系事实必须由 Pro 明确要求，且永远不进入权限策略。
- GPT-SoVITS 只有显式授权开关与引擎同时存在才进入可用枚举；默认关闭并回退 MeloTTS，不以体验升级为由启用真人克隆。
