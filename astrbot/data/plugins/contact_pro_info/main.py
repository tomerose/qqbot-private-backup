# -*- coding: utf-8 -*-
"""Public help, tier and contact replies backed by implemented features.

Long Chinese text is loaded from external UTF-8 .txt files to avoid encoding issues
that can occur when writing Chinese characters through certain tools.
"""

import os
import re

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Star
try:
    from xiaoning_runtime import defer_stop_event
except ImportError:
    from data.plugins.xiaoning_runtime import defer_stop_event
try:
    from xiaoning_capabilities import capability_prompt_block
except ImportError:
    from data.plugins.xiaoning_capabilities import capability_prompt_block


# ---- Load long Chinese text from external UTF-8 files ----

def _load_text(filename):
    """Load Chinese text from a UTF-8 file next to this plugin."""
    path = os.path.join(os.path.dirname(__file__), filename)
    with open(path, 'r', encoding='utf-8') as f:
        return f.read().strip()


USER_GUIDE = _load_text('user_guide.txt')
CAPABILITY_MEMORY = _load_text('capability_memory.txt')
CAPABILITY_CATALOG_MEMORY = capability_prompt_block()

CONTACT_REPLY = (
    "联系邮箱：portelamicheli636@gmail.com。"
    "邮件里说明用途和想咨询的内容即可。"
)

PRO_APPLICATION_GUIDE = (
    "【小柠资格指南】\n"
    "添加小柠为 QQ 好友即可自动获得 X资格（无需口令，系统自动检测）。\n"
    "Pro 资格通过邀请码开通：/redeem <邀请码>。\n"
    "发送 /pro status 可查看当前资格。"
)

CONVERSATIONAL_HELP_REPLY = (
    "能陪你聊，也能真动手：查资料、读文件、画图、做网页、报告和视频都行。"
    "别研究命令，直接说眼下想搞定什么；比如“帮我比较 A 和 B”，我会自己接到合适的功能。"
)

VERSION_REPLY = (
    "【小柠版本说明】\n"
    "普通版：聊天、识图、语音、情绪倾听、"
    "实时搜索/地图/计算、GitHub与B站工具、"
    "翻译、链接与文档分析、时间胶囊、狼人杀、"
    "欢迎卡片、消息回应、作图 1次/天。\n"
    "X资格（添加QQ好友自动获得）：普通版全部 + "
    "个人长期记忆、深度思考、私聊增强对话、文件交付、"
    "AI 早报订阅、AI 辩论 3次/天、模拟面试 3次/天、"
    "文档/PDF分析 5次/天、网页工坊 1次/天（最多3个）、"
    "搜索行动包 3次/天（文件返回QQ）、Agent 1次/周、"
    "作图 1次/天、AI 视频 1次/天、视频Agent 1次/天、视频工坊 1次/天。\n"
    "Pro（邀请制）：X资格全部 + 网页工坊 5次/天（最多20个并独立复核）+ "
    "行动包 10次/天并独立复核 + 作图 10次/天 + 定制图 1次/天 + "
    "AI 视频 Veo全功能 1次/月 + 视频Agent/视频工坊各 5次/天 + "
    "音乐生成 + Agent 不限次数 + 辩论/面试/文档无限。\n"
    "添加小柠为QQ好友自动获得X资格；Pro 邀请码仍有效；安全限制始终有效。"
)

