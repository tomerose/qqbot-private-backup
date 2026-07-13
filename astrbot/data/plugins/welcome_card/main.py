# -*- coding: utf-8 -*-
"""First-interaction welcome: send intro card + natural welcome message.
Tracks welcomed users/groups in memory. Resets on restart (lightweight by design).
"""
from __future__ import annotations

import json
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
    " —— 对, 租的云服务器跑的. 但这不是重点.\n\n"
    "重点是: 你可能也不知道加我能干嘛. 正常. 我给你几个实在的 ——\n\n"
    "懒得看的文章PDF扔过来, 我帮你看\n"
    "脑子里有画面画不出来, 我帮你画\n"
    "写不动的代码报告方案, 丢给我跑\n"
    "心情不好想找人说说话, 我在\n\n"
    "不是什么功能清单, 就是日常能搭把手的事. "
    "想到了就叫我, 想不到就放着, 我不催人."
)

GROUP_WELCOME = (
    "嗨, 我是小柠. 新群报到 \U0001F44B\n\n"
    "不用特意研究我能干嘛 —— 该聊天聊天, "
    "哪天你需要一个人帮你看看文件, 画个图, 写个东西, 查个资料的时候, @我就行.\n\n"
    "平时我就安静蹲着, 不刷屏."
)


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
        try:
            sender = event.get_sender_id()
        except Exception:
            return
        sender = str(sender or "").strip()
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
        async for _ in self._send_card(event, GROUP_WELCOME, INTRO_IMAGE):
            yield _
