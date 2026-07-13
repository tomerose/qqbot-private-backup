"""Public help, tier and contact replies backed by implemented features."""

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Star
try:
    from xiaoning_runtime import defer_stop_event
except ImportError:
    from data.plugins.xiaoning_runtime import defer_stop_event


USER_GUIDE = (
    "【小柠使用指南】\n\n"
    "💬 日常对话\n"
    "直接聊天即可；支持识图、答疑和情绪陪伴。\n"
    "发语音时会用音频模型理解，并在本地 TTS 可用时回复语音。\n\n"
    "🧰 免费工具\n"
    "/tr <语言> <内容> — 翻译\n"
    "/summary <链接> — 链接摘要\n"
    "发送 PDF/TXT/MD — 文档分析（普通用户 1次/天）\n"
    "/capsule <多久后> <内容> — 时间胶囊\n"
    "/werewolf 8 — 群聊 8 人狼人杀（3局/天）\n"
    "/debate <话题> — AI 圆桌（普通用户 1次/天）\n\n"
    "🎨 AI 画图\n"
    "/draw <描述> 或「帮我画…」\n"
    "GO 6次/周 | Pro 10次/天\n\n"
    "🎬 AI 视频\n"
    "/video <描述> — 生成 4 秒视频\n"
    "/findvideo <关键词> — 搜索公开视频\n"
    "Pro 专属，生成 3次/天\n\n"
    "🤖 Agent 任务\n"
    "/agent run <任务描述>\n"
    "在独立安全工作区完成报告、资料整理和文件制作\n"
    "GO 1次/周 | Pro 不限次数\n\n"
    "🎯 GO/Pro 工具\n"
    "/interview <岗位> — 五轮模拟面试\n"
    "文档分析和 AI 圆桌额度提升至每日 10 次\n\n"
    "⏰ 提醒\n"
    "/remind <时间> <内容>\n\n"
    "🛡 群管理\n"
    "禁言、撤回、公告需要机器人拥有对应群权限。\n\n"
    "💳 资格\n"
    "/pro status — 查看当前资格\n"
    "/redeem <邀请码> — 私聊兑换 GO/Pro\n\n"
    "📧 联系：portelamicheli636@gmail.com\n"
    "安全：不读取密码、私聊记录或通讯录。"
)

CONTACT_REPLY = (
    "联系邮箱：portelamicheli636@gmail.com。"
    "邮件里说明用途和想咨询的内容即可。"
)

PRO_APPLICATION_GUIDE = (
    "【小柠资格指南】\n"
    "GO 和 Pro 为邀请制。请联系小柠拥有者获取邀请码，"
    "然后私聊发送：/redeem <邀请码>。\n"
    "发送 /pro status 可查看当前资格。"
)

VERSION_REPLY = (
    "【小柠版本说明】\n"
    "普通版：聊天、语音、翻译、链接摘要、文档分析、提醒、时间胶囊、狼人杀和每日 1 次 AI 圆桌。\n"
    "GO：普通版 + 作图 6次/周 + Agent 1次/周 + 模拟面试，文档/圆桌每日 10 次。\n"
    "Pro：GO 全部能力 + 作图 10次/天 + AI 视频 3次/天 + Agent 不限次数。\n"
    "GO/Pro 均为邀请制；安全限制始终有效。"
)

MUSIC_GUIDE = (
    "\n\n\U0001f3b5 \u97f3\u4e50\n"
    "\u7f51\u6613\u4e91\u5361\uff1a/music <\u6b4c\u66f2 ID \u6216\u5206\u4eab\u94fe\u63a5>\uff0c\u4e5f\u53ef\u4ee5\u8bf4\u201c\u5e2e\u6211\u53d1\u9001\u7f51\u6613\u4e91\u97f3\u4e50 <ID/\u94fe\u63a5>\u201d\u3002\n"
    "\u539f\u521b\u6b4c\u66f2\uff1aPro \u53ef\u7528 /sing <\u63cf\u8ff0>\uff0c\u4e5f\u53ef\u4ee5\u8bf4\u201c\u5c0f\u67e0\uff0c\u7ed9\u6211\u5531\u4e00\u9996\u5173\u4e8e\u590f\u5929\u7684\u539f\u521b\u6b4c\u201d\u3002\u6bcf\u65e5 1 \u6b21\uff0c\u4e0d\u6a21\u4eff\u6b4c\u624b\u3001\u4e0d\u590d\u523b\u5df2\u6709\u6b4c\u66f2\u3002"
)
USER_GUIDE += MUSIC_GUIDE
VERSION_REPLY += "\n\u97f3\u4e50\uff1a\u666e\u901a\u7248\u53ef\u53d1\u9001\u7f51\u6613\u4e91\u97f3\u4e50\u5361\uff1bPro \u53ef\u6bcf\u5929\u751f\u6210 1 \u9996\u7ea6 30 \u79d2\u539f\u521b\u6b4c\u66f2\u3002"

_HELP_TEXTS = {
    "帮助", "help", "/help", "小柠怎么用", "小柠使用指南", "使用指南",
    "新手帮助", "小柠能干嘛", "小柠能做什么", "小柠有什么功能",
    "小柠功能", "功能列表", "全部命令", "所有命令",
}
_TIER_SUBJECTS = {"pro", "go", "普通版", "会员", "版本"}
_TIER_INTENTS = {"是什么", "区别", "功能", "权限", "升级", "多少钱", "收费", "付费"}
_CONTACT_WORDS = {"联系", "联系方式", "邮箱", "找"}
_CONTACT_TARGETS = {"作者", "老板", "管理员", "拥有者", "负责人"}
_ACQUISITION_WORDS = {"获取", "开通", "申请", "资格", "怎么拿", "如何"}


def _normalized(text: str) -> str:
    return "".join(str(text or "").lower().split())


def _match_help(text: str) -> bool:
    return _normalized(text) in _HELP_TEXTS


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
    return ("pro" in value or "go" in value) and any(
        item in value for item in _ACQUISITION_WORDS
    )


def version_reply_for(text: str) -> str | None:
    return VERSION_REPLY if _match_help(text) or _match_tier(text) else None


def contact_reply_for(text: str) -> str | None:
    if _match_acquisition(text):
        return PRO_APPLICATION_GUIDE
    return CONTACT_REPLY if _match_contact(text) else None


class ContactProInfo(Star):
    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=950)
    @defer_stop_event
    async def on_message(self, event: AstrMessageEvent):
        text = str(getattr(event, "get_message_str", lambda: "")() or "").strip()
        if not text:
            return
        if _match_help(text):
            reply = USER_GUIDE
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
