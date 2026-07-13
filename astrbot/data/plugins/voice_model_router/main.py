"""Route QQ voice messages to Gemini and convert the reply to QQ voice."""

from __future__ import annotations

import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, Record
from astrbot.api.star import Context, Star
from astrbot.core.message.message_event_result import MessageChain

from .local_tts_client import LocalTTSClient
from .audio_merge import merge_wav_files
from .voice_reply_core import prepare_spoken_chunks, wants_voice_reply
from .voice_router_core import contains_voice_component


def _event_text(event: AstrMessageEvent) -> str:
    value = getattr(event, "message_str", "")
    if value:
        return str(value)
    message_obj = getattr(event, "message_obj", None)
    value = getattr(message_obj, "message_str", "")
    if value:
        return str(value)
    getter = getattr(event, "get_message_str", None)
    if callable(getter):
        try:
            return str(getter() or "")
        except Exception:
            return ""
    return ""


class VoiceModelRouter(Star):
    """Keep normal text on DeepSeek and route only voice requests to Gemini Flash."""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}
        project_root = Path(__file__).resolve().parents[4]
        token_file = Path(
            self.config.get(
                "token_file",
                project_root / "claude_workspace" / "state" / "local_tts.token",
            )
        )
        audio_root = Path(
            self.config.get(
                "audio_root", project_root / "claude_workspace" / "tts_audio"
            )
        )
        self.audio_root = audio_root.resolve(strict=False)
        self.tts_client: LocalTTSClient | None = None
        if bool(self.config.get("voice_output_enabled", True)) and token_file.is_file():
            token = token_file.read_text(encoding="utf-8", errors="strict").strip()
            if token:
                self.tts_client = LocalTTSClient(
                    str(self.config.get("tts_endpoint", "http://127.0.0.1:8766")),
                    token,
                    audio_root,
                )

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def route_voice_request(self, event: AstrMessageEvent) -> None:
        explicit_voice = wants_voice_reply(_event_text(event))
        allowed_chat = event.is_private_chat() or event.is_at_or_wake_command
        if explicit_voice and allowed_chat:
            event.set_extra("voice_reply_requested", True)
            event.set_extra("selected_provider", self._best_voice_provider())
            return

        message_obj = getattr(event, "message_obj", None)
        components = getattr(message_obj, "message", None)
        if not contains_voice_component(components):
            return

        # Preserve the existing QQ behavior: private chats can speak directly;
        # group chats still need an @ mention or wake prefix.
        if not event.is_private_chat() and not event.is_at_or_wake_command:
            return
        event.set_extra("selected_provider", self._best_voice_provider())
        # ponytail: voice input → auto voice reply.  User sent a Record
        # so they want spoken output.  No need for "发语音" keyword.
        if self.config.get("auto_voice_reply", True):
            event.set_extra("voice_reply_requested", True)

        # The selected audio-capable provider consumes the Record component
        # in the normal AstrBot request pipeline. Do not start a second,
        # racing transcription request against the same event.

    @filter.on_decorating_result(priority=-10000)
    async def synthesize_voice_reply(self, event: AstrMessageEvent) -> None:
        client = getattr(self, "tts_client", None)
        if client is None:
            return
        transcript = str(
            event.get_extra("_gemini_stt_transcript", "")
            or event.get_extra("_gemini_stt_raw_text", "")
            or ""
        )
        requested = bool(event.get_extra("voice_reply_requested", False)) or wants_voice_reply(
            transcript
        )
        if not requested:
            return
        result = event.get_result()
        if result is None or not result.chain:
            return
        if isinstance(result.chain, str):
            components = [Plain(result.chain)]
        elif isinstance(result.chain, MessageChain):
            components = list(result.chain.chain)
        else:
            components = list(result.chain)
        plain_text = "\n".join(
            component.text for component in components if isinstance(component, Plain)
        ).strip()
        if not plain_text or plain_text.startswith(
            ("已启动", "检测到高风险", "确认码", "当前没有可取消", "任务队列已满")
        ) or ("已排队" in plain_text and plain_text.startswith("任务 ")):
            return
        chunks = prepare_spoken_chunks(plain_text)
        if not chunks:
            return
        audio_paths: list[Path] = []
        for chunk in chunks:
            path = await client.synthesize(chunk)
            if path is None:
                return
            audio_paths.append(path)
        audio_root = Path(getattr(self, "audio_root", "")).resolve(strict=False)
        merged_path = audio_root / f"voice-{uuid.uuid4().hex}.wav"
        try:
            merge_wav_files(audio_paths, merged_path)
        except (OSError, ValueError) as exc:
            logger.warning("[Voice] could not merge local TTS chunks: %s", exc)
            event.set_extra("_local_tts_audio_paths", [str(path) for path in audio_paths])
            return
        non_plain = [component for component in components if not isinstance(component, Plain)]
        result.chain = [Record(file=str(merged_path))] + non_plain
        event.set_extra(
            "_local_tts_audio_paths", [str(path) for path in audio_paths] + [str(merged_path)]
        )
        event.set_extra("voice_reply_emitted", True)

    @filter.after_message_sent(priority=-1000)
    async def cleanup_sent_voice(self, event: AstrMessageEvent) -> None:
        paths = event.get_extra("_local_tts_audio_paths", []) or []
        event.set_extra("_local_tts_audio_paths", [])
        configured_root = getattr(self, "audio_root", None)
        if configured_root is None:
            return
        root = Path(configured_root).resolve(strict=False)
        for raw in paths:
            candidate = Path(str(raw or ""))
            if candidate.is_symlink():
                continue
            try:
                resolved = candidate.resolve(strict=True)
                private_root = root.resolve(strict=True)
            except OSError:
                continue
            if (
                private_root not in resolved.parents
                or resolved.suffix.lower() != ".wav"
                or not resolved.is_file()
            ):
                continue
            try:
                resolved.unlink()
            except OSError:
                continue

    def _best_voice_provider(self) -> str:
        """Return best available Gemini provider for voice: direct API first, proxy fallback."""
        try:
            providers = getattr(self.context, 'provider_manager', None)
            if providers:
                for p in getattr(providers, 'providers', []):
                    pid = getattr(p, 'id', '')
                    if pid == 'gemini-2.5-flash-direct' and getattr(p, 'enable', False):
                        logger.info("[VoiceRouter] Using direct Gemini for voice")
                        return 'gemini-2.5-flash-direct'
        except Exception:
            pass
        logger.info("[VoiceRouter] Falling back to Gemini proxy for voice")
        return 'gemini-2.5-flash'
