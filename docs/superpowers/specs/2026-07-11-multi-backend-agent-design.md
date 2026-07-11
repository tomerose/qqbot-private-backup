# QQBot 三后端本机 Agent 设计

## 目标

仅 QQ `1211000567` 可在私聊或群聊明确 @ 小柠后，调用本机 Claude Code、Codex CLI、WorkBuddy CLI 完成任务。Claude 为默认后端，现有全局 Claude Code 与 cc-connect 配置保持不变。

## 交互

- `/agent use claude|codex|workbuddy`：切换当前后端。
- `/agent cwd [绝对目录]`：查看或设置执行目录；允许所有现存本机目录。
- `/agent run <任务>`：使用当前后端、完整权限执行。
- `/agent status`：查看后端、目录和运行任务。
- `/agent cancel`：终止当前任务。

## 执行与回传

- 三个 CLI 均使用参数数组启动，不拼接 Shell 命令。
- 同一时间只运行一个任务，保留取消、超时、日志和输出大小控制。
- 每次任务建立独立 `outputs` 目录，提示 Agent 将需要回传的交付物复制到该目录。
- 图片使用 QQ 图片消息回传，其他普通文件使用 QQ 文件消息回传。
- 只回传任务 `outputs` 内的普通文件，并限制数量与单文件大小，避免误发本机任意文件。

## 权限边界

- 只有精确发送者 ID `1211000567` 有执行权限。
- 群聊必须真实 @ 当前机器人。
- Agent 能力不做目录或工具限制；传输层与稳定性保护不等于能力沙箱。
- 不修改全局 Claude Code、Codex、WorkBuddy 或 cc-connect 配置。
