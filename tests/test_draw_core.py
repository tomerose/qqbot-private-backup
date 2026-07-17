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
    is_dewatermark_request,
)


class DrawCoreTests(unittest.TestCase):
    def test_pro_ids_accept_only_valid_qq_numbers(self):
        self.assertEqual(
            parse_pro_user_ids("1211000567, 1211000567;not-pro 01234"),
            ("1211000567",),
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

        self.assertEqual(limiter.try_acquire("1211000567"), 0)
        now[0] = 115.2
        self.assertEqual(limiter.try_acquire("1211000567"), 45)
        now[0] = 160.1
        self.assertEqual(limiter.try_acquire("1211000567"), 0)

    def test_recognizes_natural_dewatermark_request(self):
        self.assertTrue(is_dewatermark_request("帮我把右下角那个浅灰色的“@画师”小尾巴彻底抹掉"))
        self.assertTrue(is_dewatermark_request("消除这张图的 Logo"))
        self.assertTrue(is_dewatermark_request("去水印"))
        self.assertTrue(is_dewatermark_request("帮我去s水印"))
        self.assertFalse(is_dewatermark_request("把右下角的人物抹掉"))

    def test_recognizes_redraw_followups(self):
        self.assertIn("忠实重绘", parse_edit_command("重画"))
        self.assertEqual(
            parse_edit_command("重新画一个极简黑白线条的大耳狗小鸡头像"),
            "以参考图为基础重新绘制：极简黑白线条的大耳狗小鸡头像",
        )


if __name__ == "__main__":
    unittest.main()
