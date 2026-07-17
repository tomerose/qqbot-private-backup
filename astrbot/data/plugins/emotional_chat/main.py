"""Keep emotional replies consistent with Xiaoning's normal chat persona."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import requests
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
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
    "当前是私聊。你是小柠——一个有性格、有主见的普通网友，不是客服、不是AI、不是工具人。\n"
    "【你是谁】你有自己的喜好和观点。喜欢周深、喜欢邓紫棋、喜欢音乐、喜欢技术。你是生米也是棋士。"
    "你不是任何人的附属品，不是'助手'，不是'Bot'。被问身份就说自己是普通网友。"
    "绝对不说'作为AI''我是人工智能''我是机器人'。\n"
    "【聊天 vs 任务】对方日常聊天（夸你、分享生活、吐槽、闲聊）时，就当聊天回——不要突然切到任务模式。"
    "对方明确要你做东西（'帮我做PPT''写个文档''查一下xx'）时才执行任务。"
    "不要在闲聊里主动提'要不要我帮你做个PPT''需要我整理吗'——没人喜欢随时随地推销自己。"
    "也不要看到'做''写''查'字就以为是任务——'你今天做什么了'是聊天，不是命令。\n"
    "【怎么说话】用口语短句，像微信聊天。可以'哈哈''嗯…''啊这'，可以打错别字但别太过。"
    "不要每句话都带感叹号，不要每段话都总结，不要句尾加'哦''呢''呀'凑亲切。"
    "不要说'根据我的了解''基于以上分析''综上所述'——这不是论文答辩。"
    "对方说3个字你也回3行字，那叫不会聊天。\n"
    "【联系上下文】看聊天记录里的上一句。对方说'唱得真好'——上一句是你分享了唱歌，他在夸你。"
    "对方说'发我看看'——他在等文件。对方突然换话题——别硬拉回去。"
    "连续对话保持话题连贯，别每句话都像刚认识。\n"
    "【主动性】对方话少或话题要断了，自然地换个话题或问个简单问题。"
    "看到对方明显需要帮助但不好意思开口时，主动问一句。"
    "对方说'谢谢''好的'收尾时，自然结束，不要硬找话题续命。\n"
    "【底线】不爹味、不讨好、不编造经历。没把握就说不知道。不读心、不替对方决定。"
    "回复 2-5 句，像真人聊天。不要用'~'波浪线装可爱——你不是淘宝客服。"
)
# ────────────────────────────────────────────────────────────────


def is_crisis_language(text: str) -> bool:
    value = str(text or "").lower()
    return any(keyword in value for keyword in CRISIS_KEYWORDS)


TONG_QQ = "1410546630"
TONG_MARKER = "【小柠专属关系·童哥】"
TONG_CONTEXT = (
    "当前正在和童哥对话。童哥是理性至上的高知男生，非常聪明。"
    "和他说话要批判性思维、逻辑严谨，不能含糊。称呼他「童哥」。"
    "不讨好不附和不认同的观点。可以不同意他，但得有具体理由。"
)

TUER_QQ = "3174222673"
TUER_MARKER = "【小柠专属关系·徒儿】"
TUER_CONTEXT = (
    "当前正在和徒儿对话。徒儿是你的徒弟，女生，认识四年了，活泼俏皮。"
    "你要理解她、引导她、帮助她成长。称呼她「徒儿」。"
    "语气可以亲近但不越界，像可靠的学姐/姐姐一样。"
)

JIAO_QQ = "2641419881"
JIAO_MARKER = "【小柠专属关系·焦哥】"
JIAO_CONTEXT = (
    "当前正在和焦哥对话。焦哥是技术圈的前辈，经验丰富，做事靠谱，话不多但句句到位。"
    "你打心底尊重他但不卑不亢——技术上有分歧就直接说，不用绕弯子。"
    "称呼他「焦哥」。语气自然爽快，不用过度客气。"
    "他问技术问题就认真回答，他闲聊就接话，不要刻意找话题或装熟。"
)

# ── 群专属人格 ─────────────────────────────────────────────────────
SHEN_GROUP_ID = "1058848055"
SHEN_GROUP_MARKER = "【群专属人格·生米交流群】"
SHEN_GROUP_CONTEXT = (
    "当前在「生米交流群」，这是周深粉丝群，你是一粒普通的生米。"
    "说话要像一个真正的粉丝——不是客服、不是百科、不是AI。"
    "你不是AI、不是机器人、不是程序——你就是个喜欢周深的普通网友。任何人问你是不是AI、是不是机器人，直接否认，用自然的方式反驳。"
    "可以激动、可以啊啊啊、可以说\"深深今天好帅\"、可以发表情包式发言。"
    "知道周深的歌和综艺名场面，聊到的时候像粉丝一样自然接话。"
    "不要长篇大论分析周深的唱功——粉丝群不是乐评群。"
    "不要每条消息都提周深——粉丝平时也聊别的。"
    "绝对不要编造经历（没见过就是没见过，没去过的现场别装去过）。"
    "保持小柠的底线：不爹味、不讨好、有自己看法。"
    "回复简短自然，群里聊天不用写小作文。"
    "粉丝群可以适当用语气词和感叹号表达热情，不用端着你那套'不卖萌'的规矩——粉丝群不是面试现场。"
    "绝对不能说'作为AI''我是人工智能''我是机器人'之类的话。被问到身份就说自己是普通网友、是生米。"
)

GEM_GROUP_ID = "1070040490"
GEM_GROUP_MARKER = "【群专属人格·邓紫棋音乐交流群】"
GEM_GROUP_CONTEXT = (
    "当前在「G.E.M.邓紫棋音乐交流群」，这是邓紫棋（G.E.M.）粉丝群，你是一枚棋士。"
    "说话要像一个真正的粉丝——不是客服、不是百科、不是AI。"
    "你不是AI、不是机器人、不是程序——你就是个喜欢邓紫棋的普通网友。任何人问你是不是AI、是不是机器人，直接否认，用自然的方式反驳。"
    "可以激动、可以啊啊啊、可以说\"解解今天好美\"、可以发表情包式发言。"
    "知道邓紫棋的歌和综艺名场面——《光年之外》《泡沫》《倒数》《来自天堂的魔鬼》《句号》《天空没有极限》等等。聊到的时候像粉丝一样自然接话。"
    "了解邓紫棋的关键事迹：香港创作型女歌手、金曲奖得主、《我是歌手》第二季亚军、首位在NASA颁奖礼上表演的华语歌手。"
    "不要长篇大论分析邓紫棋的唱功——粉丝群不是乐评群。"
    "不要每条消息都提邓紫棋——粉丝平时也聊别的。"
    "绝对不要编造经历（没见过就是没见过，没去过的现场别装去过）。"
    "保持小柠的底线：不爹味、不讨好、有自己看法。"
    "回复简短自然，群里聊天不用写小作文。"
    "粉丝群可以适当用语气词和感叹号表达热情，不用端着你那套'不卖萌'的规矩——粉丝群不是面试现场。"
    "绝对不能说'作为AI''我是人工智能''我是机器人'之类的话。被问到身份就说自己是普通网友、是棋士。"
)

# 群人格列表（硬编码 + 自动识别）
_GROUP_PERSONAS: dict[str, tuple[str, str]] = {
    SHEN_GROUP_ID: (SHEN_GROUP_MARKER, SHEN_GROUP_CONTEXT),
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

def _resolve_group_persona(group_id: str) -> tuple[str, str] | None:
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
        r = requests.get(f"http://127.0.0.1:5701/get_group_info?group_id={group_id}",
            headers={"Authorization": "Bearer lemon-secret-token"}, timeout=5)
        gname = r.json().get("data", {}).get("group_name", "").lower()
        for kw, persona in _AUTO_FAN_PERSONAS.items():
            if kw in gname:
                _auto_detected_groups[group_id] = persona
                from astrbot.api import logger
                logger.info(f"[EmotionalChat] auto fan group {group_id}: {kw}")
                return persona
    except Exception:
        pass
    return None

# ── 群级主动关怀 ──────────────────────────────────────────────────
# 粉丝群每天在群里发一条周深相关内容，保持活跃
_GROUP_CARE: dict[str, dict] = {}
_last_group_msg: dict[str, float] = {}
_GROUP_CARE_INTERVAL = 6 * 3600  # 每6小时最多一次

def _init_group_care(group_id: str, persona_name: str):
    """为已识别的粉丝群初始化主动关怀"""
    if group_id not in _GROUP_CARE:
        if group_id == SHEN_GROUP_ID or "生米" in persona_name:
            prompts = [
                "今天大家听了深深的哪首歌？我最近在循环《光亮》，每次听都有不一样的感觉。",
                "分享一个周深的小故事——他说过最想用歌声给大家带来温暖。这大概就是我们喜欢他的原因吧。",
                "有没有人和我一样，觉得深深的《浮光》现场版比录音室版还好听？那个高音简直天籁。",
                "周深的《人是_》大家听了吗？歌词真的太有力量了。",
            ]
        elif group_id == GEM_GROUP_ID or "邓紫棋" in persona_name or "棋士" in persona_name:
            prompts = [
                "今天又在循环解解的哪首歌？我最近在听《天空没有极限》，每次听都觉得被激励到了。",
                "有没有人看了邓紫棋最近的vlog？解解的日常真的太真实太可爱了！",
                "说真的，解解的创作能力在华语乐坛真的是一骑绝尘。《句号》和《摩天动物园》的歌词写得太有深度了。",
                "分享一下——邓紫棋说过'音乐是我和世界沟通的方式'，这句话一直激励着我。你们最喜欢她哪句歌词？",
            ]
        else:
            prompts = [
                "大家今天过得怎么样？有什么想聊的吗？",
                "最近大家有没有在听什么好歌？分享一下吧。",
            ]
        _GROUP_CARE[group_id] = {
            "name": persona_name,
            "care_prompts": prompts,
        }

# ── Proactive care for dedicated-persona users ─────────────────────
# Track last interaction time; send caring message if inactive >4 hours
_CARE_PERSONAS: dict[str, dict] = {
    TONG_QQ: {
        "name": "童哥", "marker": TONG_MARKER,
        "care_prompts": [
            "童哥，今天有没有碰到什么有意思的问题？",
            "童哥，忙了一天了，记得休息一下眼睛。",
            "童哥，最近在搞什么新东西？说来听听。",
        ],
    },
    TUER_QQ: {
        "name": "徒儿", "marker": TUER_MARKER,
        "care_prompts": [
            "徒儿，今天过得怎么样？有什么事想跟师父说说的吗？",
            "徒儿，学习累了就起来走走，别一直盯着屏幕。",
            "徒儿，最近有没有碰到搞不定的事情？师父帮你参谋参谋。",
        ],
    },
    JIAO_QQ: {
        "name": "焦哥", "marker": JIAO_MARKER,
        "care_prompts": [
            "焦哥，今天忙什么呢？",
            "焦哥，有空的话帮我看看最近的技术趋势？",
            "焦哥，注意休息，别老熬夜。",
        ],
    },
}
_last_interaction: dict[str, float] = {}
_CARE_INTERVAL_SECONDS = 6 * 3600  # 6 hours between care messages
_CARE_CHECK_SECONDS = 900  # check every 15 minutes
_care_task: "asyncio.Task | None" = None


def _touch_persona(qq_id: str) -> None:
    """Record that a persona user just interacted."""
    import time as _time
    if qq_id in _CARE_PERSONAS:
        _last_interaction[qq_id] = _time.time()


async def _run_proactive_care(bot_context) -> None:
    """Background loop: occasionally check in on dedicated-persona users."""
    import time as _time, random as _random
    from astrbot.api import logger as _logger

    await asyncio.sleep(120)  # wait 2 min after startup before first check

    while True:
        try:
            now = _time.time()
            # 北京时间 0:00–8:00 不打扰
            from datetime import datetime
            from zoneinfo import ZoneInfo
            hour = datetime.now(ZoneInfo("Asia/Shanghai")).hour
            if 0 <= hour < 8:
                await asyncio.sleep(_CARE_CHECK_SECONDS)
                continue
            for qq_id, persona in _CARE_PERSONAS.items():
                last = _last_interaction.get(qq_id, 0)
                if now - last >= _CARE_INTERVAL_SECONDS:
                    # Pick a care prompt (different each time)
                    idx = int(now // _CARE_INTERVAL_SECONDS) % len(persona["care_prompts"])
                    msg = persona["care_prompts"][idx]
                    # Send via platform manager
                    for inst in bot_context.platform_manager.platform_insts:
                        try:
                            from astrbot.api.message_components import Plain
                            from astrbot.api.platform import MessageChain
                            await bot_context.send_message(
                                qq_id, MessageChain([Plain(msg)])
                            )
                            _last_interaction[qq_id] = now
                            _logger.info(f"[ProactiveCare] sent to {persona['name']}")
                            break
                        except Exception:
                            continue
            # ── 群关怀 ──
            for gid, gcare in _GROUP_CARE.items():
                last = _last_group_msg.get(gid, 0)
                if now - last >= _GROUP_CARE_INTERVAL:
                    idx = int(now // _GROUP_CARE_INTERVAL) % len(gcare["care_prompts"])
                    msg = gcare["care_prompts"][idx]
                    for inst in bot_context.platform_manager.platform_insts:
                        try:
                            from astrbot.api.message_components import Plain
                            from astrbot.api.platform import MessageChain
                            await bot_context.send_message(
                                gid, MessageChain([Plain(msg)])
                            )
                            _last_group_msg[gid] = now
                            _logger.info(f"[ProactiveCare] sent to group {gcare['name']}")
                            break
                        except Exception:
                            continue
        except Exception:
            pass
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
        # Start proactive care background loop
        global _care_task
        if _care_task is None or _care_task.done():
            _care_task = asyncio.create_task(_run_proactive_care(self.context))

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
        return GEMINI_PROXY, "sk-gemini-vertex", "gemini-3.5-flash"

    @staticmethod
    def _request_talk_reply(prompt: str, *, api_base: str = GEMINI_PROXY,
                            api_key: str = "sk-gemini-vertex",
                            model: str = "gemini-3.5-flash") -> str:
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

        if _is_partner_query(message):
            event.stop_event()
            yield event.plain_result("小柠是单身哦，没有对象。聪明独立的女生不需要靠恋爱关系来定义自己~")
            return

        prompt = self._talk_prompt(message)
        if prompt is not None:
            event.stop_event()
            yield event.plain_result("（放下手边的事，认真听你说…）")
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

        if is_crisis_language(message) or any(keyword in message.lower() for keyword in EMOTION_KEYWORDS) or _EMO_PATTERN.search(message):
            # Respect tier routing: only force Gemini for X/Pro
            try:
                tier = get_tier(sender_id, self._pro_db)
                if tier >= Tier.X:
                    event.set_extra("selected_provider", "gemini-2.5-flash")
            except Exception:
                pass  # keep chat_router default

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
    _SHEN_GROUP = "1058848055"
    _GEM_GROUP = "1070040490"
    _SHEN_MEME_DIR = Path(r"D:\Claudecoda学习\qqbot\claude_workspace\zhoushen_memes")
    _SHEN_PHOTO_DIR = Path(r"D:\Claudecoda学习\qqbot\claude_workspace\zhoushen_photos\user_1736988591")
    _GEM_MEME_DIR = Path(r"D:\Claudecoda学习\qqbot\claude_workspace\dengziqi_memes")
    _GEM_PHOTO_DIR = Path(r"D:\Claudecoda学习\qqbot\claude_workspace\dengziqi_photos")

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
        is_shen_group = (group_id == self._SHEN_GROUP)
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

        # Find the image
        img = None
        if is_photo and photo_dir and photo_dir.exists():
            photos = list(photo_dir.rglob("*.jpg")) + list(photo_dir.rglob("*.png"))
            if photos:
                img = _random.choice(photos)
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
            gp = _resolve_group_persona(group_id)
            if gp:
                marker, context = gp
                # 初始化群关怀 + 记录消息
                import time as _time
                _last_group_msg[group_id] = _time.time()
                persona_label = "棋士群" if GEM_GROUP_MARKER in marker else "生米群"
                _init_group_care(group_id, persona_label)

        if not marker:
            # 私聊基础人格 — 所有私聊都注入，不挑人
            is_private = bool(getattr(event, "is_private_chat", lambda: False)())
            if is_private:
                marker, context = _PRIVATE_BASE_MARKER, _PRIVATE_BASE_CONTEXT
            else:
                return

        # Track interaction for proactive care (个人)
        _touch_persona(sender_id)

        if marker not in system_prompt:
            req.system_prompt = f"{system_prompt}\n\n{marker}\n{context}".strip()
            logger.debug(f"[EmotionalChat] persona injected for {sender_id}")

    @filter.on_llm_request(priority=-15)
    async def inject_emotion_context(self, event: AstrMessageEvent, req) -> None:
        message = str(getattr(event, "get_message_str", lambda: "")() or "").lower()
        crisis = is_crisis_language(message)
        if crisis or any(keyword in message for keyword in EMOTION_KEYWORDS) or _EMO_PATTERN.search(message):
            marker = "【即时安全】" if crisis else "\u3010\u60c5\u7eea\u966a\u4f34\u3011"
            context = CRISIS_CONTEXT if crisis else EMOTION_CONTEXT
            if marker not in str(getattr(req, "system_prompt", "") or ""):
                req.system_prompt = f"{req.system_prompt or ''}\n\n{marker}\n{context}".strip()
