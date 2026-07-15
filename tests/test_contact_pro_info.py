import asyncio
import json
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = PROJECT_ROOT / "astrbot" / "data" / "plugins"
CMD_CONFIG = PROJECT_ROOT / "astrbot" / "data" / "cmd_config.json"
sys.path.insert(0, str(PLUGINS_DIR))

from contact_pro_info.main import (  # noqa: E402
    CONTACT_REPLY,
    CAPABILITY_MEMORY,
    CONVERSATIONAL_HELP_REPLY,
    ContactProInfo,
    contact_reply_for,
    feature_help_for,
    version_reply_for,
    VERSION_REPLY,
    PRO_APPLICATION_GUIDE,
    USER_GUIDE,
)


class FakeEvent:
    def __init__(self, text: str):
        self.text = text
        self.stopped = False

    def get_message_str(self):
        return self.text

    def plain_result(self, text: str):
        return text

    def is_private_chat(self):
        return True

    def stop_event(self):
        self.stopped = True


async def collect(generator):
    return [item async for item in generator]


class ContactProInfoTests(unittest.TestCase):
    def test_public_help_describes_the_explicit_music_paths(self):
        self.assertIn("\u7f51\u6613\u4e91\u6b4c\u66f2 ID", USER_GUIDE)
        self.assertIn("/sing <\u539f\u521b\u6b4c\u66f2\u63cf\u8ff0>", USER_GUIDE)
        self.assertIn("\u97f3\u4e50\u751f\u6210", VERSION_REPLY)

    def test_version_questions_return_user_facing_feature_summary(self):
        for text in (
            "普通版和Pro有什么区别",
            "Pro版功能",
        ):
            with self.subTest(text=text):
                self.assertEqual(version_reply_for(text), VERSION_REPLY)

    def test_conversational_help_is_short_and_does_not_dump_the_manual(self):
        self.assertEqual(version_reply_for("小柠能做什么"), CONVERSATIONAL_HELP_REPLY)
        self.assertIn("直接说", CONVERSATIONAL_HELP_REPLY)
        self.assertNotIn("【小柠使用指南】", CONVERSATIONAL_HELP_REPLY)

    def test_feature_questions_are_explained_without_claiming_real_tasks(self):
        for text in (
            "深度研究怎么用啊",
            "网页工坊有什么用",
            "你会画图吗",
            "视频怎么用",
            "圆桌辩论是干嘛的",
        ):
            with self.subTest(text=text):
                self.assertIsNotNone(feature_help_for(text))

        for text in (
            "帮我深度研究今年大学生就业趋势",
            "帮我查资料，Python 怎么用",
            "帮我做一个记账网页",
            "帮我画一只猫",
            "帮我生成一段猫的视频",
            "圆桌讨论AI会不会替代人",
            "请生成一份 Agent 功能怎么用的报告",
        ):
            with self.subTest(text=text):
                self.assertIsNone(feature_help_for(text))

    def test_feature_question_handler_stops_before_task_plugins(self):
        async def scenario():
            plugin = ContactProInfo.__new__(ContactProInfo)
            event = FakeEvent("深度研究怎么用啊")
            replies = await collect(plugin.on_message(event))
            self.assertEqual(replies, [feature_help_for(event.text)])
            self.assertTrue(event.stopped)

        asyncio.run(scenario())

    def test_unrelated_pro_model_question_does_not_return_version_summary(self):
        self.assertIsNone(version_reply_for("这个 Pro 模型怎么样"))

    def test_contact_and_pro_acquisition_intents_return_public_email(self):
        for text in (
            "怎么联系作者",
            "老板的联系方式",
        ):
            with self.subTest(text=text):
                self.assertEqual(contact_reply_for(text), CONTACT_REPLY)
        for text in (
            "Pro 怎么获取",
            "我想申请 pro 资格",
        ):
            with self.subTest(text=text):
                self.assertEqual(contact_reply_for(text), PRO_APPLICATION_GUIDE)

    def test_unrelated_pro_discussion_does_not_trigger(self):
        for text in ("这个 Pro 模型怎么样", "今天吃什么", "老板键是什么"):
            with self.subTest(text=text):
                self.assertIsNone(contact_reply_for(text))

    def test_handler_returns_native_result_and_stops_matching_event(self):
        async def scenario():
            plugin = ContactProInfo.__new__(ContactProInfo)
            event = FakeEvent("普通版和 Pro 有什么区别")

            replies = await collect(plugin.on_message(event))

            self.assertEqual(replies, [VERSION_REPLY])
            self.assertTrue(event.stopped)

        asyncio.run(scenario())

    def test_active_prompts_and_runtime_memory_match_public_capabilities(self):
        config = json.loads(CMD_CONFIG.read_text(encoding="utf-8-sig"))
        persona_prompt = next(
            persona.get("prompt", "")
            for persona in config["persona"]
            if persona.get("name") == "xiaoning"
        )
        self.assertIn("询问联系作者", persona_prompt)
        self.assertIn("公开邮箱", persona_prompt)
        self.assertIn("GitHub 查询", persona_prompt)
        self.assertIn("文件任务的成功标准是 QQ 已实际收到文件", persona_prompt)
        self.assertIn("不公开管理入口", persona_prompt)
        self.assertNotRegex(persona_prompt, r"QQ\s*\d{5,}")
        self.assertIn("普通版含", CAPABILITY_MEMORY)
        self.assertIn("目标文件", USER_GUIDE)
        self.assertIn("小柠网页工坊", USER_GUIDE)
        self.assertIn("公开HTTPS页面", CAPABILITY_MEMORY)
        self.assertIn("/早报 开启", USER_GUIDE)
        self.assertIn("/记忆 清除", USER_GUIDE)
        self.assertIn("/think <问题>", USER_GUIDE)
        self.assertIn("/gh <关键词>", USER_GUIDE)
        self.assertIn("/订阅动态", USER_GUIDE)
        self.assertIn("/search <问题>", USER_GUIDE)
        self.assertIn("/browse <公开链接>", USER_GUIDE)
        self.assertIn("公开网页阅读", CAPABILITY_MEMORY)
        self.assertIn("最直接的帮助方式", CAPABILITY_MEMORY)
        self.assertIn("先看完用户这一轮的全部内容", CAPABILITY_MEMORY)
        self.assertIn("接住并推进对应功能", CAPABILITY_MEMORY)
        self.assertIn("一次说几件事", USER_GUIDE)
        self.assertNotIn("白名单 QQ", USER_GUIDE)
        self.assertNotIn("/添加提醒", USER_GUIDE)
        self.assertNotIn("/remind ", USER_GUIDE)

        async def scenario():
            plugin = ContactProInfo.__new__(ContactProInfo)
            req = type("Req", (), {"system_prompt": "persona"})()
            await plugin.inject_capability_memory(FakeEvent(""), req)
            self.assertIn(CAPABILITY_MEMORY, req.system_prompt)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
