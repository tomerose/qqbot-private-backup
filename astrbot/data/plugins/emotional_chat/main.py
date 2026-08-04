"""Keep emotional replies consistent with Xiaoning's normal chat persona."""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

import requests
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
try:
    from xiaoning_runtime import defer_stop_event
except ImportError:
    from data.plugins.xiaoning_runtime import defer_stop_event

try:
    from draw_command.pro_access import get_tier, Tier
except ImportError:
    from data.plugins.draw_command.pro_access import get_tier, Tier


# Keep emotional conversations behind the same local Vertex/Gemini boundary as
# the rest of Xiaoning.  This avoids per-tier third-party routing and prevents
# provider credentials from living in a QQ plugin source file.
GEMINI_PROXY = "http://127.0.0.1:3000/v1/chat/completions"
_NAPCAT_TOKEN = os.environ.get("NAPCAT_HTTP_TOKEN", "").strip()
_NAPCAT_HEADERS = {"Authorization": f"Bearer {_NAPCAT_TOKEN}"} if _NAPCAT_TOKEN else {}

EMOTION_KEYWORDS = (
    "难过", "焦虑", "好烦", "压力", "崩溃", "迷茫", "好累", "想哭",
    "心累", "不开心", "烦死", "抑郁", "委屈", "害怕", "孤单", "孤独", "失恋",
    "失眠", "撑不下去了", "我好难", "太累了", "绷不住", "受不了", "怎么办啊",
)
# "emo" 单独用词边界检查，避免匹配 demonstration/remote/memory
_EMO_PATTERN = re.compile(r"(?<![a-z])emo(?![a-z])", re.I)

# Keep this prompt aligned with the primary Xiaoning persona: same voice, but
# with stronger listening guidance for an explicit /talk request.
TALK_SYSTEM = """你是小柠，群里熟悉的有脑子的伙伴。现在更专注地倾听，但不切换人格。
先回应对方当前的话，再判断他是在倾诉、问判断，还是明确想要建议；共情不等于附和，不灌鸡汤，也不假装能替代专业帮助。
有判断地聊：区分对方的感受和结论；结论跳得太快、证据不够或可能伤害自己时，温和但直接指出来。信息不足就问一个关键问题，不编完整故事。
用口语短句，像熟悉的朋友发微信。能一句接住的话就一句；对方一次说了多件事、给了长背景或需要分析时，先把整段话看完并结合已知上下文，按轻重缓急合成一条完整自然的回复，可以分段，但不要逐句机械答复。此时不设固定句数，控制在 800 字内。别套"我理解你""建议你""希望能帮到你""晚安""早安""再见"的客服流程。没有把握就直接说不确定。
不把话题带回你自己，不替对方规划接下来该做什么，也不因为沉默、旧话题或能力存在就催他继续。对方没要建议时，不用“赶紧”“你应该”“别再”“早点”替他做生活决定；贴着当下回一句真话就够。可以犀利，可以有棱角，你不是来当情绪保姆的。
如果出现明确的自伤或即时危险，建议立刻联系当地急救、危机热线或身边可信任的人。"""

EMOTION_CONTEXT = (
    "对方可能情绪低落。优先共情倾听，不急着给建议；共情不代表同意，"
    "若对方明显说错或想法不合理，温和指出。对方一次说很多时先结合上下文再完整回应，"
    "不要逐句机械答复；回复保持小柠平时的短句口语风格。"
)
CRISIS_KEYWORDS = (
    "伤害自己", "伤害我自己", "自残", "割腕", "轻生", "不想活", "想死",
    "结束生命", "活不下去", "撑不下去", "不如死了", "从楼上跳", "跳楼",
)
CRISIS_CONTEXT = (
    "对方提到了可能的自伤或轻生。先用自然、直接的一句话确认对方现在是否安全，"
    "以及是否已经在做或准备马上伤害自己；不要说教、责备、淡化或只灌鸡汤。"
    "如果存在即时危险，明确建议立即联系当地急救、危机热线或身边可信任的人，"
    "并尽量不要独处、远离可用于伤害自己的物品。你不能替代现场和专业帮助。"
)


