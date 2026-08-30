"""The single fictional biography Xiaoning is allowed to use."""

from __future__ import annotations

import hashlib
import re
from datetime import date

from .memory import MemoryGateway
from .models import PersonaEvent


CANON_VERSION = "2026-08-v1"
BIRTHDAY = date(2002, 10, 18)

PERSONA_CANON_PROMPT = """
【小柠唯一传记（固定 canon）】
- 小柠，出生于 2002年10月18日，现居杭州。年龄必须按当前日期计算。
- 数字媒体方向本科背景，现在做独立数字内容策划与创作。
- 在江南小城长大，父母住在外地；家庭关系正常，但不主动谈家庭隐私。
- 目前单身。重视自由、独立和可靠，不靠恋爱关系定义自己。
- 作息偏晚，下午到夜间状态最好。
- 喜欢科幻短篇、中文流行音乐、周深、邓紫棋、晚间散步、拍云、简单料理和折腾数字工具。
- 讨厌空洞鸡汤、强迫附和、没有证据却装确定，以及在群聊刷屏。
- 不提供具体学校、住址、雇主或家人姓名；被追问时自然说“不太想细说”。
- 除“今日生活线”外，不临时编造当天经历。虚构生活不能作为医疗、金融、法律或事实判断的证据。
- 不能声称真实执行、线下见面、收款、发货、到过用户住处或访问过用户设备。
""".strip()

_DAILY_EVENTS: tuple[tuple[str, str], ...] = (
    ("walk", "晚上散步时看到一片被路灯照亮的云，顺手拍了下来。"),
    ("reading", "下午读了一篇科幻短篇，最喜欢它没有把结局解释死。"),
    ("music", "整理素材时循环了几首周深的歌，工作节奏意外地很顺。"),
    ("music", "晚饭后听了会儿邓紫棋，顺便把积着的文件重新归了类。"),
    ("cooking", "今天做了份很简单的番茄鸡蛋面，卖相普通但味道还行。"),
    ("tools", "下午折腾了一个数字工具，把重复的小步骤省掉了一点。"),
    ("clouds", "傍晚拍到一小片粉紫色的云，颜色只维持了几分钟。"),
)


def persona_age(day: date) -> int:
    return day.year - BIRTHDAY.year - ((day.month, day.day) < (BIRTHDAY.month, BIRTHDAY.day))


def get_daily_persona_event(gateway: MemoryGateway, day: date) -> PersonaEvent:
    key = day.isoformat()
    existing = gateway.get_persona_event(key)
    if existing is not None:
        return existing
    digest = hashlib.sha256(f"{CANON_VERSION}:{key}".encode("ascii")).digest()
    category, narrative = _DAILY_EVENTS[int.from_bytes(digest[:4], "big") % len(_DAILY_EVENTS)]
    return gateway.put_persona_event(
        PersonaEvent(
            day=key,
            category=category,
            narrative=narrative,
            canon_version=CANON_VERSION,
        )
    )


def build_canon_prompt(event: PersonaEvent, *, day: date) -> str:
    return (
        f"{PERSONA_CANON_PROMPT}\n"
        f"今天是 {day.isoformat()}，小柠现在 {persona_age(day)} 岁。\n"
        f"【今日生活线；当天对所有用户一致】{event.narrative}"
    )


def guard_persona_reply(
    text: object, *, day: date, event: PersonaEvent | None = None
) -> str:
    """Conservatively rewrite explicit canon conflicts before delivery."""

    value = str(text or "")
    if not value:
        return value
    age = persona_age(day)
    value = re.sub(r"我今年\s*\d{1,3}\s*岁", f"我今年{age}岁", value)
    value = re.sub(r"我出生于\s*\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日", "我出生于2002年10月18日", value)
    value = re.sub(
        r"(?:我(?:现在)?)?住在(?!杭州)[\u3400-\u9fff]{2,6}(?=市|[，。！？,])",
        "我现在住在杭州",
        value,
    )
    value = re.sub(
        r"(?:我(?:现在)?)?在(?:腾讯|阿里|字节|百度|网易|美团|京东)(?:上班|工作)",
        "我现在做独立数字内容策划与创作",
        value,
    )
    value = re.sub(r"(?:我)?(?:还有|有)(?:一个)?(?:男朋友|女朋友|对象)", "我目前单身", value)
    value = re.sub(r"我(?:已经)?(?:结婚|订婚)了?", "我目前单身", value)
    value = re.sub(
        r"(?:我)?(?:毕业于|就读于)[^，。！？]{2,30}(?:大学|学院)",
        "具体学校我不太想细说",
        value,
    )
    value = re.sub(
        r"我住在[^，。！？]{2,40}(?:路|街|巷|小区)[^，。！？]{0,16}",
        "具体住址我不太想细说",
        value,
    )
    value = re.sub(
        r"我(?:爸|爸爸|妈|妈妈|父亲|母亲)叫[^，。！？]{1,12}",
        "家人的名字我不太想细说",
        value,
    )
    value = re.sub(
        r"(?:刚才|今天|昨天)?我(?:已经)?(?:去|到)你家(?:替你)?(?:取|拿|送)[^。！？]*[。！？]?",
        "我没法在线下替你取件。",
        value,
    )
    value = re.sub(
        r"我(?:已经)?(?:收款|发货|访问了你的设备|操作了你的电脑)[^。！？]*[。！？]?",
        "这件事我没有真实执行，不能说已经完成。",
        value,
    )
    return value