_GUIDE_TEXTS = {
    "帮助", "help", "/help",
    "小柠使用指南", "使用指南", "新手帮助",
    "功能列表", "全部命令", "所有命令",
}
_CONVERSATIONAL_HELP_TEXTS = {
    "小柠怎么用", "小柠能干嘛", "小柠能做什么", "小柠有什么功能", "小柠功能",
    "你能干嘛", "你能做什么", "你会什么", "你可以干什么", "你有什么功能",
}
_FEATURE_HELP = (
    (("深度研究",), "深度研究适合需要多轮检索、交叉核验再出完整报告的题，Pro 可用。直接说“帮我深度研究大学生就业趋势”，做好后报告会发回 QQ。"),
    (("行动包", "比较决策", "旅行规划"), "行动包会把研究、比较或行程做成可执行报告。直接说“帮我比较 A 和 B”或“帮我规划杭州三天行程”就行。"),
    (("网页工坊", "做网页", "制作网页"), "网页工坊会直接做出能操作的网页，并返回预览、HTML 和公开链接。你可以说“帮我做一个记账网页”。"),
    (("去水印", "改图", "编辑图片", "重画"), "会。原图和要求可以一起发，也可以相邻两条发；例如回复图片说“去水印，抹掉右下角的字”。只有出现任务已开始才代表真正处理，QQ 收到新图片才算完成。"),
    (("画图", "作图", "绘图", "图片生成"), "会，直接把画面说清楚就行，比如“帮我画一只雨夜霓虹下的黑猫”。需要改图就回复原图说要改哪里。"),
    (("视频", "短片", "动画"), "原创 AI 片段说“帮我生成一段海边日落视频”；完整脚本、素材、配音短片说“帮我做一段如何在家做拿铁的视频”；高质量模板评分版说“帮我做一个高质量的拿铁科普视频”；找现成视频说“帮我找周杰伦现场视频”。四条链路互不抢任务。"),
    (("圆桌辩论", "圆桌讨论", "ai辩论", "辩论"), "圆桌适合把一个问题拆成多种立场。直接说“圆桌讨论 AI 会不会降低人的能力”就会开始。"),
    (("模拟面试", "ai面试"), "模拟面试会连续追问并给反馈。直接说“帮我模拟产品经理面试”，我就按真实面试开始。"),
    (("agent", "文件交付", "生成报告"), "要报告、Word、PDF 或表格成品时，直接说清楚要什么文件和内容，比如“帮我做一份暑假计划 Word”。成品会作为 QQ 文件返回。"),
    (("实时搜索", "查资料", "搜索"), "需要最新资料时直接说“帮我查一下……”；如果想要完整报告，就说“帮我深度研究……”。"),
    (("翻译",), "把原文和目标语言一起发来就行，比如“把这段话翻译成英文”，不用背命令。"),
    (("原创歌曲", "写歌", "唱歌", "音乐生成"), "想点现成歌曲就说歌名和歌手；想生成原创歌就描述主题、风格和情绪，我会自动分开处理。"),
    (("长期记忆", "记忆功能"), "你明确说出的长期偏好、计划和稳定事实可以被记住；一次性命令和文件任务不会当成长期记忆。"),
    (("任务追踪", "跨对话任务", "任务进度"), "X/PRO 的 Agent、改图/去水印、网页、研究报告、音乐和视频任务都会按真实状态跨对话追踪。说“任务进度怎么样”可查询；只有真实输出并成功交付后才会标记完成，催促或口头承诺不会改状态。"),
)
_META_QUESTION_WORDS = (
    "怎么用", "如何用", "如何使用", "用法", "有什么用", "能做什么", "能干什么",
    "是干嘛的", "是做什么的", "怎么玩", "支持什么",
)
_CAPABILITY_PREFIXES = ("能不能", "可不可以", "会不会", "能否", "是否", "有没有", "能", "会", "可以", "支持")
_TASK_REQUEST_PREFIXES = (
    "帮我", "给我", "替我", "帮忙", "我要", "我想", "麻烦", "能帮我", "可以帮我", "可不可以帮我",
    "请帮", "请给", "请做", "请生成", "请制作",
)
_SHEN_GROUP_ID = "1058848055"  # 周深粉丝群：不透露小柠会员体系

_TIER_SUBJECTS = {"pro", "普通版", "会员", "版本", "资格", "x资格", "go", "go资格", "x", "pro资格"}
_TIER_INTENTS = {"是什么", "区别", "功能", "权限",
                 "升级", "多少钱", "收费", "付费"}
_CONTACT_WORDS = {"联系", "联系方式", "邮箱", "找"}
_CONTACT_TARGETS = {"作者", "老板", "管理员", "拥有者", "负责人"}
_ACQUISITION_WORDS = {"获取", "开通", "申请", "资格", "怎么拿", "如何"}


def _normalized(text: str) -> str:
    return "".join(str(text or "").lower().split())


def _normalized_query(text: str) -> str:
    value = _normalized(text).rstrip("。！!？?")
    return re.sub(r"(?:吗|嘛|呢|啊|呀)$", "", value)


def _match_help(text: str) -> bool:
    return _normalized_query(text) in _GUIDE_TEXTS


