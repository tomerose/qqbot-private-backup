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
from friend_core.group_help import group_help_offer  # noqa: E402
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
