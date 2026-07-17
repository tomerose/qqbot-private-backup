import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot"))

from data.plugins.web_studio.publisher import (  # noqa: E402
    FIREBASE_PROJECT,
    FIREBASE_SITE,
    FirebasePublisher,
    PublishError,
)


class FirebasePublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.publisher = FirebasePublisher(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_scaffold_is_locked_to_one_site_and_adds_security_headers(self):
        body = json.loads(self.publisher.config_path.read_text(encoding="utf-8"))
        self.assertEqual(body["hosting"]["site"], FIREBASE_SITE)
        headers = body["hosting"]["headers"][0]
        self.assertEqual(headers["source"], "/x/**")
        values = {item["key"]: item["value"] for item in headers["headers"]}
        self.assertIn("connect-src 'none'", values["Content-Security-Policy"])
        # The managed shell must be allowed to load its own opaque srcdoc frame.
        # Generated app HTML receives a separate CSP with frame-src 'none'.
        self.assertIn("frame-src 'self'", values["Content-Security-Policy"])
        self.assertIn("worker-src blob:", values["Content-Security-Policy"])
        self.assertEqual(values["X-Frame-Options"], "DENY")
        self.assertIn("no-store", values["Cache-Control"])

    def test_stage_snapshot_restore_and_remove(self):
        page_id = "0123456789"
        path = self.publisher.stage(page_id, "old")
        self.assertEqual(self.publisher.read_app(page_id), "old")
        preview = self.publisher.preview_path(page_id)
        preview.parent.mkdir(parents=True)
        preview.write_bytes(b"old-preview")
        old = self.publisher.snapshot(page_id)
        self.publisher.stage(page_id, "new")
        preview.write_bytes(b"new-preview")
        self.publisher.restore(page_id, old)
        self.assertEqual(self.publisher.read_app(page_id), "old")
        self.assertEqual(preview.read_bytes(), b"old-preview")
        self.assertEqual(self.publisher.remove(page_id), old)
        self.assertFalse(path.exists())
        self.assertFalse(preview.exists())
        self.publisher.restore(page_id, None)

    def test_stage_uses_opaque_sandbox_and_round_trips_untrusted_html(self):
        page_id = "0123456789"
        app = '<!doctype html><html><script>const x="</script>"</script>中文</html>'
        path = self.publisher.stage(page_id, app)
        shell = path.read_text(encoding="utf-8")
        self.assertEqual(self.publisher.read_app(page_id), app)
        self.assertNotIn(app, shell)
        self.assertIn('sandbox="allow-scripts allow-forms allow-downloads"', shell)
        self.assertIn('allow="clipboard-write; fullscreen"', shell)
        self.assertIn("fullscreen", shell)
        self.assertIn("xiaoning-shell-mark", shell)
        self.assertNotIn("allow-same-origin", shell)
        self.assertNotIn("allow-top-navigation", shell)
        self.assertIn("const PAGE_ID = '0123456789';", shell)
        self.assertIn("'xn:web:' + PAGE_ID", shell)
        self.assertIn("event.source !== frame.contentWindow", shell)
        self.assertIn("frame-src 'self'", shell)
        self.assertIn("data:text/json", shell)

        other = self.publisher.stage("abcdef0123", app).read_text(encoding="utf-8")
        self.assertIn("const PAGE_ID = 'abcdef0123';", other)

    def test_rejects_path_like_page_ids(self):
        for value in ("../escape", "ABCDEFGHIJ", "123", "01234567890"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.publisher.page_path(value)

    @patch("data.plugins.web_studio.publisher.subprocess.run")
    @patch("data.plugins.web_studio.publisher._EDGE_CANDIDATES")
    def test_render_preview_uses_sandbox_shell_and_blocks_network(self, candidates, run):
        edge = self.root / "msedge.exe"
        edge.write_bytes(b"edge")
        candidates.__iter__.return_value = iter((edge,))
        page_id = "0123456789"
        self.publisher.stage(page_id, "<!doctype html><title>x</title><body>x</body>")

        def create_image(command, **kwargs):
            screenshot = next(item for item in command if item.startswith("--screenshot="))
            Path(screenshot.split("=", 1)[1]).write_bytes(b"x" * 200)
            return type("Result", (), {"returncode": 0})()

        run.side_effect = create_image
        output = self.publisher.render_preview(page_id)
        self.assertTrue(output.is_file())
        command = run.call_args.args[0]
        self.assertTrue(command[-1].startswith("file:///"))
        self.assertIn("--headless", command)
        self.assertIn("--window-size=1440,900", command)
        self.assertIn("--force-device-scale-factor=1.5", command)
        self.assertIn("--host-resolver-rules=MAP * ~NOTFOUND", command)
        self.assertIn('sandbox="allow-scripts allow-forms allow-downloads"', self.publisher.page_path(page_id).read_text(encoding="utf-8"))

    @patch("data.plugins.web_studio.publisher.subprocess.run")
    @patch("data.plugins.web_studio.publisher._EDGE_CANDIDATES")
    def test_render_preview_never_accepts_a_stale_png(self, candidates, run):
        edge = self.root / "msedge.exe"
        edge.write_bytes(b"edge")
        candidates.__iter__.return_value = iter((edge,))
        page_id = "0123456789"
        self.publisher.stage(page_id, "x")
        output = self.publisher.preview_path(page_id)
        output.parent.mkdir(parents=True)
        output.write_bytes(b"old" * 100)
        run.return_value = type("Result", (), {"returncode": 0})()

        with self.assertRaises(PublishError):
            self.publisher.render_preview(page_id)
        self.assertFalse(output.exists())

    @patch("data.plugins.web_studio.publisher.subprocess.run")
    @patch("data.plugins.web_studio.publisher.shutil.which")
    def test_deploy_has_fixed_project_config_and_no_shell(self, which, run):
        which.return_value = "firebase.cmd" if os.name == "nt" else "/bin/firebase"
        run.return_value = type("Result", (), {"returncode": 0})()
        self.publisher.deploy()
        command = run.call_args.args[0]
        self.assertIn(FIREBASE_PROJECT, command)
        self.assertIn(str(self.publisher.config_path), command)
        self.assertNotIn("--site", command)
        self.assertFalse(run.call_args.kwargs["check"])

    @patch("data.plugins.web_studio.publisher.time.sleep")
    @patch("data.plugins.web_studio.publisher.urllib.request.urlopen")
    def test_verify_public_rejects_stale_page_with_same_id(self, urlopen, sleep):
        page_id = "0123456789"
        stale = self.publisher.stage(page_id, "old").read_bytes()
        current = self.publisher.stage(page_id, "new").read_bytes()
        response = urlopen.return_value.__enter__.return_value
        response.status = 200
        response.read.return_value = stale
        self.assertFalse(self.publisher._verify_public(page_id, True))
        response.read.return_value = current
        self.assertTrue(self.publisher._verify_public(page_id, True))
        self.assertTrue(sleep.called)

    @patch("data.plugins.web_studio.publisher.subprocess.run")
    @patch("data.plugins.web_studio.publisher.shutil.which")
    def test_deploy_reports_failure_without_leaking_cli_output(self, which, run):
        which.return_value = "firebase.cmd" if os.name == "nt" else "/bin/firebase"
        run.return_value = type("Result", (), {"returncode": 1})()
        with self.assertRaisesRegex(PublishError, "Firebase 发布失败"):
            self.publisher.deploy()


if __name__ == "__main__":
    unittest.main()
