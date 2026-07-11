"""Reply with the approved public contact for author/boss and Pro requests."""

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Star


CONTACT_REPLY = (
    "咕咕嘎嘎～联系邮箱：portelamicheli636@gmail.com。"
    "邮件里说明你的情况、用途和想咨询的内容就行。"
)

VERSION_REPLY = (
    "【小柠版本说明】\n"
    "普通版：可以聊天、答疑、总结翻译、情绪陪伴、语音交流、设置提醒、查询公开 GitHub 信息，"
    "并在小柠拥有群权限时执行禁言、撤回、公告等群管理。\n"
    "Pro 版：包含普通版全部功能，另外支持 AI 作图、生成 Word 和其他文件、读取和修改项目、"
    "运行测试、整理公开 GitHub 资料，并把完成的文件直接发到群里。任务开始前会提示预计时间。\n"
    "安全限制：不会读取密码、私聊记录、通讯录或系统隐私，也不会踢人或未经确认向外发送消息。\n"
    "想了解或申请 Pro，可以联系：portelamicheli636@gmail.com"
)


def contact_reply_for(text: str) -> str | None:
    normalized = "".join(str(text or "").lower().split())
    contact = any(word in normalized for word in ("联系", "联系方式", "邮箱", "找"))
    target = any(word in normalized for word in ("作者", "老板"))
    pro_acquisition = "pro" in normalized and any(
        word in normalized
        for word in ("获取", "开通", "申请", "资格", "怎么拿", "如何")
    )
    return CONTACT_REPLY if (contact and target) or pro_acquisition else None


def version_reply_for(text: str) -> str | None:
    normalized = "".join(str(text or "").lower().split())
    comparison = (
        ("普通版" in normalized and "pro" in normalized)
        or "普通版功能" in normalized
        or "pro版功能" in normalized
        or "版本区别" in normalized
        or "有什么功能" in normalized
        or "能做什么" in normalized
    )
    return VERSION_REPLY if comparison else None


class ContactProInfo(Star):
    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=950)
    async def on_message(self, event: AstrMessageEvent):
        reply = version_reply_for(event.get_message_str()) or contact_reply_for(
            event.get_message_str()
        )
        if reply is None:
            return
        event.stop_event()
        yield event.plain_result(reply)
