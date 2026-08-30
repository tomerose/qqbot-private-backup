import sys
import unittest
from pathlib import Path


PLUGINS_DIR = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS_DIR))

from draw_command.draw_core import (  # noqa: E402
    DrawRateLimiter,
    DrawRequestError,
    parse_draw_command,
    parse_edit_command,
    parse_pro_user_ids,
)


class DrawCoreTests(unittest.TestCase):
    def test_pro_ids_accept_only_valid_qq_numbers(self):
        self.assertEqual(
            parse_pro_user_ids("900000001, 900000001;not-pro 01234"),
            ("900000001",),
        )

    def test_parses_only_explicit_draw_commands_and_bounds_prompt(self):
        self.assertEqual(parse_draw_command("/draw a moonlit lake"), "a moonlit lake")
        self.assertEqual(parse_draw_command("/画图 一只小猫"), "一只小猫")
        self.assertEqual(parse_draw_command("帮我画一只小猫"), "一只小猫")
        with self.assertRaises(DrawRequestError):
            parse_draw_command("/draw " + "x" * 501)

    def test_rate_limiter_returns_remaining_cooldown_without_consuming_second_request(self):
        now = [100.0]
        limiter = DrawRateLimiter(cooldown_seconds=60, clock=lambda: now[0])

        self.assertEqual(limiter.try_acquire("900000001"), 0)
        now[0] = 115.2
        self.assertEqual(limiter.try_acquire("900000001"), 45)
        now[0] = 160.1
        self.assertEqual(limiter.try_acquire("900000001"), 0)

    def test_removed_watermark_feature_never_becomes_an_edit(self):
        self.assertIsNone(parse_edit_command("去水印"))
        self.assertIsNone(parse_edit_command("帮我去s水印"))
        self.assertIsNone(parse_edit_command("/edit 去掉watermark"))

    def test_recognizes_redraw_followups(self):
        self.assertIn("忠实重绘", parse_edit_command("重画"))
        self.assertEqual(
            parse_edit_command("重新画一个极简黑白线条的大耳狗小鸡头像"),
            "以参考图为基础重新绘制：极简黑白线条的大耳狗小鸡头像",
        )


if __name__ == "__main__":
    unittest.main()
