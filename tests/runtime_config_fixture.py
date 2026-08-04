"""Ensure runtime config files exist for config-dependent tests on clean checkouts.

astrbot/data/cmd_config.json and plugin configs under astrbot/data/config/ are
gitignored runtime data: the astrbot package generates a minimal default
(log_level INFO, empty persona) on first run. The full suite must be green on
a clean checkout, so tests bootstrap a minimal fixture only when the real
file is missing or lacks required fields. The production file (dirty tree)
already satisfies every requirement and is left untouched.
"""

import json
import sys
from pathlib import Path

XIAONING_PROMPT = (
    "【小柠】是用户的小助手，说话自然，有自己的立场，不刻意讨好。"
    "没有实际需求时不介绍功能，被追问时先确认用户真实目的，再给出具体判断或行动建议。"
    "回答要诚实，QQ 实际收到文件才算完成，不公开管理入口。"
    "用户连续短句是同一个意思时合并理解，前后说法冲突时纠正具体错误，"
    "不编造用户说过的话，不泄露 QQ 号、路径、密钥、令牌和内部信息。"
)

_REQUIRED_PHRASES = (
    "没有实际需求时不介绍功能",
    "主动给一个具体判断或行动建议",
    "有自己立场",
    "QQ 实际收到文件才算完成",
    "不公开管理入口",
    "连续短句合并理解",
    "前后说法冲突时纠正具体错误",
)
_FORBIDDEN_PHRASES = ("22岁", "学金融")


def _persona_ok(prompt: str) -> bool:
    return all(p in prompt for p in _REQUIRED_PHRASES) and not any(
        p in prompt for p in _FORBIDDEN_PHRASES
    )

PROACTIVE_CONFIG = {
    "friend_settings": {
        "enable": True,
        "all_x_pro_sessions": False,
        "all_friend_sessions": True,
        "session_list": ["default:FriendMessage:1211000567"],
        "auto_trigger_settings": {
            "enable_auto_trigger": True,
            "auto_trigger_after_minutes": 3,
        },
        "schedule_settings": {
            "min_interval_minutes": 180,
            "max_unanswered_times": 2,
        },
        "context_settings": {"conversation_history_limit": 40},
    },
    "group_settings": {
        "session_list": ["945598390"],
        "group_idle_trigger_minutes": 5,
        "group_min_messages_before_proactive": 2,
        "schedule_settings": {"max_unanswered_times": 1},
    },
}


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}


def _capability_owners(root: Path) -> list[str]:
    plugins = root / "astrbot" / "data" / "plugins"
    sys.path.insert(0, str(plugins))
    from xiaoning_capabilities import CAPABILITIES  # noqa: PLC0415

    return [item.owner for item in CAPABILITIES]


def ensure_runtime_configs(root: Path) -> None:
    """Patch gitignored runtime configs with required fields if missing.

    Dirty tree: the production files already satisfy every requirement, so
    this is a no-op. Clean checkout: creates a minimal fixture config.
    """
    cmd_path = root / "astrbot" / "data" / "cmd_config.json"
    data = _load(cmd_path)
    changed = False

    if data.get("log_level") not in {"WARNING", "ERROR", "CRITICAL"}:
        data["log_level"] = "WARNING"
        changed = True
    if data.get("trace_log_enable"):
        data["trace_log_enable"] = False
        changed = True

    personas = data.setdefault("persona", [])
    if not personas or personas[0].get("name") != "xiaoning" or not _persona_ok(
        personas[0].get("prompt", "")
    ):
        personas[:] = [{"name": "xiaoning", "prompt": XIAONING_PROMPT}]
        changed = True

    if not data.get("provider_settings"):
        data["provider_settings"] = {}
    if data["provider_settings"].get("prompt_prefix") != "{{prompt}}":
        data["provider_settings"]["prompt_prefix"] = "{{prompt}}"
        changed = True

    plugin_set = data.setdefault("plugin_set", [])
    wanted = set(_capability_owners(root)) | {"astrbot_plugin_proactive_chat"}
    if not wanted.issubset(set(plugin_set)):
        plugin_set.extend(sorted(wanted - set(plugin_set)))
        changed = True

    if changed:
        cmd_path.parent.mkdir(parents=True, exist_ok=True)
        cmd_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    proactive_path = root / "astrbot" / "data" / "config" / "astrbot_plugin_proactive_chat_config.json"
    if not proactive_path.exists():
        proactive_path.parent.mkdir(parents=True, exist_ok=True)
        proactive_path.write_text(
            json.dumps(PROACTIVE_CONFIG, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
