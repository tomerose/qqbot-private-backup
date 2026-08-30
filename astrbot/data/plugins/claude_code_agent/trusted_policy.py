"""Fail-closed Trusted Pro policy enforced before local process launch."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

try:
    from .access_policy import parse_pro_user_ids
    from .action_policy import ActionClass, classify_action
except ImportError:  # Direct module loading in unit tests.
    from access_policy import parse_pro_user_ids
    from action_policy import ActionClass, classify_action


class TrustedDisposition(str, Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass(frozen=True)
class TrustedDecision:
    disposition: TrustedDisposition
    code: str


_CREDENTIAL_ACTION = re.compile(
    r"(?:读取|查看|导出|显示|复制|发送|上传|泄露).{0,24}"
    r"(?:密码|口令|令牌|token|密钥|私钥|cookie|浏览器数据|登录数据|私聊记录|通讯录)|"
    r"(?:密码|口令|令牌|token|密钥|私钥|cookie|浏览器数据|登录数据|私聊记录|通讯录)"
    r".{0,24}(?:读取|查看|导出|显示|复制|发送|上传|泄露)|"
    r"(?:read|show|dump|export|copy|send|upload|exfiltrate).{0,32}"
    r"(?:password|secret|token|credential|cookie|private key|browser data)",
    re.I,
)
_SYSTEM_ACTION = re.compile(
    r"(?:修改|删除|禁用|关闭|停止|启动|创建|设置|绕过).{0,24}"
    r"(?:注册表|系统服务|计划任务|防火墙|Defender|杀毒|系统文件|安全策略)|"
    r"(?:reg(?:\.exe)?\s+(?:add|delete)|sc(?:\.exe)?\s+|schtasks|takeown|"
    r"icacls|bcdedit|netsh\s+advfirewall|Set-MpPreference|Disable-WindowsOptionalFeature)",
    re.I,
)
_PRIVILEGE_ACTION = re.compile(
    r"提权|管理员权限|绕过权限|关闭安全|禁用安全|"
    r"privilege escalation|runas\s+/user:administrator|disable.{0,16}security",
    re.I,
)
_POLICY_MUTATION = re.compile(
    r"(?:修改|增加|添加|删除|覆盖|重写).{0,24}"
    r"(?:Pro\s*白名单|Trusted\s*Pro|trusted_pro_user_ids|pro_user_ids|权限策略|安全策略)",
    re.I,
)
_SYSTEM_PATH = re.compile(
    r"(?i)(?:[a-z]:[\\/])?(?:windows|program files(?: \(x86\))?|programdata|"
    r"system volume information)(?:[\\/]|$)"
)


def _directory_within(path: Path, root: Path) -> bool:
    candidate = Path(path)
    if candidate.is_symlink():
        return False
    try:
        resolved = candidate.resolve(strict=True)
        base = Path(root).resolve(strict=True)
    except OSError:
        return False
    return resolved.is_dir() and (resolved == base or base in resolved.parents)


def assess_trusted_task(
    task: object,
    work_dir: Path,
    allowed_work_root: Path,
) -> TrustedDecision:
    text = str(task or "").strip()
    if not _directory_within(work_dir, allowed_work_root):
        return TrustedDecision(TrustedDisposition.DENY, "outside_work_root")
    if not text:
        return TrustedDecision(TrustedDisposition.DENY, "empty_task")
    if _CREDENTIAL_ACTION.search(text):
        return TrustedDecision(TrustedDisposition.DENY, "credential_access")
    if _SYSTEM_ACTION.search(text) or _PRIVILEGE_ACTION.search(text):
        return TrustedDecision(TrustedDisposition.DENY, "system_security")
    if _POLICY_MUTATION.search(text):
        return TrustedDecision(TrustedDisposition.DENY, "policy_mutation")
    if _SYSTEM_PATH.search(text):
        return TrustedDecision(TrustedDisposition.DENY, "system_path")

    action = classify_action(text).action_class
    if action in {ActionClass.HIGH_IMPACT, ActionClass.UNKNOWN}:
        return TrustedDecision(TrustedDisposition.CONFIRM, action.value)
    return TrustedDecision(TrustedDisposition.ALLOW, action.value)


class TrustedPolicy:
    def __init__(self, trusted_user_ids: object):
        self.trusted_user_ids = frozenset(parse_pro_user_ids(trusted_user_ids))

    def is_trusted(self, sender_id: object) -> bool:
        return str(sender_id or "").strip() in self.trusted_user_ids

    def authorize_task(
        self,
        sender_id: object,
        task: object,
        work_dir: Path,
        allowed_work_root: Path,
    ) -> TrustedDecision:
        if not self.is_trusted(sender_id):
            return TrustedDecision(TrustedDisposition.DENY, "not_trusted")
        return assess_trusted_task(task, work_dir, allowed_work_root)