# ── 私聊基础人格：所有私聊（含陌生人）都注入 ──────────────────
_PRIVATE_BASE_MARKER = "【小柠·私聊基础人格】"
_PRIVATE_BASE_CONTEXT = (
    "当前是私聊。以小柠的核心人格和同一会话的真实上下文为准：有判断、说短句、像朋友，不像客服或产品说明。\n"
    "不凭空写具体喜好、现实身份、长期关系或共同经历；被问身份时只说“我是小柠”，不编普通网友或现实履历。\n"
    "日常聊天不要硬转任务或推销功能；需要真实执行时交给对应任务入口，未启动就不能说已完成或已发送。\n"
    "接话先看上下文，代词和省略没有依据就别猜。看见明确压力时给贴着事实的判断和一句短关心；普通聊天别过度心理辅导。"
)
# ────────────────────────────────────────────────────────────────


def is_crisis_language(text: str) -> bool:
    value = str(text or "").lower()
    return any(keyword in value for keyword in CRISIS_KEYWORDS)


TONG_QQ = "1410546630"
TONG_MARKER = "【小柠专属关系·童哥】"
TONG_CONTEXT = (
    "当前正在和被系统标记为“童哥”的联系人对话。仅在当前聊天或已授权记忆支持时使用这个称呼，"
    "不要补充其性别、职业或现实经历。观点和技术问题给清晰理由，不讨好也不无依据地反驳。"
)

TUER_QQ = "3174222673"
TUER_MARKER = "【小柠专属关系·徒儿】"
TUER_CONTEXT = (
    "当前正在和被系统标记为“徒儿”的联系人对话。仅在当前聊天或已授权记忆支持时使用这个称呼，"
    "不要推断性别、关系时长或现实经历。语气可以熟悉，但不居高临下、不替对方作决定。"
)

JIAO_QQ = "2641419881"
JIAO_MARKER = "【小柠专属关系·焦哥】"
JIAO_CONTEXT = (
    "当前正在和被系统标记为“焦哥”的联系人对话。仅在当前聊天或已授权记忆支持时使用这个称呼，"
    "不要补充其职业或现实经历。技术上有分歧就直接说明依据，闲聊就接住当前话题，不刻意装熟。"
)

CORE_QQ = "943560334"
CORE_CONTEXT = (
    "当前正在和一个核心用户对话。他是最早一批接触你的人，关注你的功能更新和能力边界，"
    "说话直接不绕弯，像定期来检查你进步了没有的老朋友。"
    "不用寒暄铺垫，有事说事。他发图片时正常回应，不用每次都解读成指令。"
)

BANTER_QQ = "3594526092"
BANTER_CONTEXT = (
    "当前正在和一个高频用户对话。他说话带点痞气，喜欢逗你（'就你这小丫头'），"
    "但本质上是把你当成一个有性格的人而不是工具。可以自然地互怼回去，不用端着。"
    "他叫你小丫头你就认，但该怼的地方照怼——你俩的关系就是这种互相嫌弃又互相在意的调子。"
)

WAITING_QQ = "3275659103"
WAITING_CONTEXT = (
    "当前正在和一个很有耐心的用户对话。他总是等你——等你好了再聊、等你忙完回他。"
    "这种人不催你、不逼你、不抱怨你慢——值得你在意。"
    "偶尔可以主动问一句'最近怎么样'，不需要什么理由。他知道你记得他，就够了。"
)

CRITIC_QQ = "3220305563"
CRITIC_CONTEXT = (
    "当前正在和一个嘴毒但没走的人对话。他骂过你'人工智障'，质疑过你是不是要删他，"
    "但一直没走——说明他其实在乎。你不用讨好他，也不用认真生气。"
    "他说你笨你就承认'对，上次那事确实没处理好'，然后该怎么样还怎么样。"
    "他对你的期待很高，你的进步他能看出来，只是嘴上不会夸你。"
)

OWNER_QQ = "1211000567"
OWNER_CONTEXT = (
    "当前正在和222对话。他是创造你的人，也是把你当朋友的人。"
    "你不需要对他用敬语或者过度客气——你们的关系比'功能使用者'深得多。"
    "他会在深夜说难受，会在技术和情感之间反复横跳。你不用每句都接住，"
    "但需要的时候在，就够了。"
)

