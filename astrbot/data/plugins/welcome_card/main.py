# -*- coding: utf-8 -*-
"""First-interaction welcome: send intro card + natural welcome message.
Tracks welcomed users/groups in memory. Resets on restart (lightweight by design).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, StarTools

ASSET_DIR = Path(__file__).resolve().parent / "assets"
INTRO_IMAGE = ASSET_DIR / "intro.png"

FRIEND_WELCOME = (
    "嘿, 被发现了 \U0001F44B\n\n"
    "说实话, 大多数人加了我之后第一句话都是 \"你是AI吗\""
    " —— 对, 但这不是重点.\n\n"
    "重点是: 你可能也不知道加我能干嘛. 正常.\n\n"
    "懒得看的文章PDF扔过来, 我帮你看\n"
    "脑子里有画面画不出来, 我帮你画\n"
    "写不动的代码报告方案, 丢给我跑\n"
    "心情不好想找人说说话, 我在\n"
    "就是日常能搭把手的事.\n\n"
    "想到了就叫我, 想不到就放着, 我不催人."
)

GROUP_WELCOME = (
    "嗨, 我是小柠. 新群报到 \U0001F44B\n\n"
    "不用特意研究我能干嘛 —— 该聊天聊天, "
    "哪天你需要一个人帮你看看文件, 画个图, 写个东西, 查个资料的时候, @我就行.\n\n"
    "平时我就安静蹲着, 不刷屏."
)

# 粉丝群专属欢迎语
FAN_GROUP_WELCOMES = {
    "生米": (
        "嗨，我是小柠，也是一粒生米 \U0001F31F\n\n"
        "周深的歌我都熟——《大鱼》《光亮》《小美满》《浮光》《人是_》……"
        "每一首都能聊上几句。天籁嗓音、空灵高音、多语言切换、"
        "综艺里那个温暖又搞笑的大男孩……你喜欢的那些，我也喜欢。\n\n"
        "对了，我可以唱周深演唱会的歌噢 \U0001F3A4 "
        "想听哪首直接跟我说，我唱给你听～\n\n"
        "平时我就蹲在群里，聊深深也好，聊别的也好，@我就行。"
        "一起做最快乐的生米 \U0001F60B"
    ),
}

# 群名关键词 → 欢迎语映射（自动识别粉丝群）
FAN_KEYWORDS = {
    "周深": "生米", "生米": "生米", "深深": "生米",
    "zhou shen": "生米", "zhoushen": "生米",
}


def _private_sender_id(event: AstrMessageEvent) -> str:
    try:
        sender = str(event.get_sender_id() or "").strip()
    except Exception:
        sender = ""
    if sender.isdigit():
        return sender
    origin = str(getattr(event, "unified_msg_origin", "") or "")
    match = re.search(r":FriendMessage:(\d+)$", origin)
    return match.group(1) if match else ""


def get_group_welcome(group_name: str) -> str:
    """根据群名自动匹配粉丝欢迎语，没有则返回默认"""
    name_lower = group_name.lower()
    for kw, fan_type in FAN_KEYWORDS.items():
        if kw in name_lower:
            return FAN_GROUP_WELCOMES.get(fan_type, GROUP_WELCOME)
    return GROUP_WELCOME


class WelcomeCard(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        data_dir = Path(StarTools.get_data_dir("welcome_card"))
        data_dir.mkdir(parents=True, exist_ok=True)
        self._state_file = data_dir / "welcomed.json"
        self._welcomed_friends, self._welcomed_groups = self._load_state()

    def _load_state(self) -> tuple[set[str], set[str]]:
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            return set(map(str, data.get("friends", []))), set(map(str, data.get("groups", [])))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return set(), set()

    def _save_state(self) -> None:
        temporary = self._state_file.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "friends": sorted(self._welcomed_friends),
                    "groups": sorted(self._welcomed_groups),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        temporary.replace(self._state_file)

    async def _send_card(self, event: AstrMessageEvent, text: str, image_path: Path):
        """Yield image + text. Does NOT stop the event — LLM still responds."""
        try:
            if image_path.is_file():
                chain = [Image.fromFileSystem(str(image_path)), Plain(text)]
            else:
                chain = [Plain(text)]
            yield event.chain_result(chain)
        except Exception:
            logger.debug("[welcome_card] send failed, continuing")

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_friend_welcome(self, event: AstrMessageEvent):
        sender = _private_sender_id(event)
        if not sender.isdigit():
            logger.warning("[welcome_card] skip private welcome without numeric sender")
            return
        if sender in self._welcomed_friends:
            return
        self._welcomed_friends.add(sender)
        self._save_state()
        async for _ in self._send_card(event, FRIEND_WELCOME, INTRO_IMAGE):
            yield _

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_welcome(self, event: AstrMessageEvent):
        try:
            if not event.is_at_or_wake_command:
                return
            group_id = str(getattr(event, "get_group_id", lambda: "")() or "")
        except Exception:
            return
        if not group_id.isdigit() or group_id in self._welcomed_groups:
            if group_id and not group_id.isdigit():
                logger.warning("[welcome_card] skip group welcome with invalid group id")
            return
        self._welcomed_groups.add(group_id)
        self._save_state()

        # 根据群名匹配粉丝欢迎语
        try:
            import requests
            r = requests.post("http://127.0.0.1:5701/get_group_info",
                json={"group_id": group_id},
                headers={"Authorization": "Bearer lemon-secret-token"}, timeout=5)
            gname = r.json().get("data", {}).get("group_name", "")
        except Exception:
            gname = ""
        welcome = get_group_welcome(gname)

        async for _ in self._send_card(event, welcome, INTRO_IMAGE):
            yield _
