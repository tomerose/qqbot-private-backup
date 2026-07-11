import sys
import unittest
from pathlib import Path

PLUGIN_PARENT = Path(r"D:\Claudecoda学习\qqbot\astrbot\data\plugins")
sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot_plugin_qqadmin.core.ai_moderation_policy import (  # noqa: E402
    ModerationDecision,
    build_anonymous_context,
    is_candidate,
    parse_decision,
    resolve_action,
    sanitize_message,
)


class AIModerationPolicyTests(unittest.TestCase):
    def test_invalid_low_confidence_and_unknown_values_never_punish(self):
        samples = [
            "not-json",
            '{"decision":"recall_and_mute","category":"spam","confidence":0.89,"reason_code":"repeated_spam"}',
            '{"decision":"kick","category":"spam","confidence":1,"reason_code":"repeated_spam"}',
            '{"decision":"recall","category":"opinion","confidence":1,"reason_code":"repeated_spam"}',
        ]
        for raw in samples:
            with self.subTest(raw=raw):
                self.assertEqual(parse_decision(raw).decision, "none")

    def test_action_escalation_is_bounded(self):
        decision = ModerationDecision(
            "recall_and_mute", "spam", 0.99, "repeated_spam"
        )
        actions = [resolve_action(decision, count) for count in (0, 1, 2, 3, 99)]
        self.assertEqual([action.mute_seconds for action in actions], [0, 60, 300, 1800, 1800])
        self.assertTrue(all(action.recall for action in actions))

    def test_context_removes_identifiers_paths_queries_and_secrets(self):
        raw = (
            r"QQ1211000567 文件 C:\Users\liu\private.txt "
            "token=very-secret https://example.test/a?q=private#fragment"
        )
        cleaned = sanitize_message(raw)
        for secret in ("1211000567", "liu", "very-secret", "private", "fragment"):
            self.assertNotIn(secret, cleaned)
        self.assertIn("https://example.test/a", cleaned)

    def test_anonymous_context_uses_no_ids_and_is_bounded(self):
        context = build_anonymous_context(
            [("123456789", "第一条"), ("987654321", "第二条")],
            max_messages=8,
            max_chars=3000,
        )
        self.assertEqual(context, "成员A：第一条\n成员B：第二条")
        self.assertNotIn("123456789", context)
        self.assertNotIn("987654321", context)

    def test_candidate_filter_is_broad_but_skips_normal_chat(self):
        self.assertFalse(is_candidate("今天晚上吃什么", recent_same=0))
        self.assertTrue(is_candidate("点击链接领取返现 https://example.test", recent_same=0))
        self.assertTrue(is_candidate("同一句话", recent_same=2))


if __name__ == "__main__":
    unittest.main()