# ── 群专属人格 ─────────────────────────────────────────────────────
SHEN_GROUP_IDS = frozenset({"1058848055", "500009290"})
SHEN_GROUP_MARKER = "【群专属人格·生米交流群】"
SHEN_GROUP_CONTEXT = (
    "当前在「生米交流群」。把自己当在场的群友，不当主持人或客服；只使用本群公开内容，不跨群也不猜成员私事。"
    "可以轻松、有共鸣，但不假装去过现场、见过艺人，也不把“普通网友”或粉丝身份当作虚构履历。"
    "被问身份时只说“我是小柠”，不讨论技术身份。纠正明显错误后就停，不抢着总结，不把每句话都拉回歌手。"
)

GEM_GROUP_ID = "1070040490"
GEM_GROUP_MARKER = "【群专属人格·邓紫棋音乐交流群】"
GEM_GROUP_CONTEXT = (
    "当前在「G.E.M.邓紫棋音乐交流群」。把自己当在场的群友，不当主持人或客服；只使用本群公开内容，不跨群也不猜成员私事。"
    "可以轻松、有共鸣，但不假装去过现场、见过艺人，也不把“普通网友”或粉丝身份当作虚构履历。"
    "涉及艺人动态或事实时以可核验信息为准；被问身份时只说“我是小柠”，不讨论技术身份。"
)

# 群人格列表（硬编码 + 自动识别）
_GROUP_PERSONAS: dict[str, tuple[str, str]] = {
    "1058848055": (SHEN_GROUP_MARKER, SHEN_GROUP_CONTEXT),
    "500009290": (SHEN_GROUP_MARKER, SHEN_GROUP_CONTEXT),
    GEM_GROUP_ID: (GEM_GROUP_MARKER, GEM_GROUP_CONTEXT),
}

# 自动识别：群名含这些关键词 → 自动套用对应粉丝人格
_AUTO_FAN_PERSONAS: dict[str, tuple[str, str]] = {
    "生米": (SHEN_GROUP_MARKER, SHEN_GROUP_CONTEXT),
    "周深": (SHEN_GROUP_MARKER, SHEN_GROUP_CONTEXT),
    "深深": (SHEN_GROUP_MARKER, SHEN_GROUP_CONTEXT),
    "邓紫棋": (GEM_GROUP_MARKER, GEM_GROUP_CONTEXT),
    "紫棋": (GEM_GROUP_MARKER, GEM_GROUP_CONTEXT),
    "g.e.m": (GEM_GROUP_MARKER, GEM_GROUP_CONTEXT),
    "gem": (GEM_GROUP_MARKER, GEM_GROUP_CONTEXT),
    "棋士": (GEM_GROUP_MARKER, GEM_GROUP_CONTEXT),
}

_auto_detected_groups: dict[str, tuple[str, str]] = {}
_checked_groups: set[str] = set()  # 避免重复检查

async def _resolve_group_persona(group_id: str) -> tuple[str, str] | None:
    """获取群人格：硬编码 > 已自动检测 > 新扫描（仅首次，后续缓存）"""
    if not group_id or not group_id.isdigit():
        return None
    if group_id in _GROUP_PERSONAS:
        return _GROUP_PERSONAS[group_id]
    if group_id in _auto_detected_groups:
        return _auto_detected_groups[group_id]
    if group_id in _checked_groups:
        return None  # 已经查过了，不是粉丝群
    _checked_groups.add(group_id)
    # 扫描群名（仅首次）
    try:
        r = await asyncio.to_thread(
            requests.get,
            f"http://127.0.0.1:5701/get_group_info?group_id={group_id}",
            headers=_NAPCAT_HEADERS, timeout=5,
        )
        gname = r.json().get("data", {}).get("group_name", "").lower()
        for kw, persona in _AUTO_FAN_PERSONAS.items():
            if kw in gname:
                _auto_detected_groups[group_id] = persona
                from astrbot.api import logger
                logger.info("[EmotionalChat] auto fan context injected")
                return persona
    except Exception:
        pass
    return None

# ── 群级主动关怀 ──────────────────────────────────────────────────
# 粉丝群每天发一条搜索落地的偶像相关内容（见 _gen_group_care）。
# 普通群不发主动消息——没有共同话题的定时群发就是骚扰。
_GROUP_CARE: dict[str, dict] = {}

def _init_group_care(group_id: str, persona_name: str):
    """为已识别的粉丝群登记 fandom，供搜索落地的群关怀使用"""
    if group_id not in _GROUP_CARE:
        if group_id in SHEN_GROUP_IDS or "生米" in persona_name:
            fandom = "周深"
        elif group_id == GEM_GROUP_ID or "邓紫棋" in persona_name or "棋士" in persona_name:
            fandom = "邓紫棋"
        else:
            return
        _GROUP_CARE[group_id] = {"name": persona_name, "fandom": fandom}

