"""配置读取与验证模块。

包含配置校验、会话配置解析等基础逻辑。
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.star import StarTools

try:
    from friend_core.relationship_state import (
        can_send_proactive,
        load_state,
        record_proactive_send,
        save_state,
    )
except ImportError:
    from data.plugins.friend_core.relationship_state import (
        can_send_proactive,
        load_state,
        record_proactive_send,
        save_state,
    )

try:
    from draw_command.pro_access import Tier, get_tier
except ImportError:
    from data.plugins.draw_command.pro_access import Tier, get_tier


_PRO_DB = (
    Path(__file__).resolve().parents[3]
    / "plugin_data"
    / "xiaoning_pro"
    / "pro_members.db"
)


class ConfigMixin:
    """配置读取与验证混入类。"""

    config: dict
    session_override_manager: Any

    def _private_proactive_allowed(self, session_id: str, cooldown_seconds: float) -> bool:
        parsed = self._parse_session_id(session_id)
        if not parsed or ("Friend" not in parsed[1] and "Private" not in parsed[1]):
            return True
        state_path = Path(StarTools.get_data_dir("proactive_behavior")) / "relationship_state.json"
        return can_send_proactive(load_state(state_path), parsed[2], cooldown_seconds)

    def _record_private_proactive_send(self, session_id: str) -> None:
        parsed = self._parse_session_id(session_id)
        if not parsed or ("Friend" not in parsed[1] and "Private" not in parsed[1]):
            return
        state_path = Path(StarTools.get_data_dir("proactive_behavior")) / "relationship_state.json"
        state = load_state(state_path)
        record_proactive_send(state, parsed[2])
        save_state(state_path, state)

    async def _validate_config(self) -> None:
        """验证插件配置的完整性和有效性"""
        try:
            # 读取全局配置块
            friend_settings = self.config.get("friend_settings", {})
            group_settings = self.config.get("group_settings", {})

            # 私聊配置校验
            if friend_settings.get("enable", False):
                session_list = friend_settings.get("session_list", [])
                if not session_list and not friend_settings.get("all_x_pro_sessions", False):
                    logger.warning(
                        "[主动消息] 私聊主动消息已启用但未配置任何会话喵（session_list 为空）。"
                    )

                # 调度区间合法性
                schedule_settings = friend_settings.get("schedule_settings", {})
                min_interval = schedule_settings.get("min_interval_minutes", 30)
                max_interval = schedule_settings.get("max_interval_minutes", 900)
                if min_interval > max_interval:
                    logger.warning(
                        "[主动消息] 私聊主动消息配置中最小间隔大于最大间隔喵，将自动调整喵。"
                    )

            # 群聊配置校验
            if group_settings.get("enable", False):
                session_list = group_settings.get("session_list", [])
                if not session_list:
                    logger.warning(
                        "[主动消息] 群聊主动消息已启用但未配置任何会话喵（session_list 为空）。"
                    )

            logger.info("[主动消息] 配置验证完成喵。")

        except Exception as e:
            logger.error(f"[主动消息] 配置验证过程出错喵: {e}")
            raise

    def _get_session_config(self, session_id: str) -> dict | None:
        """根据会话 UMO 获取最终生效配置（base + override）。"""
        base = self._get_base_session_config(session_id)
        if not base:
            return None
        return self._build_effective_config(session_id, base)

    def _get_base_session_config(self, session_id: str) -> dict | None:
        """获取仅由全局配置与会话命中规则决定的基础配置。"""
        parsed = self._parse_session_id(session_id)
        if not parsed:
            return None

        _, message_type, target_id = parsed
        # 根据消息类型路由到不同配置区块（私聊/群聊）
        # FriendMessage / PrivateMessage 均归为私聊配置
        if "Friend" in message_type or "Private" in message_type:
            return self._get_typed_session_config(
                session_id, target_id, "friend_settings", "friend"
            )
        # GroupMessage / GuildMessage 均归为群聊配置
        if "Group" in message_type:
            return self._get_typed_session_config(
                session_id, target_id, "group_settings", "group"
            )
        return None

    def _build_effective_config(
        self, session_id: str, base_config: dict | None
    ) -> dict | None:
        """将会话差异补丁合并到基础配置，返回最终生效配置。"""
        if not base_config:
            return None

        manager = getattr(self, "session_override_manager", None)
        if not manager:
            return base_config

        normalized_session_id = self._normalize_session_id(session_id)
        effective = manager.get_effective(normalized_session_id, base_config)

        if isinstance(effective, dict):
            # 保留运行时元信息，避免被白名单过滤丢失
            effective["_session_type"] = base_config.get("_session_type")
            effective["_from_session_list"] = base_config.get(
                "_from_session_list", False
            )
            effective["_has_override"] = bool(
                manager.get_override(normalized_session_id)
            )

        return effective

    def _get_typed_session_config(
        self, session_id: str, target_id: str, settings_key: str, session_type: str
    ) -> dict | None:
        # 配置仅在 enable 且命中 session_list 时生效
        settings = self.config.get(settings_key, {})
        if not settings.get("enable", False):
            return None

        # 命中规则：支持完整 UMO、规范化 UMO 或纯 target_id 三种写法。
        # 私聊可额外配置为所有 X/Pro 用户自动加入，普通用户仍保持完全关闭。
        session_list = settings.get("session_list", [])
        normalized_session_id = self._normalize_session_id(session_id)
        candidates = {session_id, normalized_session_id, target_id}
        from_session_list = any(candidate in session_list for candidate in candidates)
        all_x_pro = session_type == "friend" and bool(
            settings.get("all_x_pro_sessions", False)
        )
        eligible_x_pro = False
        if all_x_pro and str(target_id).isdigit():
            try:
                eligible_x_pro = get_tier(str(target_id), _PRO_DB) >= Tier.X
            except Exception:
                eligible_x_pro = False

        if from_session_list or eligible_x_pro:
            # 返回深拷贝，避免调用方意外修改全局配置对象
            config_copy = copy.deepcopy(settings)
            config_copy["_session_type"] = session_type
            config_copy["_from_session_list"] = from_session_list
            config_copy["_all_x_pro_session"] = eligible_x_pro
            return config_copy

        return None

    def _get_friend_session_config(
        self, session_id: str, target_id: str
    ) -> dict | None:
        return self._get_typed_session_config(
            session_id, target_id, "friend_settings", "friend"
        )

    def _get_group_session_config(self, session_id: str, target_id: str) -> dict | None:
        return self._get_typed_session_config(
            session_id, target_id, "group_settings", "group"
        )
