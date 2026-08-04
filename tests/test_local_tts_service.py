import importlib.util
from array import array
import os
import sys
import tempfile
import time
import types
import unittest
import wave
from unittest.mock import patch
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "services" / "local_tts" / "server.py"
SPEC = importlib.util.spec_from_file_location("local_tts_server", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class FakeEngine:
    def __init__(self, label="fake"):
        self.label = label
        self.calls = []

    def synthesize(self, text, output_path):
        self.calls.append(text)
        output_path.write_bytes(b"RIFF-" + self.label.encode("ascii"))


class FailingEngine:
    def synthesize(self, text, output_path):
        raise RuntimeError("private task text must never be logged")


class LocalTTSServiceTests(unittest.TestCase):
    def test_quiet_pcm_output_is_normalized_to_an_audible_peak(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quiet.wav"
            with wave.open(str(path), "wb") as output:
                output.setparams((1, 2, 24000, 4, "NONE", "not compressed"))
                output.writeframes(array("h", [0, 10, -20, 15]).tobytes())

            self.assertTrue(MODULE.normalize_pcm_wav(path))

            with wave.open(str(path), "rb") as normalized:
                samples = array("h")
                samples.frombytes(normalized.readframes(normalized.getnframes()))
            self.assertEqual(max(abs(sample) for sample in samples), 28000)

    def test_start_script_passes_ascii_relative_private_paths(self):
        script = (
            MODULE_PATH.parent / "start_local_tts.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn('$tokenArg = "..\\..\\claude_workspace\\state\\local_tts.token"', script)
        self.assertIn('$audioArg = "..\\..\\claude_workspace\\tts_audio"', script)
        self.assertIn('"--token-file", $tokenArg', script)
        self.assertIn('"--audio-root", $audioArg', script)
        self.assertIn("E1F13CF2532DAADFD6F3BC481A49859F0B8EA6432CCDCD83E6A49A5F19008DE9", script)
        self.assertIn("raw.githubusercontent.com/nltk/nltk_data", script)

    def test_idle_service_periodically_cleans_expired_audio(self):
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "old.wav"
            old.write_bytes(b"RIFF")
            old_time = time.time() - 120
            os.utime(old, (old_time, old_time))
            service = MODULE.TTSService(root, "token", melo_engine=FakeEngine())
            app = MODULE.create_app(
                service,
                cleanup_interval_seconds=0.01,
                cleanup_max_age_seconds=60,
            )

            with TestClient(app):
                deadline = time.time() + 1
                while old.exists() and time.time() < deadline:
                    time.sleep(0.02)

            self.assertFalse(old.exists())

    def test_bind_host_must_be_exact_loopback(self):
        self.assertEqual(MODULE.validate_bind_host("127.0.0.1"), "127.0.0.1")
        for host in ("0.0.0.0", "localhost", "::1", "192.168.1.2"):
            with self.assertRaisesRegex(ValueError, "环回"):
                MODULE.validate_bind_host(host)

    def test_missing_or_wrong_token_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MODULE.TTSService(Path(tmp), "secret-token", melo_engine=FakeEngine())
            with self.assertRaises(MODULE.AuthenticationError):
                service.synthesize("你好", "melo", "")
            with self.assertRaises(MODULE.AuthenticationError):
                service.synthesize("你好", "melo", "wrong")

    def test_missing_gpt_engine_falls_back_to_melo_and_writes_private_wav(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "audio"
            melo = FakeEngine("melo")
            service = MODULE.TTSService(root, "token", melo_engine=melo, gpt_engine=None)
            result = service.synthesize("你好，小柠。", "gpt_sovits", "token")
            path = Path(result["path"])
            self.assertTrue(path.is_file())
            self.assertEqual(path.parent, root.resolve())
            self.assertEqual(path.suffix, ".wav")
            self.assertEqual(melo.calls, ["你好，小柠。"])
            self.assertEqual(result["engine"], "melo")

    def test_gpt_voice_is_disabled_without_explicit_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            melo = FakeEngine("melo")
            gpt = FakeEngine("gpt")
            service = MODULE.TTSService(
                Path(tmp),
                "token",
                melo_engine=melo,
                gpt_engine=gpt,
                authorized_voice=False,
            )

            result = service.synthesize("你好", "gpt_sovits", "token")

            self.assertEqual(result["engine"], "melo")
            self.assertEqual(gpt.calls, [])
            self.assertEqual(melo.calls, ["你好"])

    def test_health_lists_only_authorized_available_engines(self):
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as tmp:
            service = MODULE.TTSService(
                Path(tmp),
                "token",
                melo_engine=FakeEngine("melo"),
                gpt_engine=FakeEngine("gpt"),
                authorized_voice=False,
            )
            client = TestClient(MODULE.create_app(service))

            response = client.get("/health")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"status": "ok", "engines": ["melo"]})

    def test_input_is_bounded_and_engine_name_is_allowlisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MODULE.TTSService(Path(tmp), "token", melo_engine=FakeEngine())
            with self.assertRaisesRegex(ValueError, "不能为空"):
                service.synthesize(" ", "melo", "token")
            with self.assertRaisesRegex(ValueError, "过长"):
                service.synthesize("x" * 601, "melo", "token")
            with self.assertRaisesRegex(ValueError, "引擎"):
                service.synthesize("你好", "shell", "token")

    def test_old_audio_cleanup_never_deletes_non_audio_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_wav = root / "old.wav"
            keep_wav = root / "keep.wav"
            private = root / "private.txt"
            old_wav.write_bytes(b"old")
            keep_wav.write_bytes(b"keep")
            private.write_text("private", encoding="utf-8")
            old_time = time.time() - 1000
            old_wav.touch()
            import os

            os.utime(old_wav, (old_time, old_time))
            MODULE.cleanup_old_audio(root, max_age_seconds=600, now=time.time())
            self.assertFalse(old_wav.exists())
            self.assertTrue(keep_wav.exists())
            self.assertTrue(private.exists())

    def test_http_endpoint_accepts_json_body_and_token_header(self):
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as tmp:
            service = MODULE.TTSService(Path(tmp), "token", melo_engine=FakeEngine())
            client = TestClient(MODULE.create_app(service))
            response = client.post(
                "/synthesize",
                headers={"X-Local-TTS-Token": "token"},
                json={"text": "你好", "voice": "xiaoning", "engine": "melo"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["engine"], "melo")

    def test_http_failure_logs_only_exception_type(self):
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as tmp:
            service = MODULE.TTSService(Path(tmp), "token", melo_engine=FailingEngine())
            client = TestClient(MODULE.create_app(service))
            with self.assertLogs("local_tts_service", level="ERROR") as captured:
                response = client.post(
                    "/synthesize",
                    headers={"X-Local-TTS-Token": "token"},
                    json={"text": "sensitive input", "engine": "melo"},
                )
            self.assertEqual(response.status_code, 503)
            joined = "\n".join(captured.output)
            self.assertIn("RuntimeError", joined)
            self.assertNotIn("private task", joined)
            self.assertNotIn("sensitive input", joined)

    def test_melo_engine_sets_unidic_lite_mecabrc_before_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            dicdir = Path(tmp)
            mecabrc = dicdir / "mecabrc"
            mecabrc.write_text("", encoding="utf-8")
            fake_unidic = types.ModuleType("unidic_lite")
            fake_unidic.DICDIR = str(dicdir)
            fake_full_unidic = types.ModuleType("unidic")
            fake_full_unidic.DICDIR = "missing-full-unidic-dictionary"
            fake_melo = types.ModuleType("melo")
            fake_api = types.ModuleType("melo.api")

            class FakeTTS:
                def __init__(self, language, device):
                    self.hps = types.SimpleNamespace(
                        data=types.SimpleNamespace(spk2id={"ZH": 0})
                    )
                    self.seen_mecabrc = os.environ.get("MECABRC")
                    self.seen_nltk_data = os.environ.get("NLTK_DATA")

            fake_api.TTS = FakeTTS
            with patch.dict(
                sys.modules,
                {
                    "unidic_lite": fake_unidic,
                    "unidic": fake_full_unidic,
                    "melo": fake_melo,
                    "melo.api": fake_api,
                },
            ), patch.dict(os.environ, {}, clear=False):
                os.environ.pop("MECABRC", None)
                engine = MODULE.MeloEngine()
                engine._load()
                self.assertEqual(engine._model.seen_mecabrc, str(mecabrc))
                self.assertEqual(fake_full_unidic.DICDIR, str(dicdir))
                self.assertEqual(
                    engine._model.seen_nltk_data,
                    str(MODULE_PATH.parent / "nltk_data"),
                )


if __name__ == "__main__":
    unittest.main()
