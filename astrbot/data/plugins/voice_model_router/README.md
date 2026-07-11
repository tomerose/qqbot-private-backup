# 小柠语音路由

- 默认文字聊天继续使用 DeepSeek。
- 收到 QQ 语音输入或用户明确要求“发语音/语音回答/语音回复/用语音说”时，切换 Gemini Flash 处理语义。
- 只有明确要求语音输出才生成 QQ `Record`；普通聊天始终返回文字。
- 朗读前删除代码块、本机路径、Bearer/API Key/Token/Password/Secret，最多 600 字、3 段。
- TTS 只连接 `127.0.0.1:8766`，使用私有令牌；返回文件必须位于会话私有音频目录且通过大小、扩展名和符号链接校验。
- 当前落地引擎为本机 CPU MeloTTS；GPT-SoVITS 接口已保留，但没有经授权的参考声线时不会启用真人克隆。
- `/health` 只返回固定引擎可用性枚举。`authorized_voice` 默认关闭，即使检测到 GPT-SoVITS 引擎也会安全降级到 MeloTTS。
- TTS 失败时保留脱敏后的文字回复，不发送错误路径、令牌或任务内容。
- QQ 消息发送完成后删除本次生成的 WAV；服务空闲时也每分钟清理超过 10 分钟的遗留音频。

启动本地服务：

```powershell
powershell -ExecutionPolicy Bypass -File services\local_tts\start_local_tts.ps1
```

首次安装会创建 Python 3.11 隔离环境，并校验固定 SHA-256 的官方 NLTK 资源。服务只监听本机环回地址。
