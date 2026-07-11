"""Loopback-only local TTS service for QQ voice replies."""

from __future__ import annotations

import argparse
import asyncio
import csv
import hmac
import logging
import os
import subprocess
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field


logger = logging.getLogger("local_tts_service")


class AuthenticationError(RuntimeError):
    pass


class TTSEngine(Protocol):
    def synthesize(self, text: str, output_path: Path) -> None: ...


class SynthesisRequest(BaseModel):
    text: str = Field(min_length=1, max_length=600)
    voice: str = Field(default="xiaoning", max_length=40)
    engine: str = Field(default="gpt_sovits", max_length=20)


def validate_bind_host(host: str) -> str:
    value = str(host or "").strip()
    if value != "127.0.0.1":
        raise ValueError("本地 TTS 必须绑定 127.0.0.1 环回地址")
    return value


def _run_hidden(command: list[str]) -> None:
    subprocess.run(
        command,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _current_user_sid() -> str:
    result = subprocess.run(
        ["whoami.exe", "/user", "/fo", "csv", "/nh"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    sid = next(csv.reader([result.stdout.strip()]))[-1].strip()
    if not sid.startswith("S-1-"):
        raise RuntimeError("无法确认当前用户安全标识")
    return sid


def harden_audio_path(root: Path, files: tuple[Path, ...] = ()) -> None:
    if os.name != "nt":
        return
    sid = _current_user_sid()
    _run_hidden(
        [
            "icacls.exe",
            str(root),
            "/inheritance:r",
            "/grant:r",
            f"*{sid}:(OI)(CI)F",
            "*S-1-5-18:(OI)(CI)F",
            "*S-1-5-32-544:(OI)(CI)F",
        ]
    )
    for path in files:
        if path.exists():
            _run_hidden(
                [
                    "icacls.exe",
                    str(path),
                    "/inheritance:r",
                    "/grant:r",
                    f"*{sid}:F",
                    "*S-1-5-18:F",
                    "*S-1-5-32-544:F",
                ]
            )


def cleanup_old_audio(root: Path, max_age_seconds: int = 600, now: float | None = None) -> int:
    cutoff = (time.time() if now is None else float(now)) - max(60, int(max_age_seconds))
    removed = 0
    for path in Path(root).glob("*.wav"):
        try:
            if not path.is_symlink() and path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


class MeloEngine:
    """Lazy CPU-only MeloTTS adapter; imports stay outside the AstrBot process."""

    def __init__(self):
        self._model = None
        self._speaker_id = None

    def _load(self):
        if self._model is not None:
            return
        import unidic_lite

        mecabrc = Path(unidic_lite.DICDIR) / "mecabrc"
        if not mecabrc.is_file():
            raise RuntimeError("MeloTTS 本地 MeCab 词典不可用")
        os.environ["MECABRC"] = str(mecabrc)
        nltk_data = Path(__file__).resolve().parent / "nltk_data"
        if not (nltk_data / "taggers" / "averaged_perceptron_tagger").is_dir():
            raise RuntimeError("MeloTTS 本地 NLTK 资源不可用")
        os.environ["NLTK_DATA"] = str(nltk_data)
        try:
            import unidic

            unidic.DICDIR = str(Path(unidic_lite.DICDIR))
        except ImportError:
            pass
        from melo.api import TTS

        self._model = TTS(language="ZH", device="cpu")
        speakers = getattr(getattr(self._model, "hps", None), "data", None)
        speaker_map = getattr(speakers, "spk2id", {})
        if not speaker_map:
            raise RuntimeError("MeloTTS 中文声线不可用")
        self._speaker_id = next(iter(speaker_map.values()))

    def synthesize(self, text: str, output_path: Path) -> None:
        self._load()
        self._model.tts_to_file(
            text,
            self._speaker_id,
            str(output_path),
            speed=1.0,
            quiet=True,
        )


class TTSService:
    def __init__(
        self,
        audio_root: Path,
        token: str,
        *,
        melo_engine: TTSEngine | None = None,
        gpt_engine: TTSEngine | None = None,
        authorized_voice: bool = False,
    ):
        normalized_token = str(token or "").strip()
        if not normalized_token or len(normalized_token) > 200:
            raise ValueError("本地 TTS 令牌无效")
        self.audio_root = Path(audio_root).resolve(strict=False)
        self.audio_root.mkdir(parents=True, exist_ok=True)
        harden_audio_path(self.audio_root)
        self.token = normalized_token
        self.melo_engine = melo_engine or MeloEngine()
        self.gpt_engine = gpt_engine
        self.authorized_voice = bool(authorized_voice)

    def available_engines(self) -> list[str]:
        engines = ["melo"] if self.melo_engine is not None else []
        if self.authorized_voice and self.gpt_engine is not None:
            engines.insert(0, "gpt_sovits")
        return engines

    def synthesize(self, text: str, engine: str, token: str) -> dict[str, str]:
        if not hmac.compare_digest(self.token, str(token or "")):
            raise AuthenticationError("unauthorized")
        value = str(text or "").strip()
        if not value:
            raise ValueError("待朗读文字不能为空")
        if len(value) > 600:
            raise ValueError("待朗读文字过长")
        requested = str(engine or "").strip().lower()
        if requested not in {"gpt_sovits", "melo"}:
            raise ValueError("语音引擎无效")
        selected = (
            self.gpt_engine
            if requested == "gpt_sovits" and self.authorized_voice
            else self.melo_engine
        )
        selected_name = requested
        if requested == "gpt_sovits" and not self.authorized_voice:
            selected_name = "melo"
        elif selected is None:
            selected = self.melo_engine
            selected_name = "melo"
        if selected is None:
            raise RuntimeError("本地语音引擎不可用")

        cleanup_old_audio(self.audio_root)
        identifier = uuid.uuid4().hex
        temporary = self.audio_root / f".{identifier}.tmp.wav"
        final = self.audio_root / f"{identifier}.wav"
        try:
            selected.synthesize(value, temporary)
            if not temporary.is_file() or temporary.stat().st_size <= 0:
                raise RuntimeError("本地语音生成失败")
            os.replace(temporary, final)
            harden_audio_path(self.audio_root, (final,))
            return {"path": str(final), "engine": selected_name}
        finally:
            temporary.unlink(missing_ok=True)


async def _periodic_cleanup(
    root: Path,
    interval_seconds: float,
    max_age_seconds: int,
) -> None:
    while True:
        cleanup_old_audio(root, max_age_seconds=max_age_seconds)
        await asyncio.sleep(max(0.01, float(interval_seconds)))


def create_app(
    service: TTSService,
    *,
    cleanup_interval_seconds: float = 60.0,
    cleanup_max_age_seconds: int = 600,
):
    from fastapi import FastAPI, Header, HTTPException

    @asynccontextmanager
    async def lifespan(_app):
        cleanup_task = asyncio.create_task(
            _periodic_cleanup(
                service.audio_root,
                cleanup_interval_seconds,
                cleanup_max_age_seconds,
            )
        )
        try:
            yield
        finally:
            cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup_task

    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health():
        return {"status": "ok", "engines": service.available_engines()}

    @app.post("/synthesize")
    async def synthesize(
        request: SynthesisRequest,
        x_local_tts_token: str = Header(default=""),
    ):
        try:
            return service.synthesize(
                request.text, request.engine, x_local_tts_token
            )
        except AuthenticationError:
            raise HTTPException(status_code=401, detail="unauthorized") from None
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid request") from None
        except Exception as exc:
            logger.error("synthesis_failed type=%s", type(exc).__name__)
            raise HTTPException(status_code=503, detail="synthesis unavailable") from None

    return app


def _default_paths() -> tuple[Path, Path]:
    project_root = Path(__file__).resolve().parents[2]
    state_root = project_root / "claude_workspace" / "state"
    return state_root / "local_tts.token", project_root / "claude_workspace" / "tts_audio"


def main() -> None:
    default_token, default_audio = _default_paths()
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--token-file", type=Path, default=default_token)
    parser.add_argument("--audio-root", type=Path, default=default_audio)
    parser.add_argument("--authorized-voice", action="store_true", default=False)
    args = parser.parse_args()
    host = validate_bind_host(args.host)
    token = args.token_file.read_text(encoding="utf-8").strip()
    service = TTSService(
        args.audio_root,
        token,
        authorized_voice=args.authorized_voice,
    )
    app = create_app(service)
    import uvicorn

    uvicorn.run(app, host=host, port=args.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
