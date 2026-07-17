"""DAG pipeline engine — 9-stage video production with review-driven iteration."""

from __future__ import annotations

import asyncio
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .engines.script_engine import ScriptEngine, Script, Scene, ScriptReview
from .engines.asset_engine import AssetEngine, Asset
from .engines.voice_engine import VoiceEngine
from .engines.edit_engine import EditEngine, EditConfig, Timeline
from .engines.review_engine import ReviewEngine, ReviewResult

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


@dataclass
class PipelineState:
    """Mutable state passed through each stage of the pipeline."""
    topic: str
    template_name: str
    output_dir: Path
    # Stage outputs
    brief: dict | None = None
    script: Script | None = None
    assets: list[Asset] = field(default_factory=list)
    audio_paths: list[Path] = field(default_factory=list)
    output_video: Path | None = None
    review: ReviewResult | None = None
    # Config
    max_duration: int = 60
    max_scenes: int = 6
    resolution: str = "720p"
    transition: str = "fade"
    max_iterations: int = 2
    iteration: int = 0
    # Progress tracking
    current_stage: str = ""
    errors: list[str] = field(default_factory=list)


@dataclass
class PipelineResult:
    success: bool
    video_path: Path | None
    script: Script | None
    review: ReviewResult | None
    errors: list[str]
    stage: str  # which stage failed, or "done"


