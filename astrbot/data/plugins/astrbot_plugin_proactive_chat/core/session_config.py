"""配置读取与验证模块。

包含配置校验、会话配置解析等基础逻辑。
"""

from __future__ import annotations

import copy
import hashlib
import json
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
try:
    from xiaoning_core.memory import MemoryGateway
    from xiaoning_core.trace import TraceStore, new_trace_id
except ImportError:
    from data.plugins.xiaoning_core.memory import MemoryGateway
    from data.plugins.xiaoning_core.trace import TraceStore, new_trace_id


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

    @staticmethod
    def _xiaoning_rollout_allowed(target_id: str) -> bool:
        try:
            path = Path(__file__).resolve().parents[3] / "config" / "xiaoning_core_config.json"
            settings = json.loads(path.read_text(encoding="utf-8-sig"))
            if bool(settings.get("proactive_kill_switch", False)):
                return False
            percent = max(0, min(100, int(settings.get("proactive_rollout_percent", 0))))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False
        bucket = int.from_bytes(
            hashlib.sha256(f"xiaoning-rollout-v1:{target_id}".encode()).digest()[:4],
            "big",
        ) % 100
        return bucket < percent

    def _private_proactive_allowed(self, session_id: str, cooldown_seconds: float) -> bool:
        parsed = self._parse_session_id(session_id)
        if not parsed:
            return False
        normalized = self._normalize_session_id(session_id)
        manual = normalized in getattr(self, "manual_trigger_sessions", set())
        if "Friend" not in parsed[1] and "Private" not in parsed[1]:
            return manual
        gateway = getattr(self, "_xiaoning_memory_gateway", None)
        if gateway is None:
            try:
                root = Path(StarTools.get_data_dir("xiaoning_core"))
                gateway = MemoryGateway(root / "xiaoning-memory.sqlite3")
                self._xiaoning_memory_gateway = gateway
            except Exception:
                return False
        if not gateway.get_consent(parsed[2]).proactive:
            return False
        profile = gateway.get_relationship_profile(parsed[2])
        if not profile.activated:
            return False
        state_path = Path(StarTools.get_data_dir("proactive_behavior")) / "relationship_state.json"
        if not can_send_proactive(load_state(state_path), parsed[2], cooldown_seconds):
            return False
        if manual:
            return True
        if not self._xiaoning_rollout_allowed(parsed[2]):
            return False
        candidate = gateway.claim_due_candidate(parsed[2])
        if candidate is None:
            return False
        selected = getattr(self, "_xiaoning_selected_candidates", None)
        if selected is None:
            selected = {}
            self._xiaoning_selected_candidates = selected
        selected[normalized] = candidate
        return True

    def _xiaoning_candidate_context(self, session_id: str) -> str:
        selected = getattr(self, "_xiaoning_selected_candidates", {})
        candidate = selected.get(self._normalize_session_id(session_id))
        if candidate is None:
            return ""
        return (
            "\n\n【本次主动联系的唯一具体切口】\n"
            f"原因：{candidate.why_now}\n"
            f"证据类型：{candidate.source_type}；综合分：{candidate.score:.3f}\n"
            "只围绕这个切口写一句自然短消息；证据不足或时机不对就输出 NO_SEND。"
        )

    def _decorate_private_proactive_message(self, session_id: str, text: str) -> str:
        parsed = self._parse_session_id(session_id)
        if not parsed or ("Friend" not in parsed[1] and "Private" not in parsed[1]):
            return text
        gateway = getattr(self, "_xiaoning_memory_gateway", None)
        if gateway is None:
            return text
        selected = getattr(self, "_xiaoning_selected_candidates", {})
        if self._normalize_session_id(session_id) not in selected:
            return text
        profile = gateway.get_relationship_profile(parsed[2])
        if profile.first_proactive_notice_sent:
            return text
        return text.rstrip() + "\n最近不想我主动找你，直接说一声就行。"

    def _record_private_proactive_send(self, session_id: str) -> None:
        parsed = self._parse_session_id(session_id)
        if not parsed or ("Friend" not in parsed[1] and "Private" not in parsed[1]):
            return
        state_path = Path(StarTools.get_data_dir("proactive_behavior")) / "relationship_state.json"
        state = load_state(state_path)
        record_proactive_send(state, parsed[2])
        save_state(state_path, state)
        selected = getattr(self, "_xiaoning_selected_candidates", {})
        candidate = selected.pop(self._normalize_session_id(session_id), None)
        gateway = getattr(self, "_xiaoning_memory_gateway", None)
        if candidate is not None and gateway is not None:
            if not gateway.mark_candidate_sent(parsed[2], candidate.candidate_id):
                logger.error(
                    "[主动消息] 已发送但候选状态未能记为 sent；候选保持不可重发状态。"
                )
            else:
                try:
                    root = Path(StarTools.get_data_dir("xiaoning_core"))
                    TraceStore(root / "events.jsonl").record_engagement(
                        trace_id=new_trace_id(),
                        scope=session_id,
                        event_type="proactive_sent",
                        attributes={
                            "source_type": candidate.source_type,
                            "score": candidate.score,
                        },
                    )
                except Exception:
                    logger.warning("[主动消息] 匿名主动效果事件写入失败。")

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
        # 私聊可额外覆盖所有已建立会话，或只覆盖 X/Pro 用户。
        session_list = settings.get("session_list", [])
        normalized_session_id = self._normalize_session_id(session_id)
        candidates = {session_id, normalized_session_id, target_id}
        normalized_parsed = self._parse_session_id(normalized_session_id)
        if normalized_parsed:
            candidates.add(normalized_parsed[2])
        from_session_list = any(candidate in session_list for candidate in candidates)
        all_x_pro = session_type == "friend" and bool(
            settings.get("all_x_pro_sessions", False)
        )
        all_friend_sessions = session_type == "friend" and bool(
            settings.get("all_friend_sessions", False)
        )
        eligible_x_pro = False
        if all_x_pro and str(target_id).isdigit():
            try:
                eligible_x_pro = get_tier(str(target_id), _PRO_DB) >= Tier.X
            except Exception:
                eligible_x_pro = False

        if from_session_list or eligible_x_pro or all_friend_sessions:
            # 返回深拷贝，避免调用方意外修改全局配置对象
            config_copy = copy.deepcopy(settings)
            config_copy["_session_type"] = session_type
            config_copy["_from_session_list"] = from_session_list
            config_copy["_all_x_pro_session"] = eligible_x_pro
            config_copy["_all_friend_session"] = all_friend_sessions
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
