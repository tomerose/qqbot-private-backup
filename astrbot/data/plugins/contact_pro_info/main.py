"""Reply with the approved public contact for author/boss and Pro requests."""

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Star


CONTACT_REPLY = (
    "咕咕嘎嘎～联系邮箱：portelamicheli636@gmail.com。"
    "邮件里说明你的情况、用途和想咨询的内容就行。"
)

PRO_APPLICATION_GUIDE = (
    "【小柠 Pro 申请指南】\n"
    "1. 发送：/pro apply，获取申请编号。\n"
    "2. 72 小时内发邮件到 portelamicheli636@gmail.com；主题写【小柠 Pro 申请】+申请编号。\n"
    "3. 邮件正文写：QQ 号、用途、想使用的功能，以及同意安全规则。\n"
    "4. 回到小柠发送：/pro sent 申请编号，等待人工审核。\n"
    "5. 审核通过后，在原 QQ 私聊完成验证码验证即可开通。\n\n"
    "Pro 可使用 AI 作图、生成并交付 Word/文件、公开资料整理和 Agent 任务。"
    "安全限制始终有效：不读取密码、私聊或通讯录；不修改系统安全、权限策略或系统文件。"
)

VERSION_REPLY = (
    "【小柠版本说明】\n"
    "普通版：可以聊天、答疑、总结翻译、情绪陪伴、语音交流、设置提醒、查询公开 GitHub 信息，"
    "并在小柠拥有群权限时执行禁言、撤回、公告等群管理。\n"
    "Pro 版：包含普通版全部功能，另外支持 AI 作图、生成 Word 和其他文件、公开资料整理、"
    "Agent 任务与文件交付。任务开始前会提示预计时间。\n"
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
    if pro_acquisition:
        return PRO_APPLICATION_GUIDE
    return CONTACT_REPLY if contact and target else None


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
