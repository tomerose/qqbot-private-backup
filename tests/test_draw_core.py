import sys
import unittest
from pathlib import Path


PLUGINS_DIR = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS_DIR))

from draw_command.draw_core import DrawRateLimiter, DrawRequestError, parse_draw_command  # noqa: E402


class DrawCoreTests(unittest.TestCase):
    def test_parses_only_explicit_draw_commands_and_bounds_prompt(self):
        self.assertEqual(parse_draw_command("/draw a moonlit lake"), "a moonlit lake")
        self.assertEqual(parse_draw_command("/画图 一只小猫"), "一只小猫")
        self.assertIsNone(parse_draw_command("帮我画一只小猫"))
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


if __name__ == "__main__":
    unittest.main()