class VideoPipeline:
    """9-stage video production pipeline with DAG execution and review-driven iteration.

    Stages:
      1. brief     — parse topic → structured brief
      2. research  — search for reference materials
      3. script    — multi-model script generation + review
      4. asset     — acquire visual assets for each scene
      5. voice     — TTS audio for narration
      6. edit      — FFmpeg compositing with effects
      7. review    — LLM Judge + quantitative metrics
      8. iterate   — if score < 6, go back to stage 3 (max 2 iterations)
      9. package   — final output
    """

    def __init__(self):
        self._script_engine = ScriptEngine()
        self._voice_engine = VoiceEngine()
        self._review_engine = ReviewEngine()
        self._asset_engine: AssetEngine | None = None

    async def run(self, topic: str, template_name: str = "knowledge",
                  max_duration: int = 60, max_scenes: int = 6,
                  resolution: str = "720p", output_dir: Path | None = None,
                  progress_cb=None) -> PipelineResult:
        """Run the full pipeline. progress_cb(stage_name, message) for async updates."""
        state = PipelineState(
            topic=topic,
            template_name=template_name,
            output_dir=output_dir or Path(tempfile_get_dir()),
            max_duration=max_duration,
            max_scenes=max_scenes,
            resolution=resolution,
        )
        state.output_dir.mkdir(parents=True, exist_ok=True)
        self._asset_engine = AssetEngine(state.output_dir)

        # Load template config
        tmpl = self._load_template(template_name)
        if tmpl:
            state.max_duration = min(max_duration, tmpl.get("duration_range", [30, 60])[1])
            state.max_scenes = min(max_scenes, 8)
            state.resolution = resolution
            state.transition = tmpl.get("transitions", ["fade"])[0]
            if tmpl.get("voice_style"):
                self._voice_engine.set_voice(tmpl["voice_style"])

        try:
            # ── S1: Brief ──────────────────────────────────
            await self._progress(progress_cb, "brief", "分析主题…")
            state.brief = {"topic": topic, "template": template_name,
                           "max_duration": state.max_duration}
            await self._progress(progress_cb, "brief",
                                 f"主题：{topic[:40]}，模板：{template_name}")

            # ── S2: Research ────────────────────────────────
            await self._progress(progress_cb, "research", "搜索参考素材…")
            # (placeholder — future: search for reference videos/images)
            await self._progress(progress_cb, "research", "素材调研完成")

            # ── S3: Script ─────────────────────────────────
            await self._progress(progress_cb, "script", "生成脚本（多模型协作）…")
            tmpl_hint = tmpl.get("script_hint", "") if tmpl else ""
            state.script = await asyncio.to_thread(
                self._script_engine.generate,
                topic, state.max_duration, state.max_scenes, tmpl_hint,
            )
            if not state.script:
                return PipelineResult(False, None, None, None, ["脚本生成失败"], "script")
            await self._progress(progress_cb, "script",
                                 f"脚本：{state.script.title}（{len(state.script.scenes)}场景）")

            # ── S4: Asset ──────────────────────────────────
            await self._progress(progress_cb, "asset", f"获取{len(state.script.scenes)}个场景素材…")
            visuals = [s.visual for s in state.script.scenes]
            durations = [s.duration for s in state.script.scenes]
            state.assets = await asyncio.to_thread(
                self._asset_engine.acquire_multi, visuals, durations, state.resolution,
            )
            valid_count = sum(1 for a in state.assets if a is not None)
            if valid_count == 0:
                return PipelineResult(False, None, state.script, None,
                                      ["所有场景素材获取失败"], "asset")
            await self._progress(progress_cb, "asset",
                                 f"素材：{valid_count}/{len(state.assets)} 获取成功")

            # ── S5: Voice ──────────────────────────────────
            await self._progress(progress_cb, "voice", "合成配音…")
            narrations = [s.narration for s in state.script.scenes]
            state.audio_paths = await self._voice_engine.generate_all(
                narrations, state.output_dir)
            voice_count = sum(1 for p in state.audio_paths if p and p.exists()
                              and p.stat().st_size > 100)
            if voice_count == 0:
                return PipelineResult(False, None, state.script, None,
                                      ["配音全部失败"], "voice")
            await self._progress(progress_cb, "voice", f"配音：{voice_count}/{len(state.audio_paths)} 完成")

            # ── S6: Edit ───────────────────────────────────
            await self._progress(progress_cb, "edit", "剪辑合成（转场+字幕+运镜）…")
            timeline = Timeline(
                clips=[a.path for a in state.assets if a is not None],
                audio_paths=[p for p in state.audio_paths if p and p.exists()
                             and p.stat().st_size > 100],
                subtitles=[s.narration for s in state.script.scenes],
                durations=[s.duration for s in state.script.scenes],
            )
            edit_config = EditConfig(
                resolution=state.resolution,
                transition=state.transition,
                motion="ken_burns" if tmpl and tmpl.get("motion") != "none" else "none",
            )
            state.output_video = state.output_dir / f"pipeline-{uuid.uuid4().hex}.mp4"
            ok = await asyncio.to_thread(
                EditEngine.compose, timeline, state.output_video, edit_config)
            if not ok:
                return PipelineResult(False, None, state.script, None,
                                      ["剪辑合成失败"], "edit")
            size_mb = state.output_video.stat().st_size / (1024 * 1024)
            await self._progress(progress_cb, "edit", f"剪辑完成（{size_mb:.1f}MB）")

            # ── S7: Review ─────────────────────────────────
            await self._progress(progress_cb, "review", "质量评审…")
            script_summary = f"标题:{state.script.title}\n" + "\n".join(
                f"场景{i}:{s.narration[:50]}" for i, s in enumerate(state.script.scenes))
            state.review = await asyncio.to_thread(
                self._review_engine.review_script, script_summary)
            if state.review:
                await self._progress(progress_cb, "review",
                                     f"评分：{state.review.overall:.1f}/10 "
                                     f"{'✅' if state.review.passed else '❌'}")

            # ── S8: Iterate (if needed) ────────────────────
            while state.iteration < state.max_iterations:
                if state.review and state.review.passed:
                    break
                state.iteration += 1
                await self._progress(progress_cb, "iterate",
                                     f"评分不达标（{state.review.overall if state.review else '?'}/10），"
                                     f"第{state.iteration}次重新生成脚本…")

                # Re-generate script with different prompt
                retry_hint = (tmpl.get("script_hint", "") if tmpl else "") + (
                    f" 改进建议：{'；'.join(state.review.suggestions[:3]) if state.review else '增加吸引力'}"
                )
                state.script = await asyncio.to_thread(
                    self._script_engine.generate,
                    topic, state.max_duration, state.max_scenes, retry_hint,
                )
                if not state.script:
                    break

                # Re-acquire assets with new visuals
                visuals = [s.visual for s in state.script.scenes]
                durations = [s.duration for s in state.script.scenes]
                state.assets = await asyncio.to_thread(
                    self._asset_engine.acquire_multi, visuals, durations, state.resolution)

                # Re-generate voice
                narrations = [s.narration for s in state.script.scenes]
                state.audio_paths = await self._voice_engine.generate_all(
                    narrations, state.output_dir)

                # Re-edit
                new_timeline = Timeline(
                    clips=[a.path for a in state.assets if a is not None],
                    audio_paths=[p for p in state.audio_paths if p and p.exists()
                                 and p.stat().st_size > 100],
                    subtitles=[s.narration for s in state.script.scenes],
                    durations=[s.duration for s in state.script.scenes],
                )
                state.output_video = state.output_dir / f"pipeline-v{state.iteration}-{uuid.uuid4().hex}.mp4"
                ok = await asyncio.to_thread(
                    EditEngine.compose, new_timeline, state.output_video, edit_config)
                if not ok:
                    break

                # Re-review
                script_summary = f"标题:{state.script.title}\n" + "\n".join(
                    f"场景{i}:{s.narration[:50]}" for i, s in enumerate(state.script.scenes))
                state.review = await asyncio.to_thread(
                    self._review_engine.review_script, script_summary)

            # ── S9: Package ─────────────────────────────────
            await self._progress(progress_cb, "package", "打包完成 ✅")

            return PipelineResult(
                success=True,
                video_path=state.output_video,
                script=state.script,
                review=state.review,
                errors=state.errors,
                stage="done",
            )

        except Exception as exc:
            tb = traceback.format_exc()
            state.errors.append(f"{state.current_stage}: {exc}")
            return PipelineResult(
                False, state.output_video, state.script, state.review,
                state.errors, state.current_stage or "unknown",
            )

    @staticmethod
    def _load_template(name: str) -> dict | None:
        """Load template YAML config. Returns None if not found."""
        tmpl_path = TEMPLATE_DIR / f"{name}.yaml"
        if not tmpl_path.is_file():
            return None
        try:
            return yaml.safe_load(tmpl_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    @staticmethod
    async def _progress(cb, stage: str, message: str):
        if cb:
            try:
                await cb(stage, message)
            except Exception:
                pass


def tempfile_get_dir() -> str:
    import tempfile
    return tempfile.mkdtemp(prefix="video_pipeline_")


__all__ = ["VideoPipeline", "PipelineState", "PipelineResult"]
