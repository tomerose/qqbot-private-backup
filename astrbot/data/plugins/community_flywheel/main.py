"""小柠社群飞轮 — 让用户感到"参与感"和"养成感"
- /认识 — 查看你和小柠认识了多久、聊了多少
- /教 — 教小柠一个新词/梗，小柠学会了会在合适时候用
- 群聊自然参与：检测到关键词/梗时提供轻度参与提示
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
try:
    from friend_core.relationship_state import get_snapshot, load_state
except ImportError:
    from data.plugins.friend_core.relationship_state import get_snapshot, load_state



def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save_json(path: Path, data):
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class CommunityFlywheel(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        data_dir = Path(StarTools.get_data_dir("community_flywheel"))
        data_dir.mkdir(parents=True, exist_ok=True)
        self._legacy_state_file = data_dir / "flywheel_state.json"
        relationship_dir = Path(StarTools.get_data_dir("proactive_behavior"))
        relationship_dir.mkdir(parents=True, exist_ok=True)
        self._state_file = relationship_dir / "relationship_state.json"
        self._slang_file = data_dir / "slang_dict.json"
        self._state = load_state(self._state_file, [self._legacy_state_file])
        self._slang = _load_json(self._slang_file, {})

    # ── /认识 — 关系可视化 ──────────────────────────────

    @filter.command("认识")
    async def cmd_relationship(self, event: AstrMessageEvent):
        sender = _sender_id(event)
        if not sender or not sender.isdigit():
            yield event.plain_result("唔…认不出来你是谁。")
            event.stop_event()
            return

        entry = get_snapshot(self._state, sender)
        first_seen = entry.get("first_seen_ts", 0)
        msg_count = entry.get("message_count", 0)

        if not first_seen or msg_count < 5:
            yield event.plain_result(
                "咱俩好像还不太熟诶～多聊聊天嘛，慢慢就认识了 😄"
            )
            event.stop_event()
            return

        days = max(1, round((time.time() - first_seen) / 86400))
        lines = [
            f"我们认识 {days} 天了～",
            f"你一共发了 {msg_count} 条消息给我",
        ]
        # 计算关系标签
        if days >= 180:
            lines.append("🏆 算是老朋友啦")
        elif days >= 60:
            lines.append("🌟 挺熟的了")
        elif days >= 30:
            lines.append("👋 慢慢变熟了")
        elif days >= 7:
            lines.append("🌱 刚开始熟悉起来")
        else:
            lines.append("🌿 初见不久，来日方长")

        yield event.plain_result("\n".join(lines))
        event.stop_event()

    # ── /教 — 用户教小柠新词 ────────────────────────────

    _TEACH_RE = re.compile(r"^/教\s+(.+?)\s*[：:]\s*(.+)$", re.DOTALL)

    @filter.command("教")
    async def cmd_teach(self, event: AstrMessageEvent):
        text = _msg_text(event).strip()
        m = self._TEACH_RE.match(text)
        if not m:
            yield event.plain_result(
                "这样教：/教 绝绝子：特别棒、好到离谱的意思\n"
                "或者直接跟我说「小柠，『绝绝子』就是特别棒的意思」，我也能学会～"
            )
            event.stop_event()
            return

        word = m.group(1).strip()
        meaning = m.group(2).strip()

        if len(word) > 20 or len(meaning) > 200:
            yield event.plain_result("词太长了记不住啦，简短一点～")
            event.stop_event()
            return

        if word in self._slang:
            old = self._slang[word]
            self._slang[word] = {
                "meaning": meaning,
                "taught_by": _sender_id(event),
                "taught_at": time.time(),
                "times_taught": old.get("times_taught", 1) + 1,
            }
            yield event.plain_result(
                f"哦对，「{word}」之前有人教过我，更新了～"
            )
        else:
            self._slang[word] = {
                "meaning": meaning,
                "taught_by": _sender_id(event),
                "taught_at": time.time(),
                "times_taught": 1,
            }
            yield event.plain_result(
                f"收到！「{word}」={meaning}\n"
                f"学会了～以后遇到合适的时候我会用的 😄"
            )

        _save_json(self._slang_file, self._slang)
        event.stop_event()

    # ── 记录互动 ───────────────────────────────────────

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=979)
    async def on_message_track(self, event: AstrMessageEvent):
        """Record interaction for relationship tracking."""
        sender = _sender_id(event)
        if not sender or not sender.isdigit():
            return
        try:
            from friend_core.relationship_state import record_interaction, save_state
            entry = record_interaction(self._state, sender)
            if entry.get("message_count", 0) % 50 == 0:
                save_state(self._state_file, self._state)
        except Exception:
            pass

    # ── 注入学到的 slang 到 LLM ─────────────────────────

    @filter.on_llm_request(priority=-7)
    async def inject_slang(self, event: AstrMessageEvent, req) -> None:
        if not self._slang:
            return

        text = _msg_text(event).lower()
        # 只注入当前消息中可能相关的 slang（关键词匹配）
        relevant = []
        for word, info in self._slang.items():
            if word.lower() in text:
                relevant.append(f"「{word}」={info['meaning']}")
        if not relevant:
            return

        marker = "【社群词库】"
        sp = str(getattr(req, "system_prompt", "") or "")
        if marker in sp:
            return

        block = (
            f"\n\n{marker}\n"
            f"群友教过你的词，在合适的时候可以自然使用（不要生硬地全用）:\n"
            + "\n".join(f"- {r}" for r in relevant)
        )
        req.system_prompt = (sp + block).strip()

    # ── 定期落盘 ───────────────────────────────────────

    async def _save_loop(self):
        return


def _sender_id(event: AstrMessageEvent) -> str:
    g = getattr(event, "get_sender_id", None)
    return str(g() if callable(g) else "").strip()


def _msg_text(event: AstrMessageEvent) -> str:
    g = getattr(event, "get_message_str", None)
    return str(g() if callable(g) else "").strip()
