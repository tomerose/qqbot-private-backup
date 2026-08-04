# 小柠 QQBot 私有备份

![CI](https://github.com/tomerose/qqbot-private-backup/actions/workflows/ci.yml/badge.svg)

这是小柠的可复刻代码备份：AstrBot 插件、Gemini/Vertex 代理、本地 TTS、NapCat 启动入口和回归测试。聊天记录、QQ 登录态、API 密钥、模型配置和用户记忆不会进入 Git。

## 一键初始化（Windows 10/11）

```powershell
git clone https://github.com/tomerose/qqbot-private-backup.git
cd qqbot-private-backup
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

脚本会固定安装 AstrBot `4.26.5`，检查 Python 3.12/3.11 与 FFmpeg，生成 `xiaoning.local.ps1`，初始化 AstrBot，并启用 [xiaoning.plugins.json](xiaoning.plugins.json) 中的小柠插件。重复运行是安全的。

首次复刻仍需本人完成两项登录：

1. 在脚本打开的 NapCat 安装器中登录 QQ；OneBot HTTP 使用 `127.0.0.1:5701`，反向 WebSocket 指向 `ws://127.0.0.1:6199/ws`。若启用访问令牌，把同一个值写入 `xiaoning.local.ps1` 的 `NAPCAT_HTTP_TOKEN`。
2. 将 Google Cloud 项目写入 `xiaoning.local.ps1` 的 `VERTEX_PROJECT`，并在本机完成 Application Default Credentials 登录。其他模型 Provider 在 AstrBot WebUI 中填写，密钥不要提交。

完成后双击 `start_all_services.bat`。验证安装但不修改配置：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -CheckOnly
```

运行数据和备份边界见 [BACKUP_SCOPE.md](BACKUP_SCOPE.md)。
