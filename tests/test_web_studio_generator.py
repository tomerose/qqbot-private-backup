import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot"))

from data.plugins.web_studio.generator import (  # noqa: E402
    GenerationError,
    extract_html,
    generate_draft,
    normalize_request,
    review_draft,
    revise_page,
)


HTML = "<!doctype html><html><head><title>清单</title></head><body>ok</body></html>"


class WebStudioGeneratorTests(unittest.TestCase):
    def test_extract_html_removes_fence_and_preface(self):
        self.assertEqual(extract_html(f"这里是结果\n```html\n{HTML}\n```"), HTML)
        with self.assertRaises(GenerationError):
            extract_html("我建议做一个清单网页")

    def test_request_has_clear_length_bounds(self):
        self.assertEqual(normalize_request("  做一个   番茄钟工具  "), "做一个 番茄钟工具")
        with self.assertRaises(ValueError):
            normalize_request("网页")
        with self.assertRaises(ValueError):
            normalize_request("长" * 1201)
        with self.assertRaisesRegex(ValueError, "不能制作"):
            normalize_request("制作一个仿冒银行官网登录页面")

    @patch("data.plugins.web_studio.generator.requests.post")
    def test_generate_uses_fixed_model_and_offline_system_rules(self, post):
        post.return_value = Mock(
            status_code=200,
            json=Mock(return_value={"choices": [{"message": {"content": HTML}}]}),
        )
        self.assertEqual(generate_draft("制作一个可以增删的旅行清单"), HTML)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "gemini-2.5-pro")
        self.assertIn("禁止任何联网行为", payload["messages"][0]["content"])
        self.assertNotIn("thinking", payload)

    @patch("data.plugins.web_studio.generator.requests.post")
    def test_revision_includes_existing_page_and_requested_change(self, post):
        post.return_value = Mock(
            status_code=200,
            json=Mock(return_value={"choices": [{"message": {"content": HTML}}]}),
        )
        revise_page("制作旅行清单网页", HTML, "增加预算合计功能")
        user = post.call_args.kwargs["json"]["messages"][1]["content"]
        self.assertIn("增加预算合计功能", user)
        self.assertIn(HTML, user)

    @patch("data.plugins.web_studio.generator.requests.post")
    def test_trusted_repair_instruction_can_name_blocked_features(self, post):
        post.return_value = Mock(
            status_code=200,
            json=Mock(return_value={"choices": [{"message": {"content": HTML}}]}),
        )
        review_draft("删除 form，禁止登录和支付，同时保留任务清单", HTML)
        self.assertTrue(post.called)

    @patch("data.plugins.web_studio.generator.requests.post")
    def test_upstream_details_are_not_exposed(self, post):
        post.return_value = Mock(status_code=500, json=Mock(return_value={"error": "secret"}))
        with self.assertRaisesRegex(GenerationError, "暂时不可用") as caught:
            generate_draft("制作一个实用的旅行清单网页")
        self.assertNotIn("secret", str(caught.exception))

    @patch("data.plugins.web_studio.generator.requests.post")
    def test_pro_model_falls_back_to_current_flash(self, post):
        post.side_effect = [
            Mock(status_code=502, json=Mock(return_value={"error": "empty"})),
            Mock(
                status_code=200,
                json=Mock(return_value={"choices": [{"message": {"content": HTML}}]}),
            ),
        ]
        self.assertEqual(generate_draft("制作一个可以增删的旅行清单"), HTML)
        models = [call.kwargs["json"]["model"] for call in post.call_args_list]
        self.assertEqual(models, ["gemini-2.5-pro", "gemini-3.5-flash"])


if __name__ == "__main__":
    unittest.main()
