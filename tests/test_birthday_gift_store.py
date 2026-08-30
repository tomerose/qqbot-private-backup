import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock


PLUGINS_DIR = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS_DIR))

from friend_core.gift_store import (  # noqa: E402
    GiftAddressVault,
    GiftStore,
    parse_address_payload,
)
from friend_core.main import FriendCore  # noqa: E402


class _MemoryVault:
    def __init__(self):
        self.values = {}

    def write(self, key, value):
        self.values[key] = value

    def read(self, key):
        return self.values[key]

    def delete(self, key):
        self.values.pop(key, None)


class BirthdayGiftStoreTests(unittest.TestCase):
    def build(self, directory):
        vault = _MemoryVault()
        return GiftStore(Path(directory) / "gifts.sqlite3", vault=vault), vault

    def test_full_approval_consent_relay_shipping_completion_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            store, vault = self.build(directory)
            order = store.create_candidate("900000001", 2026, "小林")
            store.transition(order.order_id, {"candidate"}, "approved")
            store.transition(order.order_id, {"approved"}, "consented")
            store.submit_address(
                order.order_id, "900000001", "小林|13800138000|浙江省杭州市西湖区测试路 88 号"
            )
            address = store.relay_address(order.order_id)
            self.assertEqual(address.phone, "13800138000")
            store.mark_address_relayed(order.order_id)
            self.assertNotIn(order.order_id, vault.values)
            shipped = store.mark_shipped(order.order_id, "顺丰", "SF1234567890")
            self.assertEqual(shipped.status, "shipped")
            completed = store.close(order.order_id, "completed")
            self.assertEqual(completed.status, "completed")

    def test_plaintext_address_never_enters_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _vault = self.build(directory)
            order = store.create_candidate("900000001", 2026)
            store.transition(order.order_id, {"candidate"}, "approved")
            store.transition(order.order_id, {"approved"}, "consented")
            secret = "浙江省杭州市西湖区隐私路 99 号"
            store.submit_address(order.order_id, "900000001", f"小林|13800138000|{secret}")
            self.assertNotIn(secret.encode("utf-8"), store.database.read_bytes())
            self.assertNotIn(b"13800138000", store.database.read_bytes())

    def test_address_requires_strict_format_and_china_mobile(self):
        self.assertEqual(
            parse_address_payload("小林|138 0013 8000|浙江省杭州市西湖区测试路 88 号").phone,
            "13800138000",
        )
        with self.assertRaises(ValueError):
            parse_address_payload("小林|123|地址")

    def test_cancel_and_expiry_delete_private_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            store, vault = self.build(directory)
            order = store.create_candidate("900000001", 2026)
            store.transition(order.order_id, {"candidate"}, "approved")
            store.transition(order.order_id, {"approved"}, "consented")
            store.submit_address(order.order_id, "900000001", "小林|13800138000|浙江省杭州市测试路 88 号")
            store.close(order.order_id, "cancelled")
            self.assertNotIn(order.order_id, vault.values)

    def test_friend_core_relays_address_only_after_admin_and_user_consent(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as directory:
                store, vault = self.build(directory)
                order = store.create_candidate("2000000000", 2026, "小林")
                plugin = FriendCore.__new__(FriendCore)
                plugin._gift_store = store
                plugin._gift_admin_qq = "900000001"
                plugin._send_reminder_message = AsyncMock(return_value=True)

                approved = await plugin._handle_gift_command(
                    "900000001", f"/礼物 审批 {order.order_id}"
                )
                self.assertIn("已审批", approved)
                consented = await plugin._handle_gift_command(
                    "2000000000", "/生日礼物 同意且确认已满14岁"
                )
                self.assertIn("同意已记录", consented)
                relayed = await plugin._handle_gift_command(
                    "2000000000",
                    "/生日礼物 地址 小林|13800138000|浙江省杭州市西湖区测试路 88 号",
                )
                self.assertIn("密文现已删除", relayed)
                self.assertNotIn(order.order_id, vault.values)
                admin_calls = [
                    call for call in plugin._send_reminder_message.await_args_list
                    if call.args[0] == "900000001"
                ]
                self.assertEqual(len(admin_calls), 1)
                self.assertIn("浙江省杭州市西湖区测试路", admin_calls[0].args[1])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
