import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["ASTRBOT_ROOT"] = str(ROOT / "astrbot")
sys.path.insert(0, str(ROOT / "astrbot" / "data" / "plugins"))

from astrbot_plugin_xiaoning_memory import main as memory_module  # noqa: E402


class _Document:
    pass


class _Reference:
    def __init__(self):
        self.documents = []

    def document(self):
        document = _Document()
        self.documents.append(document)
        return document


class _Batch:
    def __init__(self):
        self.writes = []
        self.committed = False

    def set(self, document, payload):
        self.writes.append((document, payload))

    def commit(self):
        self.committed = True


class _Database:
    def __init__(self):
        self.batch_instance = _Batch()

    def batch(self):
        return self.batch_instance


class _GroupEvent:
    def is_private_chat(self):
        return False

    def get_group_id(self):
        return "123456"

    def get_sender_id(self):
        return "not-a-user-id"

    def get_message_str(self):
        return "童哥刚才说的方案继续做"


class _Request:
    system_prompt = "基础提示"


class XiaoningMemoryScopeTests(unittest.TestCase):
    def test_group_alias_is_self_declared_and_only_matches_current_message(self):
        self.assertEqual(memory_module._self_declared_alias("小柠，我叫童哥"), "童哥")
        self.assertIsNone(memory_module._self_declared_alias("童哥是负责人"))
        self.assertEqual(
            memory_module._mentioned_group_aliases(
                "童哥刚才说的方案继续做",
                [{"alias": "童哥"}, {"alias": "小陈"}],
            ),
            ["童哥"],
        )

    def test_group_alias_can_inject_for_ordinary_user_without_private_memory(self):
        plugin = memory_module.XiaoningMemory.__new__(memory_module.XiaoningMemory)
        plugin._db = object()
        plugin._get_group_aliases = lambda _group_id: [{"alias": "童哥"}]

        import asyncio

        request = _Request()
        asyncio.run(plugin.inject_memories(_GroupEvent(), request))
        self.assertIn("本群本人公开的称呼", request.system_prompt)
        self.assertIn("童哥", request.system_prompt)
        self.assertNotIn("当前发送者的私有记忆", request.system_prompt)

    def test_operational_requests_do_not_enter_durable_memory(self):
        for text in (
            "帮我生成一份 Word 报告",
            "请你把这段话翻译成英文",
            "小柠，分析这个文件",
            "制作一个演示文稿",
        ):
            self.assertIsNotNone(memory_module._OPERATIONAL_REQUEST.match(text), text)

    def test_memory_storage_persists_importance_and_skips_duplicates(self):
        plugin = memory_module.XiaoningMemory.__new__(memory_module.XiaoningMemory)
        reference = _Reference()
        database = _Database()
        plugin._valid_qq = lambda _qq_id: True
        plugin._get_memories = lambda *_args, **_kwargs: []
        plugin._memories_ref = lambda _qq_id: reference
        plugin._db = database

        stored = plugin._store_memories(
            "1211000567",
            [
                {
                    "key": "咖啡偏好",
                    "value": "用户喜欢不加糖的拿铁",
                    "category": "preference",
                    "importance": 0.8,
                },
                {
                    "key": "咖啡偏好",
                    "value": "用户喜欢不加糖的拿铁",
                    "category": "preference",
                    "importance": 0.8,
                },
            ],
        )

        self.assertEqual(stored, 1)
        self.assertTrue(database.batch_instance.committed)
        self.assertEqual(len(database.batch_instance.writes), 1)
        self.assertEqual(database.batch_instance.writes[0][1]["importance"], 0.8)


if __name__ == "__main__":
    unittest.main()
