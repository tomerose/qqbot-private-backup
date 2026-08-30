# 小柠公开发布边界

这个仓库只保存可复刻的项目资产：AstrBot 插件、服务脚本、测试、文档和无密钥配置模板。

以下内容不得进入 Git 或公开发布：

- 模型 API Key、QQ/OneBot 令牌、SMTP 密码、私钥和任何登录凭据；
- QQ 登录态、二维码、聊天记录、私有记忆、附件、任务数据库和浏览器缓存；
- 运行日志、临时文件、语音音频、构建产物和本机安装目录；
- 真实 QQ 号、群号、平台标识、Google/Firebase 项目标识和 Windows 用户路径；
- 任何只对单台机器或单个部署者有效的服务地址。

公开安装使用 `xiaoning.local.ps1` 的本地副本保存用户自己的 API 与平台配置。该文件已被 `.gitignore` 排除；重新部署时必须在目标机器重新填写配置并重新完成 QQ 登录。

发布前应执行：

```powershell
git grep -n -I -E 'API_KEY|TOKEN|SECRET|PASSWORD|C:\\Users\\|D:\\|solar-modem|[0-9]{7,12}' -- ':!tests/**'
git diff --check
```

发现真实密钥后，先撤销并轮换密钥，再清理 Git 历史；仅删除当前文件不足以降低泄露风险。
