# QQBot 私有备份范围

这个仓库保存 QQBot 的可维护项目资产：AstrBot 插件与扩展、任务执行代码、语音服务代码、测试、文档、启动脚本和依赖配置。

为避免即使在私有仓库中也泄露机主或聊天对象的隐私，下列本机运行数据不纳入 Git：

- 模型 API 密钥、QQ/OneBot 令牌、环境变量与私钥；
- QQ 登录态、二维码、NapCat 运行目录与浏览器缓存；
- 聊天附件、知识库、插件记忆、会话和任务数据库；
- 语音音频、日志、临时文件、构建缓存和本地安装压缩包。

恢复时，在目标机器重新填写 AstrBot / NapCat 的本地配置，并重新扫码登录 QQ。不要把真实令牌或登录状态提交到 Git。

`astrbot/data/plugins/emotional_chat/main.py` 的本机副本含有硬编码的模型密钥，已排除；对应的无密钥恢复模板位于 `backup_templates/astrbot/data/plugins/emotional_chat/main.py`，使用前将其放回原路径并设置 `DEEPSEEK_API_KEY`。
