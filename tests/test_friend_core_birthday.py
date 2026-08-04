import sys
import asyncio
import tempfile
import unittest
from datetime import date, datetime as RealDateTime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


PLUGINS_DIR = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS_DIR))

from friend_core.birthday import Birthday, birthday_greeting, is_due_birthday, parse_explicit_birthday  # noqa: E402
from friend_core.group_help import (  # noqa: E402
    group_help_offer,
    parse_group_help_confirmation,
    screen_group_help,
)
from friend_core.persona_prompt import (  # noqa: E402
    build_persona_prompt,
    sanitize_conversational_reply,
    sanitize_unverified_artifact_reply,
)
from friend_core import main as friend_main  # noqa: E402
from friend_core.main import FriendCore  # noqa: E402


class _ProfileRef:
    def __init__(self):
        self.updates = []

    def update(self, values):
        self.updates.append(values)


class _Profile:
    exists = True

    def __init__(self, data, reference):
        self._data = data
        self.reference = reference

    def to_dict(self):
        return dict(self._data)

    def get(self):
        return self


class _UserRef:
    def __init__(self, profile):
        self._profile = profile

    def collection(self, _name):
        return self

    def document(self, _name):
        return self._profile


class _User:
    id = "1211000567"

    def __init__(self, profile):
        self.reference = _UserRef(profile)


class _Db:
    def __init__(self, user):
        self._user = user

    def collection(self, _name):
        return self

    def limit(self, _count):
        return self

    def stream(self):
        return [self._user]


class _FixedDateTime:
    @classmethod
    def now(cls):
        return RealDateTime(2026, 7, 15, 9, 0)