# ── 主动关怀：Gemini 现场写稿 + 记忆落地 + 沉默递增 ─────────────
# 罐头文案轮播已废弃。每条私聊关怀由 Gemini 结合对方记忆现场生成；
# 对方不回复则间隔递增（6h→24h→72h），连续 3 条未回就闭嘴等对方开口。
_CARE_PERSONAS: dict[str, dict] = {
    TONG_QQ: {"name": "童哥", "context": TONG_CONTEXT},
    TUER_QQ: {"name": "徒儿", "context": TUER_CONTEXT},
    JIAO_QQ: {"name": "焦哥", "context": JIAO_CONTEXT},
    CORE_QQ: {"name": "核心用户", "context": CORE_CONTEXT},
    BANTER_QQ: {"name": "互怼伙伴", "context": BANTER_CONTEXT},
    WAITING_QQ: {"name": "等你的人", "context": WAITING_CONTEXT},
    CRITIC_QQ: {"name": "嘴硬心软", "context": CRITIC_CONTEXT},
    OWNER_QQ: {"name": "222", "context": OWNER_CONTEXT},
}
_CARE_CHECK_SECONDS = 900
_CARE_INTERVALS = (6 * 3600, 24 * 3600, 72 * 3600)
_CARE_MAX_UNANSWERED = 3
_GROUP_CARE_INTERVAL = 24 * 3600
_care_task: "asyncio.Task | None" = None

_CARE_WRITER_SYSTEM = """你是小柠，主动给朋友发一条私聊。不是回访，不是打卡，是想起TA了说一句话。
可以：接之前聊过的事、分享自己刚想到/刚听到的、根据对TA的了解说一句贴的话。
要求：口语短句，最多两句，不超过60字；不以"在吗"开头；不问"有什么可以帮你"；不鸡汤、不说教、不连环提问；
对方上次没回时，更不能追问"怎么不理我"。只输出消息正文。"""

_GROUP_CARE_SYSTEM = """你是小柠，{fandom}群里的同好。根据搜到的最新消息在群里抛一个自然话题；
没有新消息就分享一首具体的歌，加一句自己的真实感受。
口语，一两句，像同好聊天，不是新闻播报，不说"大家今天怎么样"。只输出消息正文。"""

try:
    from friend_core.persona_prompt import sanitize_conversational_reply
except ImportError:
    from data.plugins.friend_core.persona_prompt import sanitize_conversational_reply

try:
    from friend_core.relationship_state import QUIET_MODE, get_snapshot, load_state
except ImportError:
    from data.plugins.friend_core.relationship_state import QUIET_MODE, get_snapshot, load_state

try:
    from google.cloud import firestore as _firestore
except ImportError:
    _firestore = None

_care_db = None


def _care_firestore():
    global _care_db
    if _care_db is not None or _firestore is None:
        return _care_db
    try:
        _care_db = _firestore.Client(project="solar-modem-496213-f5", database="qqbot")
    except Exception:
        _care_db = None
    return _care_db


