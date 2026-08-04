import asyncio
import sys
import unittest
from pathlib import Path


PLUGIN_PARENT = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot_plugin_qqadmin.core.banpro_handel import BanproHandle  # noqa: E402


class _DB:
    def __init__(self):
        self.writes = []

    async def set(self, group_id, key, value):
        self.writes.append((group_id, key, value))


class _Event:
    def get_group_id(self):
        return "945598390"

    def plain_result(self, text):
        return text

    async def send(self, _result):
        return None


class GroupControlTests(unittest.TestCase):
    def test_spamming_timeout_updates_its_own_setting(self):
        handler = BanproHandle.__new__(BanproHandle)
        handler.db = db = _DB()
        asyncio.run(handler.handle_spamming_ban_time(_Event(), 120))
        self.assertEqual(db.writes, [("945598390", "spamming_ban_time", 120)])


if __name__ == "__main__":
    unittest.main()
