import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = PROJECT_ROOT / "astrbot" / "data" / "plugins"
CMD_CONFIG = PROJECT_ROOT / "astrbot" / "data" / "cmd_config.json"
sys.path.insert(0, str(PLUGINS_DIR))

from contact_pro_info.main import (  # noqa: E402
    CONTACT_REPLY,
    CAPABILITY_MEMORY,
    CAPABILITY_CATALOG_MEMORY,
    CONVERSATIONAL_HELP_REPLY,
    ContactProInfo,
    capability_contract_block,
    contact_reply_for,
    feature_help_for,
    version_reply_for,
    VERSION_REPLY,
    PRO_APPLICATION_GUIDE,
    USER_GUIDE,
    WEIXIN_PRIVATE_HELP_REPLY,
    WEIXIN_PRIVATE_CAPABILITY_MEMORY,
)
from runtime_config_fixture import ensure_runtime_configs  # noqa: E402


class FakeEvent:
    def __init__(self, text: str, *, platform_name: str = ""):
        self.text = text
        self.stopped = False
        if platform_name:
            self.platform_meta = SimpleNamespace(name=platform_name)

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
    @classmethod
    def setUpClass(cls):
        ensure_runtime_configs(PROJECT_ROOT)

    def test_weixin_private_help_never_claims_qq_membership_or_voice_delivery(self):
        async def scenario():
            plugin = ContactProInfo.__new__(ContactProInfo)
            event = FakeEvent("小柠能做什么", platform_name="weixin_oc")

            replies = await collect(plugin.on_message(event))

            self.assertEqual(replies, [WEIXIN_PRIVATE_HELP_REPLY])
            self.assertNotIn("自动获得 X", replies[0])
            self.assertIn("文字回复", replies[0])

        asyncio.run(scenario())

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

    def test_capability_contract_is_scoped_to_the_current_turn_and_channel(self):
        async def scenario():
            plugin = ContactProInfo.__new__(ContactProInfo)
            ordinary = SimpleNamespace(system_prompt="base prompt")
            await plugin.inject_capability_memory(FakeEvent("今天有点累"), ordinary)
            self.assertEqual(ordinary.system_prompt, "base prompt")

            task_event = FakeEvent("帮我做一份 Word 报告")
            task = SimpleNamespace(system_prompt="base prompt")
            await plugin.inject_capability_memory(task_event, task)
            self.assertIn("【本轮能力契约】", task.system_prompt)
            self.assertIn("唯一处理器=claude_code_agent", task.system_prompt)
            self.assertNotIn("【可执行能力目录】", task.system_prompt)
            await plugin.inject_capability_memory(task_event, task)
            self.assertEqual(task.system_prompt.count("【本轮能力契约】"), 1)

            weixin = SimpleNamespace(system_prompt="base prompt")
            await plugin.inject_capability_memory(
                FakeEvent("hi", platform_name="weixin_oc"), weixin
            )
            self.assertIn(WEIXIN_PRIVATE_CAPABILITY_MEMORY, weixin.system_prompt)
            self.assertNotIn("【公开能力事实】", WEIXIN_PRIVATE_CAPABILITY_MEMORY)
            # QQ and WeChat blocks never cross channels
            self.assertNotIn("必须QQ交付", WEIXIN_PRIVATE_CAPABILITY_MEMORY)

        asyncio.run(scenario())

    def test_contract_builder_never_returns_the_full_catalog(self):
        block = capability_contract_block("帮我画一张海报")
        self.assertIn("能力=draw", block)
        self.assertNotIn("【可执行能力目录】", block)

    def test_capability_prompt_does_not_expand_one_missed_route_into_a_blanket_refusal(self):
        self.assertIn("只说明当前这一步没有执行", CAPABILITY_MEMORY)
        self.assertIn("不要笼统声称小柠不能操作电脑", CAPABILITY_MEMORY)

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
            "能帮我查资料 Python 怎么用吗",
            "请问帮我查资料，Python 怎么用",
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
        self.assertIn("没有实际需求时不介绍功能", persona_prompt)
        self.assertIn("主动给一个具体判断或行动建议", persona_prompt)
        self.assertIn("有自己立场", persona_prompt)
        self.assertIn("QQ 实际收到文件才算完成", persona_prompt)
        self.assertIn("不公开管理入口", persona_prompt)
        self.assertNotRegex(persona_prompt, r"QQ\s*\d{5,}")
        self.assertIn("普通版含", CAPABILITY_MEMORY)
        self.assertIn("目标文件", USER_GUIDE)
        self.assertIn("小柠网页工坊", USER_GUIDE)
        self.assertIn("公开HTTPS页面", CAPABILITY_MEMORY)
        self.assertIn("/早报 开启", USER_GUIDE)
        self.assertIn("/记忆 删除全部", USER_GUIDE)
        self.assertIn("/think <问题>", USER_GUIDE)
        self.assertIn("/gh <关键词>", USER_GUIDE)
        self.assertIn("/订阅动态", USER_GUIDE)
        self.assertIn("/search <问题>", USER_GUIDE)
        self.assertIn("/browse <公开链接>", USER_GUIDE)
        self.assertIn("公开网页阅读", CAPABILITY_MEMORY)
        self.assertIn("最直接的帮助方式", CAPABILITY_MEMORY)
        self.assertIn("先看完用户这一轮的全部内容", CAPABILITY_MEMORY)
        self.assertIn("接住并推进对应功能", CAPABILITY_MEMORY)
        self.assertIn("【可执行能力目录】", CAPABILITY_CATALOG_MEMORY)
        self.assertIn(
            "成品=.docx,.pdf,.pptx,.xlsx,.csv,.md，必须QQ交付",
            CAPABILITY_CATALOG_MEMORY,
        )
        self.assertIn("一次说几件事", USER_GUIDE)
        self.assertNotIn("白名单 QQ", USER_GUIDE)
        self.assertNotIn("/添加提醒", USER_GUIDE)
        self.assertNotIn("/remind ", USER_GUIDE)

if __name__ == "__main__":
    unittest.main()