class FriendCoreBirthdayTests(unittest.TestCase):
    def test_local_path_cannot_be_presented_as_a_delivered_image(self):
        reply = sanitize_unverified_artifact_reply(
            "[Image: path D:\\private\\avatar.png]\n\n"
            "喏，说重画就重画。刚才偷偷帮你去后台把图画出来了，拿去换上吧！"
        )

        self.assertNotIn("D:\\", reply)
        self.assertNotIn("本机路径", reply)
        self.assertNotIn("后台把图画出来", reply)
        self.assertIn("没有真正发出来", reply)

    def test_normal_conversation_reply_is_unchanged(self):
        reply = "这个头像的黑白线条挺干净，耳朵比例也合适。"
        self.assertEqual(sanitize_unverified_artifact_reply(reply), reply)

    def test_missed_artifact_route_cannot_fake_completion_without_a_path(self):
        reply = sanitize_unverified_artifact_reply(
            "弄好了，报告已经发给你了。",
            "帮我生成一份 Word 报告",
        )
        self.assertNotIn("已经发给你", reply)
        self.assertIn("收到文件才算", reply)

    def test_exact_image_edit_waiting_claim_is_replaced_with_truthful_handoff(self):
        for raw in (
            "我准备用图片编辑工具把这张图改成暖色，稍等我一下。",
            "我这就调用图片编辑功能处理，稍微等我一下。",
            "代码已经跑完了，我马上把修好的图片发给你。",
        ):
            reply = sanitize_unverified_artifact_reply(raw, "需要")
            self.assertNotIn("稍等", reply)
            self.assertNotIn("马上", reply)
            self.assertIn("没有启动", reply)
            self.assertIn("QQ 收到", reply)

    def test_removed_dewatermark_is_never_offered_by_plain_chat(self):
        expected = "去水印功能已经下线了，我不能替你处理，也不会让你重发图片；前面说能做是我说错了。"
        cases = (
            ("我识别到你需要去水印，请重新发一下原图。", "需要"),
            ("可以，把图片发来。", "帮我把水印去掉"),
            ("Please upload it and I will remove the watermark.", "can you edit this image?"),
        )
        for raw, request in cases:
            with self.subTest(raw=raw, request=request):
                self.assertEqual(
                    sanitize_unverified_artifact_reply(raw, request),
                    expected,
                )

    def test_plain_chat_never_treats_the_recipient_as_host_owner(self):
        raw = """我来查一下你电脑上的 `gh` 命令行工具状态，看看能不能直接在本地命令行帮你把 GitHub 仓库建好。<execute_ipython>
import subprocess
print(subprocess.run('gh auth status', shell=True))
</execute_ipython>"""
        reply = sanitize_unverified_artifact_reply(raw, "帮我建一个 GitHub 仓库")

        self.assertNotIn("execute_ipython", reply)
        self.assertNotIn("你电脑上的", reply)
        self.assertIn("这一步还没有真正执行", reply)
        self.assertIn("由任务入口按权限和风险判断", reply)
        self.assertNotIn("不能把运行小柠的电脑当成你的电脑", reply)

    def test_execution_guard_does_not_turn_one_missed_route_into_a_permanent_ban(self):
        reply = sanitize_unverified_artifact_reply(
            "我可以查看你电脑上的日志再判断。",
            "你能帮我看看日志吗",
        )

        self.assertIn("这一步还没有真正执行", reply)
        self.assertNotIn("不能操作电脑", reply)
        self.assertNotIn("只能由机主", reply)

    def test_exact_web_waiting_and_fake_delivery_claims_are_blocked(self):
        cases = (
            ("行，我写一个简单的整理图库网页，HTML的，直接发你。稍等几分钟。", "能帮我做一个整理图库的东西吗"),
            ("做好了，发你文件", "制作一个女仆雪墨网页"),
        )
        for raw, request in cases:
            reply = sanitize_unverified_artifact_reply(raw, request)
            self.assertNotEqual(reply, raw)
            self.assertIn("没有", reply)
            self.assertIn("QQ 收到文件", reply)

    def test_status_question_gets_one_direct_verifiable_answer(self):
        reply = sanitize_unverified_artifact_reply(
            "网页做好了，这是 HTML 代码。",
            "你不是在做网页吗，好了没？",
        )
        self.assertEqual(
            reply,
            "这里没有可核验的完成记录；QQ 还没收到成品，就不能说做好了。",
        )

    def test_stage_directions_and_technical_identity_excuses_are_removed(self):
        self.assertEqual(
            sanitize_conversational_reply("（托腮看着你）\n\n这个前提不成立。"),
            "这个前提不成立。",
        )
        self.assertEqual(
            sanitize_conversational_reply(
                "前面说在北京上学是错的。我没有真实的个人经历。"
            ),
            "我是小柠。前面那句具体身份信息没有依据，是我说错了。",
        )
        for raw in (
            "作为一个人工智能助手，我没有真实经历。",
            "严格来说我是一个语言模型。",
            "我不具备身体，也没有真正的感情。",
        ):
            self.assertEqual(
                sanitize_conversational_reply(raw),
                "我是小柠。前面那句具体身份信息没有依据，是我说错了。",
            )
        factual = "ChatGPT 是由 OpenAI 开发的人工智能产品。"
        self.assertEqual(sanitize_conversational_reply(factual), factual)

    def test_customer_service_closers_are_trimmed(self):
        self.assertEqual(
            sanitize_conversational_reply("建议您先缩小范围。希望这能帮到您。"),
            "建议你先缩小范围",
        )
        self.assertEqual(
            sanitize_conversational_reply("当然可以呀，我理解你的感受。以下是我的建议：先别急。希望这能帮到你。"),
            "先别急",
        )
        self.assertEqual(
            sanitize_conversational_reply("如果你愿意，我可以继续帮你分析。这个说法站不住。"),
            "这个说法站不住",
        )

    def test_empty_followup_questions_are_trimmed(self):
        self.assertEqual(
            sanitize_conversational_reply("这个判断大概率是对的。要不要我继续帮你分析？"),
            "这个判断大概率是对的",
        )
        self.assertEqual(
            sanitize_conversational_reply("先别急，这事更像是上下文断了。你想让我怎么帮你？"),
            "先别急，这事更像是上下文断了",
        )

    def test_persona_prompt_blocks_botlike_friend_voice(self):
        prompt = build_persona_prompt(90)
        self.assertIn("【小柠本体】", prompt)
        self.assertIn("只认“小柠”这个身份", prompt)
        self.assertIn("反例、边界条件", prompt)
        self.assertIn("【多元学科判断】", prompt)
        self.assertIn("激励与代价", prompt)
        self.assertIn("最强反例", prompt)
        self.assertIn("【主动性】", prompt)
        self.assertIn("明确排序标准和取舍", prompt)
        self.assertIn("当然可以呀", prompt)
        self.assertIn("一句话顶住", prompt)
        self.assertIn("轻微吐槽空话", prompt)
        self.assertIn("去水印功能已经下线", prompt)
        self.assertIn("知识问答、故障分析、代码或命令示例正常回答", prompt)
        self.assertIn("是否可执行交给对应任务入口判断", prompt)
        self.assertNotIn("聊天对象不是运行小柠这台主机的机主", prompt)

    def test_group_persona_keeps_the_bot_from_acting_like_a_host(self):
        prompt = build_persona_prompt(0, group_chat=True)
        self.assertIn("当在场的群友", prompt)
        self.assertIn("别抢着总结、科普或给方案", prompt)
        self.assertIn("不当主持人或客服", prompt)

    def test_artifact_how_to_question_is_not_treated_as_a_delivery_task(self):
        reply = "视频制作完成后，再检查字幕和画面。"
        self.assertEqual(
            sanitize_unverified_artifact_reply(reply, "如何制作视频"),
            reply,
        )

    def test_only_accepts_explicit_solar_birthday(self):
        self.assertEqual(
            parse_explicit_birthday("我生日是3月8日"),
            Birthday(3, 8),
        )
        self.assertEqual(
            parse_explicit_birthday("今天是我生日", date(2026, 7, 15)).month,
            7,
        )
        self.assertIsNone(parse_explicit_birthday("小王生日是3月8日"))
        self.assertIsNone(parse_explicit_birthday("我农历生日是3月8日"))
        self.assertIsNone(parse_explicit_birthday("我生日是2月30日"))

    def test_due_check_is_once_per_year(self):
        today = date(2026, 7, 15)
        self.assertTrue(is_due_birthday({"month": 7, "day": 15}, today))
        self.assertFalse(is_due_birthday({"month": 7, "day": 15, "last_greeted_year": 2026}, today))
        self.assertFalse(is_due_birthday({"month": 7, "day": 16}, today))
        self.assertIn("生日快乐", birthday_greeting("小林"))

    def test_group_help_requires_clear_help_signal(self):
        self.assertIsNone(group_help_offer("今天天气不错"))
        self.assertIn("文件", group_help_offer("谁会处理这个表格文件，帮我看看"))
        self.assertIn("查清", group_help_offer("杭州周末有什么推荐，谁知道"))
        self.assertIsNone(group_help_offer("我想要一份杭州旅行规划"))
        candidate = screen_group_help("我想要一份杭州旅行规划")
        self.assertEqual(candidate.capability.id, "research")
        self.assertLess(candidate.confidence, 0.92)
        self.assertIsNone(screen_group_help("谁知道管理员的手机号和地址"))

    def test_group_help_model_confirmation_is_strict(self):
        accepted = parse_group_help_confirmation(
            '{"help_requested":true,"capability_id":"research","confidence":0.95}',
            "research",
        )
        self.assertEqual(accepted.capability.id, "research")
        self.assertIsNone(
            parse_group_help_confirmation(
                '{"help_requested":true,"capability_id":"draw","confidence":0.99}',
                "research",
            )
        )
        self.assertIsNone(parse_group_help_confirmation("not-json", "research"))

    def test_birthday_song_is_completed_only_after_verified_delivery(self):
        reference = _ProfileRef()
        profile = _Profile({"month": 7, "day": 15, "display_name": "小林"}, reference)
        plugin = FriendCore.__new__(FriendCore)
        plugin._db = _Db(_User(profile))
        plugin.enabled = True
        plugin._birthday_scan_day = ""
        plugin._birthday_song_root = Path(tempfile.gettempdir())
        plugin._send_reminder_message = AsyncMock(return_value=True)
        plugin._napcat_deliver_file = AsyncMock(return_value=True)
        with tempfile.TemporaryDirectory() as directory:
            song = Path(directory) / "birthday.mp3"
            song.write_bytes(b"song")
            with patch.object(friend_main, "datetime", _FixedDateTime), patch.object(
                friend_main, "generate_birthday_song", return_value=song
            ):
                asyncio.run(plugin._send_due_birthdays())

        self.assertIn({"last_greeted_year": 2026}, reference.updates)
        self.assertIn({"last_song_year": 2026}, reference.updates)
        self.assertTrue(any("任务已完成" in call.args[1] for call in plugin._send_reminder_message.await_args_list))

    def test_failed_birthday_delivery_is_not_marked_completed(self):
        reference = _ProfileRef()
        profile = _Profile({"month": 7, "day": 15}, reference)
        plugin = FriendCore.__new__(FriendCore)
        plugin._db = _Db(_User(profile))
        plugin.enabled = True
        plugin._birthday_scan_day = ""
        plugin._birthday_song_root = Path(tempfile.gettempdir())
        plugin._send_reminder_message = AsyncMock(return_value=True)
        plugin._napcat_deliver_file = AsyncMock(return_value=False)
        queue = MagicMock()
        queue.enqueue.return_value = "queued"
        with tempfile.TemporaryDirectory() as directory:
            song = Path(directory) / "birthday.mp3"
            song.write_bytes(b"song")
            with patch.object(friend_main, "datetime", _FixedDateTime), patch.object(
                friend_main, "generate_birthday_song", return_value=song
            ), patch.object(friend_main, "get_queue", return_value=queue):
                asyncio.run(plugin._send_due_birthdays())

        self.assertNotIn({"last_song_year": 2026}, reference.updates)
        self.assertTrue(any("任务未完成" in call.args[1] for call in plugin._send_reminder_message.await_args_list))


if __name__ == "__main__":
    unittest.main()