def _match_conversational_help(text: str) -> bool:
    return _normalized_query(text) in _CONVERSATIONAL_HELP_TEXTS


def feature_help_for(text: str) -> str | None:
    """Explain a named feature only for a clear meta/capability question."""
    value = re.sub(r"[，,：:]", "", _normalized_query(text))
    if not value:
        return None
    candidate = value
    for subject in ("小柠", "柠柠", "你"):
        if candidate.startswith(subject):
            candidate = candidate[len(subject):]
            break
    task_candidate = candidate.removeprefix("请问")
    if task_candidate.startswith(_TASK_REQUEST_PREFIXES) or (
        task_candidate.startswith("请") and not task_candidate.startswith("请问")
    ):
        return None
    for keywords, reply in _FEATURE_HELP:
        if not any(keyword in value for keyword in keywords):
            continue
        if any(word in value for word in _META_QUESTION_WORDS):
            return reply
        for prefix in _CAPABILITY_PREFIXES:
            if not candidate.startswith(prefix):
                continue
            remainder = candidate[len(prefix):]
            for bridge in ("帮我", "使用", "用", "做", "制作", "生成"):
                if remainder.startswith(bridge):
                    remainder = remainder[len(bridge):]
                    break
            if remainder in keywords:
                return reply
        return None
    return None


def _match_tier(text: str) -> bool:
    value = _normalized(text)
    return any(item in value for item in _TIER_SUBJECTS) and any(
        item in value for item in _TIER_INTENTS
    )


def _match_contact(text: str) -> bool:
    value = _normalized(text)
    return any(item in value for item in _CONTACT_WORDS) and any(
        item in value for item in _CONTACT_TARGETS
    )


def _match_acquisition(text: str) -> bool:
    value = _normalized(text)
    return ("pro" in value or "x" in value or "go" in value) and any(
        item in value for item in _ACQUISITION_WORDS
    )


def version_reply_for(text: str) -> str | None:
    if _match_conversational_help(text):
        return CONVERSATIONAL_HELP_REPLY
    return VERSION_REPLY if _match_help(text) or _match_tier(text) else None


def contact_reply_for(text: str) -> str | None:
    if _match_acquisition(text):
        return PRO_APPLICATION_GUIDE
    return CONTACT_REPLY if _match_contact(text) else None


class ContactProInfo(Star):
    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=990)
    @defer_stop_event
    async def on_message(self, event: AstrMessageEvent):
        text = str(getattr(event, "get_message_str", lambda: "")() or "").strip()
        if not text:
            return

        # In group chats: require explicit @小柠 or "小柠" mention to avoid false triggers
        is_private = bool(getattr(event, "is_private_chat", lambda: False)())
        is_at = bool(getattr(event, "is_at_or_wake_command", False))
        has_xiaoning = bool(re.search(r"小柠|柠柠|xiao\s*ning", text, re.I))
        if not is_private and not is_at and not has_xiaoning:
            return

        # 周深粉丝群：不透露小柠会员体系（版本区别/资格/开通方式）
        group_id = str(getattr(event, "get_group_id", lambda: "")() or "")
        if group_id == _SHEN_GROUP_ID and (_match_tier(text) or _match_acquisition(text)):
            return

        feature_reply = feature_help_for(text)
        if feature_reply:
            reply = feature_reply
        elif _match_help(text):
            reply = USER_GUIDE
        elif _match_conversational_help(text):
            reply = CONVERSATIONAL_HELP_REPLY
        elif _match_acquisition(text):
            reply = PRO_APPLICATION_GUIDE
        elif _match_tier(text):
            reply = VERSION_REPLY
        elif _match_contact(text):
            reply = CONTACT_REPLY
        else:
            return
        event.stop_event()
        yield event.plain_result(reply)

    @filter.on_llm_request(priority=-17)
    async def inject_capability_memory(self, event: AstrMessageEvent, req) -> None:
        system_prompt = str(getattr(req, "system_prompt", "") or "")
        if "【公开能力事实】" not in system_prompt:
            req.system_prompt = (
                f"{system_prompt}\n\n{CAPABILITY_MEMORY}\n\n{CAPABILITY_CATALOG_MEMORY}"
            ).strip()
