import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


PLUGINS = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS))

from friend_core.main import FriendCore  # noqa: E402


class _Platform:
    def __init__(self, name: str, platform_id: str):
        self.metadata = SimpleNamespace(name=name, id=platform_id)


class _Context:
    def __init__(self):
        self.platform_manager = SimpleNamespace(
            platform_insts=[
                _Platform("aiocqhttp", "qq-primary"),
                _Platform("weixin_oc", "weixin-private"),
            ]
        )
        self.sent = []

    async def send_message(self, session, message):
        self.sent.append((session, message))


class WeixinPrivateCompatibilityTests(unittest.TestCase):
    def test_weixin_reminder_uses_the_weixin_session_not_the_first_platform(self):
        async def scenario():
            context = _Context()
            plugin = FriendCore.__new__(FriendCore)
            plugin.context = context

            sent = await plugin._send_reminder_message(
                "weixin_oc:wxid_alice_123", "提醒内容"
            )

            self.assertTrue(sent)
            self.assertEqual(
                context.sent[0][0], "weixin-private:FriendMessage:wxid_alice_123"
            )

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