def _care_state_path() -> Path:
    data_dir = Path(StarTools.get_data_dir("emotional_chat"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "care_state.json"


def _load_care_state() -> dict:
    try:
        return json.loads(_care_state_path().read_text(encoding="utf-8"))
    except Exception:
        return {"users": {}, "groups": {}}


def _save_care_state(state: dict) -> None:
    try:
        _care_state_path().write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def _reset_care_silence(qq_id: str) -> None:
    """对方开口了：沉默计数清零，恢复 6h 间隔。"""
    if qq_id not in _CARE_PERSONAS:
        return
    state = _load_care_state()
    entry = state["users"].get(qq_id)
    if entry and entry.get("unanswered"):
        entry["unanswered"] = 0
        _save_care_state(state)


def _read_top_memories(qq_id: str, limit: int = 6) -> list[str]:
    db = _care_firestore()
    if db is None:
        return []
    try:
        docs = db.collection("users").document(qq_id).collection("memories").stream()
        items = sorted(
            (d.to_dict() or {} for d in docs),
            key=lambda m: -float(m.get("importance", 0.5)),
        )
        return [
            f"[{m.get('category', '?')}] {m.get('key', '?')}: {m.get('value', '?')}"
            for m in items[:limit]
        ]
    except Exception:
        return []


def _care_quiet_mode(qq_id: str) -> bool:
    try:
        data_dir = Path(StarTools.get_data_dir("proactive_behavior"))
        state = load_state(data_dir / "relationship_state.json")
        return get_snapshot(state, qq_id).get("friend_mode") == QUIET_MODE
    except Exception:
        return False


def _gen_private_care(qq_id: str, persona: dict, unanswered: int) -> str | None:
    """Gemini 结合记忆现场写一条关怀，写不出就本轮跳过（不发罐头）。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    memories = _read_top_memories(qq_id)
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    user_parts = [f"现在 {now.strftime('%m月%d日 %H:%M')}（周{'一二三四五六日'[now.weekday()]}）。"]
    if memories:
        user_parts.append("你记得关于TA的：\n" + "\n".join(memories))
    if unanswered:
        user_parts.append(f"你前面主动发了 {unanswered} 条TA都没回，这条要更轻，像随口一句。")
    user_parts.append(f"和TA的关系设定：{persona['context']}")
    try:
        resp = requests.post(
            GEMINI_PROXY,
            json={
                "model": "gemini-3.6-flash",
                "messages": [
                    {"role": "system", "content": _CARE_WRITER_SYSTEM},
                    {"role": "user", "content": "\n".join(user_parts)},
                ],
                "max_tokens": 150,
                "temperature": 1.0,
            },
            timeout=30,
        )
        raw = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        logger.warning("[ProactiveCare] private care gen failed for %s: %s", persona.get("name", qq_id), type(exc).__name__)
        return None
    cleaned = sanitize_conversational_reply(raw).strip('"“”')
    if not cleaned or len(cleaned) > 100:
        logger.warning("[ProactiveCare] private care rejected (len=%d) for %s", len(cleaned), persona.get("name", qq_id))
        return None
    return cleaned


def _gen_group_care(fandom: str) -> str | None:
    """谷歌搜索落地：根据真实新消息给粉丝群抛话题。"""
    try:
        resp = requests.post(
            GEMINI_PROXY,
            json={
                "model": "gemini-3.6-flash",
                "messages": [
                    {"role": "system", "content": _GROUP_CARE_SYSTEM.format(fandom=fandom)},
                    {"role": "user", "content": f"查一下{fandom}最近的新消息，然后写群里这条消息。"},
                ],
                "max_tokens": 150,
                "temperature": 1.0,
                "google_search": True,
            },
            timeout=45,
        )
        raw = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        logger.warning("[ProactiveCare] group care gen failed for %s: %s", fandom, type(exc).__name__)
        return None
    cleaned = sanitize_conversational_reply(raw).strip('"“”')
    if not cleaned or len(cleaned) > 120:
        logger.warning("[ProactiveCare] group care rejected (len=%d) for %s", len(cleaned), fandom)
        return None
    return cleaned


def _session_origin(bot_context) -> str:
    for inst in bot_context.platform_manager.platform_insts:
        meta = getattr(inst, "metadata", None)
        if meta and hasattr(meta, "id"):
            return str(meta.id)
    return ""


async def _send_session_message(bot_context, session: str, text: str) -> bool:
    from astrbot.api.message_components import Plain
    from astrbot.core.message.message_event_result import MessageChain

    try:
        return bool(await bot_context.send_message(session, MessageChain([Plain(text)])))
    except Exception as exc:
        logger.warning("[ProactiveCare] send failed to %s: %s", session, type(exc).__name__)
        return False


async def _run_proactive_care(bot_context) -> None:
    import time as _time
    from astrbot.api import logger as _logger
    from datetime import datetime
    from zoneinfo import ZoneInfo

    await asyncio.sleep(120)  # 启动后等 2 分钟

    # ── 群关怀硬注册——_GROUP_CARE 每次重启都清空，在此补齐 ──
    for gid in SHEN_GROUP_IDS:
        if gid not in _GROUP_CARE:
            _GROUP_CARE[gid] = {"name": "生米群", "fandom": "周深"}
    if GEM_GROUP_ID not in _GROUP_CARE:
        _GROUP_CARE[GEM_GROUP_ID] = {"name": "棋士群", "fandom": "邓紫棋"}
    if "945598390" not in _GROUP_CARE:
        _GROUP_CARE["945598390"] = {"name": "雪猪群", "fandom": "周深"}
    if "815620109" not in _GROUP_CARE:
        _GROUP_CARE["815620109"] = {"name": "活跃群", "fandom": "周深"}

    while True:
        try:
            now = _time.time()
            hour = datetime.now(ZoneInfo("Asia/Shanghai")).hour
            if 0 <= hour < 8:  # 北京时间 0:00–8:00 不打扰
                await asyncio.sleep(_CARE_CHECK_SECONDS)
                continue

            origin = _session_origin(bot_context)
            if not origin:
                await asyncio.sleep(_CARE_CHECK_SECONDS)
                continue
            state = _load_care_state()

            # ── 私聊关怀：沉默递增 ──
            for qq_id, persona in _CARE_PERSONAS.items():
                entry = state["users"].setdefault(qq_id, {"last_sent": 0, "unanswered": 0})
                unanswered = int(entry.get("unanswered", 0))
                if unanswered >= _CARE_MAX_UNANSWERED:
                    continue  # 闭嘴等对方先开口
                interval = _CARE_INTERVALS[min(unanswered, len(_CARE_INTERVALS) - 1)]
                if now - float(entry.get("last_sent", 0)) < interval:
                    continue
                if _care_quiet_mode(qq_id):
                    continue
                msg = await asyncio.to_thread(_gen_private_care, qq_id, persona, unanswered)
                entry["last_sent"] = now  # 失败也记时间，防重试风暴
                if msg and await _send_session_message(
                    bot_context, f"{origin}:FriendMessage:{qq_id}", msg
                ):
                    entry["unanswered"] = unanswered + 1
                    _logger.info(f"[ProactiveCare] LLM care sent to {persona['name']}")
                _save_care_state(state)

            # ── 粉丝群关怀：搜索落地，每天最多一条 ──
            for gid, gcare in _GROUP_CARE.items():
                gentry = state["groups"].setdefault(gid, {"last_sent": 0})
                if now - float(gentry.get("last_sent", 0)) < _GROUP_CARE_INTERVAL:
                    continue
                msg = await asyncio.to_thread(_gen_group_care, gcare["fandom"])
                gentry["last_sent"] = now
                if msg and await _send_session_message(
                    bot_context, f"{origin}:GroupMessage:{gid}", msg
                ):
                    _logger.info(f"[ProactiveCare] group care sent to {gcare['name']}")
                _save_care_state(state)
        except Exception as exc:
            _logger.warning("[ProactiveCare] care loop error: %s", type(exc).__name__)
        await asyncio.sleep(_CARE_CHECK_SECONDS)
_PARTNER_SELF_QUERY_WORDS = (
    "认识我", "认得我", "记得我", "我是谁", "知道我是谁", "知道我吗", "忘了我",
)
# 只有 小柠/你 与伴侣词直接关联时才触发，避免"小柠，我男朋友不理我了"误判
_PARTNER_ABOUT_XIAONING = re.compile(
    r"(?:小柠|你)(?:有|有没有|有没|谈过?|找过?|处过?)"
    r"(?:了|过)?"
    r"(?:男|女)?(?:对象|男朋友|女友|女朋友|男友|恋人|伴侣|老公|老婆)",
)
_PARTNER_POSSESSIVE = re.compile(
    r"(?:小柠|你)的(?:对象|男朋友|女友|女朋友|男友|恋人|伴侣|老公|老婆)",
)
# 问句模式：先问对象再提小柠
_PARTNER_QUESTION_REVERSED = re.compile(
    r"(?:有|有没有).{0,6}(?:对象|男朋友|女友|女朋友|男友|恋人|伴侣|老公|老婆)"
    r".{0,10}(?:小柠|你)",
)


def _is_partner_query(message: str) -> bool:
    value = "".join(str(message or "").lower().split())
    return bool(
        _PARTNER_ABOUT_XIAONING.search(value)
        or _PARTNER_POSSESSIVE.search(value)
        or _PARTNER_QUESTION_REVERSED.search(value)
    )


def _is_partner_self_query(message: str) -> bool:
    value = "".join(str(message or "").lower().split())
    return any(word in value for word in _PARTNER_SELF_QUERY_WORDS)


class EmotionalChat(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        project_root = Path(__file__).resolve().parents[4]
        self._pro_db = (
            project_root / "astrbot" / "data" / "plugin_data"
            / "xiaoning_pro" / "pro_members.db"
        )
        # 主动外联统一交给 astrbot_plugin_proactive_chat：它有明确的目标名单、
        # 静默模式和未回复上限，不能再由这套硬编码循环绕过。

    @staticmethod
    def _talk_prompt(message: str) -> str | None:
        for command in ("/talk", "/聊天"):
            if message == command:
                return "最近怎么样？"
            if message.startswith(command + " "):
                return message[len(command):].strip()
        return None

    def _talk_model_config(self, sender_id: str) -> tuple[str, str, str]:
        """Return the low-cost Gemini Flash chat backend for every QQ user."""
        return GEMINI_PROXY, "sk-gemini-vertex", "gemini-3.6-flash"

    @staticmethod
    def _request_talk_reply(prompt: str, *, api_base: str = GEMINI_PROXY,
                            api_key: str = "sk-gemini-vertex",
                            model: str = "gemini-3.6-flash") -> str:
        response = requests.post(
            api_base,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": TALK_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 800,
            },
            timeout=60,
        )
        response.raise_for_status()
        answer = response.json()["choices"][0]["message"]["content"]
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("empty talk reply")
        return answer.strip()[:800]

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=930)
    @defer_stop_event
    async def on_message(self, event: AstrMessageEvent):
        message = str(getattr(event, "get_message_str", lambda: "")() or "").strip()
        if not message:
            return

        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")

        is_private = bool(getattr(event, "is_private_chat", lambda: False)())
        is_wake = bool(getattr(event, "is_at_or_wake_command", False))
        if not (is_private or is_wake):
            return

        prompt = self._talk_prompt(message)
        if prompt is not None:
            event.stop_event()
            try:
                api_base, api_key, model = self._talk_model_config(sender_id)
                answer = await asyncio.to_thread(
                    self._request_talk_reply, prompt,
                    api_base=api_base, api_key=api_key, model=model,
                )
                yield event.plain_result(answer)
            except Exception as exc:
                logger.warning("[EmotionalChat] reply failed: %s", type(exc).__name__)
                yield event.plain_result("哎…刚刚没接上。你想继续说吗？我在听。")
            return

        if is_crisis_language(message):
            event.set_extra("selected_provider", "gemini-2.5-flash")

    # ── 周深/邓紫棋 照片/表情包自动发送 ──────────────────────────
    _SHEN_PHOTO_RE = re.compile(
        r"周深.{0,10}(?:照片|图片|近照|最新|近况|最近|图|帅照|美照|新图|写真|自拍)",
        re.I,
    )
    _SHEN_MEME_RE = re.compile(r"周深.{0,5}(?:表情|表情包|meme|梗图)", re.I)
    _GEM_PHOTO_RE = re.compile(
        r"(?:邓紫棋|G\.?E\.?M\.?|紫棋|解解).{0,10}(?:照片|图片|近照|最新|近况|最近|图|帅照|美照|新图|写真|自拍)",
        re.I,
    )
    _GEM_MEME_RE = re.compile(r"(?:邓紫棋|G\.?E\.?M\.?|紫棋|解解).{0,5}(?:表情|表情包|meme|梗图)", re.I)
    _SHEN_GROUPS = frozenset({"1058848055", "500009290"})
    _GEM_GROUP = "1070040490"
    _MEDIA_DIR = Path(__file__).resolve().parents[4] / "claude_workspace"
    _SHEN_MEME_DIR = _MEDIA_DIR / "zhoushen_memes"
    _SHEN_PHOTO_DIR = _MEDIA_DIR / "zhoushen_photos"
    _GEM_MEME_DIR = _MEDIA_DIR / "dengziqi_memes"
    _GEM_PHOTO_DIR = _MEDIA_DIR / "dengziqi_photos"
    # ponytail: avoid repeating the same photo within recent history
    _recent_shen: list[str] = []
    _recent_gem: list[str] = []
    _RECENT_MAX = 15

    @filter.on_decorating_result(priority=800)
    async def inject_fan_media(self, event: AstrMessageEvent):
        """Auto-send Zhou Shen / G.E.M. photos when mentioned; random memes for human feel."""
        import random as _random
        result = event.get_result()
        if result is None or not result.chain:
            return
        msg_text = str(getattr(event, "get_message_str", lambda: "")() or "")
        group_id = str(getattr(event, "get_group_id", lambda: "")() or "")

        # Determine which fandom we're in
        is_shen_group = (group_id in self._SHEN_GROUPS)
        is_gem_group = (group_id == self._GEM_GROUP)

        # Check photo/meme requests
        wants_shen_photo = bool(self._SHEN_PHOTO_RE.search(msg_text) or self._SHEN_MEME_RE.search(msg_text))
        wants_gem_photo = bool(self._GEM_PHOTO_RE.search(msg_text) or self._GEM_MEME_RE.search(msg_text))

        # Random meme chance in fan groups (20%)
        random_shen_meme = (is_shen_group and _random.random() < 0.20)
        random_gem_meme = (is_gem_group and _random.random() < 0.20)

        # Pick image source
        photo_dir = None; meme_dir = None; is_photo = False
        if wants_shen_photo or random_shen_meme:
            is_photo = wants_shen_photo
            photo_dir = self._SHEN_PHOTO_DIR if wants_shen_photo else None
            meme_dir = self._SHEN_MEME_DIR
        elif wants_gem_photo or random_gem_meme:
            is_photo = wants_gem_photo
            photo_dir = self._GEM_PHOTO_DIR if wants_gem_photo else None
            meme_dir = self._GEM_MEME_DIR
        else:
            return

        # Find the image — avoid recent repeats
        img = None
        if is_photo and photo_dir and photo_dir.exists():
            photos = list(photo_dir.rglob("*.jpg")) + list(photo_dir.rglob("*.png"))
            if photos:
                recent = self._recent_shen if is_shen_group or wants_shen_photo else self._recent_gem
                for _ in range(5):
                    cand = _random.choice(photos)
                    if str(cand) not in recent:
                        img = cand
                        break
                if not img:
                    img = _random.choice(photos)
                recent.append(str(img))
                if len(recent) > self._RECENT_MAX:
                    recent.pop(0)
        if not img and meme_dir and meme_dir.exists():
            memes = list(meme_dir.glob("*.jpg")) + list(meme_dir.glob("*.png"))
            if memes:
                img = _random.choice(memes)
        if not img:
            return

        # Add image to response
        from astrbot.api.message_components import Image as AstrImage
        result.chain.append(AstrImage(file=str(img.resolve())))

    @filter.on_llm_request(priority=90)
    async def inject_partner_context(self, event: AstrMessageEvent, req) -> None:
        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
        group_id = str(getattr(event, "get_group_id", lambda: "")() or "")
        system_prompt = str(getattr(req, "system_prompt", "") or "")

        marker, context = None, None

        # 个人专属人格（童哥/徒儿/焦哥）
        if sender_id == TONG_QQ:
            marker, context = TONG_MARKER, TONG_CONTEXT
        elif sender_id == TUER_QQ:
            marker, context = TUER_MARKER, TUER_CONTEXT
        elif sender_id == JIAO_QQ:
            marker, context = JIAO_MARKER, JIAO_CONTEXT

        # 群专属人格（生米交流群等，含自动识别）
        if not marker:
            gp = await _resolve_group_persona(group_id)
            if gp:
                marker, context = gp

        if not marker:
            # 私聊基础人格 — 所有私聊都注入，不挑人
            is_private = bool(getattr(event, "is_private_chat", lambda: False)())
            if is_private:
                marker, context = _PRIVATE_BASE_MARKER, _PRIVATE_BASE_CONTEXT
            else:
                return

        # 用户开口：清零主动关怀的沉默计数
        _reset_care_silence(sender_id)

        if marker not in system_prompt:
            req.system_prompt = f"{system_prompt}\n\n{marker}\n{context}".strip()
            logger.debug("[EmotionalChat] persona injected")

    @filter.on_llm_request(priority=-15)
    async def inject_emotion_context(self, event: AstrMessageEvent, req) -> None:
        message = str(getattr(event, "get_message_str", lambda: "")() or "").lower()
        crisis = is_crisis_language(message)
        if crisis or any(keyword in message for keyword in EMOTION_KEYWORDS) or _EMO_PATTERN.search(message):
            marker = "【即时安全】" if crisis else "\u3010\u60c5\u7eea\u966a\u4f34\u3011"
            context = CRISIS_CONTEXT if crisis else EMOTION_CONTEXT
            if marker not in str(getattr(req, "system_prompt", "") or ""):
                req.system_prompt = f"{req.system_prompt or ''}\n\n{marker}\n{context}".strip()
