"""Fail-closed action classification for full-permission local agents."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class ActionClass(str, Enum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    HIGH_IMPACT = "high_impact"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ActionAssessment:
    action_class: ActionClass
    reason: str


_HIGH_IMPACT = re.compile(
    r"删除|清空|格式化|覆盖|挪|搬移|移动|重命名|替换现有|"
    r"安装|卸载|部署|发布|提交|推送|上传|发送|分享|群发|发邮件|发消息|"
    r"登录|支付|购买|转账|权限|注册表|防火墙|系统服务|计划任务|"
    r"密码|密钥|令牌|凭据|cookie|浏览器记录|私聊|通讯录|"
    r"\b(?:delete|remove|wipe|overwrite|move|rename|install|uninstall|deploy|"
    r"publish|submit|push|upload|send|share|login|pay|purchase|transfer|chmod|"
    r"chown|takeown|icacls|password|secret|token|credential|cookie)\b",
    re.I,
)
_WORKSPACE_WRITE = re.compile(
    r"新建|创建|生成|写入|编写|修改|编辑|修复|实现|重构|更新|添加|增加|"
    r"复制|保存|导出|"
    r"\b(?:create|generate|write|modify|edit|fix|implement|refactor|update|add|"
    r"copy|save|export)\b",
    re.I,
)
_EXECUTION = re.compile(
    r"运行|执行|测试|构建|编译|打包|启动脚本|调用脚本|命令|"
    r"\b(?:run|execute|test|build|compile|package|script|command)\b",
    re.I,
)
_READ_ONLY = re.compile(
    r"读取|查看|搜索|查找|检查|解释|总结|分析|审查|审计|列出|比较|核对|"
    r"\b(?:read|view|search|find|inspect|explain|summarize|analyse|analyze|"
    r"review|audit|list|compare|check)\b",
    re.I,
)


def classify_action(task: object) -> ActionAssessment:
    value = str(task or "").strip()
    if _HIGH_IMPACT.search(value):
        return ActionAssessment(ActionClass.HIGH_IMPACT, "高影响操作")
    if _WORKSPACE_WRITE.search(value):
        return ActionAssessment(ActionClass.WORKSPACE_WRITE, "工作区写入")
    if _EXECUTION.search(value):
        return ActionAssessment(ActionClass.UNKNOWN, "命令执行需确认")
    if _READ_ONLY.search(value):
        return ActionAssessment(ActionClass.READ_ONLY, "严格只读")
    return ActionAssessment(ActionClass.UNKNOWN, "未识别的本机动作")
