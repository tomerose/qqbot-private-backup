import concurrent.futures
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot"))

from data.plugins.web_studio.core import (  # noqa: E402
    PageStore,
    UnsafePageError,
    new_page_id,
    prepare_html,
    requirement_gaps,
)


VALID = """<!doctype html><html lang="zh-CN"><head><title>喝水记录</title>
<style>body{font-family:system-ui}</style></head><body><label>杯数<input id="n" type="number"></label>
<button id="add">增加</button><script>add.addEventListener('click',()=>n.value=+n.value+1)</script></body></html>"""


class WebStudioCoreTests(unittest.TestCase):
    def test_prepare_injects_csp_and_mobile_metadata(self):
        html, title = prepare_html(VALID)
        self.assertEqual(title, "喝水记录")
        self.assertIn("Content-Security-Policy", html)
        self.assertIn("connect-src 'none'", html)
        self.assertIn("width=device-width", html)
        self.assertNotIn("xiaoning-web-studio-mark", html)

    def test_safe_data_image_is_allowed(self):
        raw = VALID.replace(
            "<button", '<img alt="dot" src="data:image/png;base64,AA=="><button'
        )
        self.assertEqual(prepare_html(raw)[1], "喝水记录")

    def test_safe_raster_data_background_and_local_text_download_are_allowed(self):
        raw = VALID.replace(
            "body{", "body{background:url(data:image/png;base64,AA==);"
        ).replace(
            "<button", '<a download="data.json" href="data:text/json,%7B%7D">x</a><button'
        )
        html, _ = prepare_html(raw)
        self.assertIn("data:image/png", html)
        self.assertIn("data:text/json", html)

    def test_standard_inline_svg_namespace_is_not_treated_as_network(self):
        raw = VALID.replace(
            "<button",
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
            '<path d="M1 1h14v14H1z"/></svg><button',
        )
        self.assertEqual(prepare_html(raw)[1], "喝水记录")

    def test_local_form_without_destination_is_allowed_under_form_action_none(self):
        raw = VALID.replace("<label", '<form id="task"><label').replace(
            "</label>", "</label></form>", 1
        )
        html, _ = prepare_html(raw)
        self.assertIn("<form", html)
        self.assertIn("form-action 'none'", html)

    def test_local_image_input_blob_preview_and_export_are_allowed(self):
        raw = VALID.replace(
            "<button",
            '<input type="file" accept="image/*" multiple><button',
        ).replace(
            "add.addEventListener",
            "const u=URL.createObjectURL(new Blob(['{}'],{type:'application/json'}));"
            "add.addEventListener",
        )
        html, _ = prepare_html(raw)
        self.assertIn('type="file"', html)
        self.assertIn("createObjectURL", html)

    def test_dangerous_tags_attributes_and_code_are_rejected(self):
        cases = [
            VALID.replace("<button", '<form action="/collect"><button'),
            VALID.replace("<button", '<iframe src="data:text/html,x"></iframe><button'),
            VALID.replace('type="number"', 'type="password"'),
            VALID.replace("</body>", '<script src="https://bad.test/x.js"></script></body>'),
            VALID.replace("add.addEventListener", "fetch('/steal');add.addEventListener"),
            VALID.replace("add.addEventListener", "navigator.clipboard.readText();add.addEventListener"),
            VALID.replace("</body>", '<a href="https://bad.test">x</a></body>'),
            VALID.replace("body{", "body{background:url(https://bad.test/x.png);"),
            VALID.replace("</head>", '<meta http-equiv="refresh" content="0"></head>'),
            VALID.replace("<button", '<div id="xiaoning-shell-mark"></div><button'),
        ]
        for raw in cases:
            with self.subTest(raw=raw[:80]), self.assertRaises(UnsafePageError):
                prepare_html(raw)

    def test_page_ids_are_opaque_and_path_safe(self):
        values = {new_page_id() for _ in range(100)}
        self.assertEqual(len(values), 100)
        self.assertTrue(all(len(value) == 10 and value.isalnum() for value in values))

    def test_requirement_check_catches_safe_but_wrong_page(self):
        request = "制作任务清单、番茄专注、饮水记录和进度，刷新后保留数据"
        wrong = "<html><title>25点游戏</title><body>计算分数</body></html>"
        self.assertEqual(
            requirement_gaps(request, wrong),
            ["任务清单", "番茄专注", "饮水记录", "进度", "本地保存"],
        )
        self.assertEqual(
            requirement_gaps(request, VALID + "任务 番茄 专注 进度 localStorage"), []
        )

    def test_requirement_check_covers_local_gallery_interactions(self):
        request = "图库支持拖入和选择多张图片，显示缩略图，搜索、排序、批量勾选并导出为 JSON"
        wrong = "<html><title>图库</title><body>只是文字</body></html>"
        self.assertEqual(
            requirement_gaps(request, wrong),
            ["本地图片选择", "拖拽导入", "缩略图", "搜索", "排序", "批量选择", "JSON 导出"],
        )

    def test_store_enforces_ownership_and_persists_soft_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pages.db"
            store = PageStore(path, clock=lambda: 1_784_000_000)
            store.create("0123456789", "111", "清单", "制作旅行清单", "x")
            self.assertEqual(store.active_count("111"), 1)
            self.assertIsNone(store.get("0123456789", "222"))
            self.assertFalse(store.delete("0123456789", "222"))
            store.update("0123456789", "111", "新清单", "增加预算")
            self.assertEqual(PageStore(path).get("0123456789", "111").title, "新清单")
            self.assertTrue(store.delete("0123456789", "111"))
            self.assertEqual(store.list("111"), [])

    def test_usage_is_atomic_daily_and_refundable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PageStore(Path(directory) / "pages.db", clock=lambda: 1_784_000_000)
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: store.consume("111", 3), range(8)))
            self.assertEqual(sum(accepted for accepted, _, _ in results), 3)
            self.assertEqual(store.consume("111", 3), (False, 3, "2026-07-14"))
            store.refund("111", "2026-07-14")
            self.assertEqual(store.consume("111", 3), (True, 3, "2026-07-14"))

    def test_refund_uses_the_reserved_day_across_midnight(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [1_784_044_799]
            store = PageStore(Path(directory) / "pages.db", clock=lambda: now[0])
            accepted, _, reserved_day = store.consume("111", 1)
            self.assertTrue(accepted)
            now[0] += 2
            store.refund("111", reserved_day)
            now[0] -= 2
            self.assertTrue(store.consume("111", 1)[0])


if __name__ == "__main__":
    unittest.main()
