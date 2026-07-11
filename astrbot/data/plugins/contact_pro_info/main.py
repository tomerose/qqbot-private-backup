"""Reply with the approved public contact for author/boss and Pro requests."""

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Star


CONTACT_REPLY = (
    "咕咕嘎嘎～联系邮箱：portelamicheli636@gmail.com。"
    "邮件里说明你的情况、用途和想咨询的内容就行。"
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


class ContactProInfo(Star):
    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=950)
    async def on_message(self, event: AstrMessageEvent):
        reply = contact_reply_for(event.get_message_str())
        if reply is None:
            return
        event.stop_event()
        yield event.plain_result(reply)

