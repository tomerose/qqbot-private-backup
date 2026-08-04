"""/视频工坊 — 全流程高质量视频制作。DAG管线+模板+评分，对标 ShortGPT+ClipForge。

Tiers:
  X:      ≤60s, 基础模板, 1次/天, 720p
  PRO:    ≤180s, 全部模板, 5次/天, 1080p, 评分迭代最多2次
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import traceback
import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .pipeline import VideoPipeline

try:
    from ..draw_command.pro_access import Tier, get_tier
except ImportError:
    from data.plugins.draw_command.pro_access import Tier, get_tier

try:
    from xiaoning_runtime import (
        ArtifactDeliveryResult,
        deliver_local_artifact,
        mirror_runtime_task_status,
    )
except ImportError:
    from data.plugins.xiaoning_runtime import (
        ArtifactDeliveryResult,
        deliver_local_artifact,
        mirror_runtime_task_status,
    )


TEMPLATES = ["knowledge", "storytelling", "product", "news", "vlog"]
TEMPLATE_NAMES = {
    "knowledge": "知识科普",
    "storytelling": "故事叙述",
    "product": "产品展示",
    "news": "资讯速报",
    "vlog": "日常Vlog",
}

TIER_CONFIG = {
    Tier.ORDINARY: (0, 0, 0, "0p", 0),     # no access
    Tier.X: (60, 6, 1, "720p", 0),           # 1/week, no iteration
    Tier.PRO: (180, 10, 5, "1080p", 2),       # 5/day, 2 iterations
}

HELP_TEXT = """🎬 视频工坊 — 全流程AI视频制作

/视频工坊 <主题> [模板] — 制作高质量视频

可用模板：
  knowledge  — 知识科普（教程、技能分享）
  storytelling — 故事叙述（vlog、经历分享）
  product    — 产品展示（介绍、评测）
  news       — 资讯速报（新闻、热点）
  vlog       — 日常Vlog（生活记录）

示例：/视频工坊 如何在家做一杯好喝的拿铁咖啡 knowledge
      /视频工坊 我的第一次徒步旅行 storytelling

当前资格：{tier_info}"""


class VideoPipelinePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}
        self._lock = asyncio.Lock()
        data_dir = Path(StarTools.get_data_dir("video_pipeline"))
        data_dir.mkdir(parents=True, exist_ok=True)
        self._usage_file = data_dir / "pipeline_usage.json"
        self._daily_usage = self._load_usage()
        project_root = Path(__file__).resolve().parents[4]
        self._output_root = project_root / "claude_workspace" / "video_pipeline"
        self._output_root.mkdir(parents=True, exist_ok=True)

    def _pro_db_path(self) -> Path:
        return (
            Path(__file__).resolve().parents[2]
            / "plugin_data" / "xiaoning_pro" / "pro_members.db"
        )

    def _load_usage(self) -> dict[str, int]:
        try:
            raw = json.loads(self._usage_file.read_text(encoding="utf-8"))
            today = time.strftime("%Y%m%d")
            return {
                k: v for k, v in raw.items()
                if k.endswith(f":{today}") and isinstance(v, int) and v >= 0
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}

    def _save_usage(self) -> None:
        tmp = self._usage_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._daily_usage, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._usage_file)

    @staticmethod
    def _sender(event: AstrMessageEvent) -> str:
        g = getattr(event, "get_sender_id", None)
        return str(g() if callable(g) else "").strip()

    @staticmethod
    def _msg(event: AstrMessageEvent) -> str:
        g = getattr(event, "get_message_str", None)
        return str(g() if callable(g) else "").strip()

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=937)
    async def on_message(self, event: AstrMessageEvent):
        text = self._msg(event)
        lowered = text.lower().strip()

        # Only /视频工坊 or natural "帮我做个高质量视频xxx"
        if not (lowered.startswith(("/视频工坊", "/video_workshop", "/workshop"))
                or self._is_natural_pipeline_request(text)):
            return
        if not (event.is_private_chat() or event.is_at_or_wake_command):
            return

        sender_id = self._sender(event)
        if not sender_id.isdigit():
            return
        tier = get_tier(sender_id, self._pro_db_path())
        cfg = TIER_CONFIG.get(tier)
        if cfg is None:
            yield event.plain_result("权限查询异常，请稍后重试。")
            event.stop_event()
            return
        max_dur, max_scenes, daily_limit, resolution, max_iter = cfg

        # Parse command
        if lowered.startswith(("/视频工坊", "/video_workshop", "/workshop")):
            parts = text.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                yield event.plain_result(
                    HELP_TEXT.format(tier_info=f"{tier.value}（{resolution} ≤{max_dur}s {daily_limit}次/天）")
                )
                event.stop_event()
                return
            raw = parts[1].strip()
        else:
            raw = self._extract_natural_topic(text)

        topic, template = self._parse_topic_and_template(raw)

        # Empty topic → show help (don't start pipeline)
        if not topic or len(topic) < 2:
            yield event.plain_result(
                HELP_TEXT.format(tier_info=f"{tier.value}（{resolution} ≤{max_dur}s {daily_limit}次/天）")
            )
            event.stop_event()
            return

        # Tier gating
        if daily_limit == 0:
            yield event.plain_result(
                "视频工坊需要 X 或 Pro 资格。\n"
                "添加小柠为 QQ 好友即可自动获得 X资格（每周 1 次）。\n"
                f"也可以使用 /做视频 <主题> — 快速视频制作。"
            )
            event.stop_event()
            return

        today = time.strftime("%Y%m%d")
        dk = f"{sender_id}:{today}"
        used = self._daily_usage.get(dk, 0)
        if used >= daily_limit:
            yield event.plain_result(
                f"今日视频工坊次数已用完（{used}/{daily_limit}）。明天自动重置。"
                + (f"\nPro 每天 5 次。" if tier < Tier.PRO else "")
            )
            event.stop_event()
            return

        if self._lock.locked():
            yield event.plain_result("正在制作另一个视频，请等这个完成后再试。")
            event.stop_event()
            return

        task_id = uuid.uuid4().hex[:12]
        task_desc = f"视频工坊：{topic[:80]}"
        await mirror_runtime_task_status(
            sender_id,
            task_id,
            task_desc,
            "in_progress",
            "pipeline_started",
            owner="video_pipeline",
        )
        yield event.plain_result(
            f"🎬 视频工坊启动 — 全流程高质量制作\n"
            f"主题：{topic[:40]}\n"
            f"模板：{TEMPLATE_NAMES.get(template, template)}\n"
            f"分辨率：{resolution}  时长上限：{max_dur}s\n"
            f"管线：脚本→素材→配音→剪辑→评审→交付\n"
            f"预计 3–8 分钟，进度实时播报。"
        )

        try:
            async with self._lock:
                pipeline = VideoPipeline()
                last_stage = ""

                async def progress_cb(stage: str, msg: str):
                    nonlocal last_stage
                    if stage != last_stage:
                        last_stage = stage
                        yield event.plain_result(f"⏳ [{stage}] {msg}")

                # Collect progress messages
                progress_msgs: list[str] = []

                async def collect_progress(stage: str, msg: str):
                    progress_msgs.append(f"[{stage}] {msg}")

                # Run pipeline
                result = await pipeline.run(
                    topic=topic,
                    template_name=template,
                    max_duration=max_dur,
                    max_scenes=max_scenes,
                    resolution=resolution,
                    output_dir=self._output_root,
                    progress_cb=collect_progress,
                )

                # Send progress summary
                for msg in progress_msgs:
                    yield event.plain_result(f"⏳ {msg}")

                if not result.success:
                    await mirror_runtime_task_status(
                        sender_id,
                        task_id,
                        task_desc,
                        "failed",
                        f"pipeline:{result.stage}",
                        owner="video_pipeline",
                    )
                    errors = "\n".join(result.errors[-3:]) if result.errors else "未知错误"
                    yield event.plain_result(
                        f"❌ 视频工坊制作失败（{result.stage}阶段）\n"
                        f"{errors}\n"
                        f"本次次数不计。可以使用 /做视频 快速出片。"
                    )
                    event.stop_event()
                    return

                # ── Deliver to QQ ──────────────────────────
                if not result.video_path or not result.video_path.is_file():
                    await mirror_runtime_task_status(
                        sender_id,
                        task_id,
                        task_desc,
                        "failed",
                        "missing_output",
                        owner="video_pipeline",
                    )
                    yield event.plain_result("视频文件丢失，本次次数不计。")
                    event.stop_event()
                    return

                delivery = await deliver_local_artifact(
                    event, result.video_path,
                    allowed_roots=[self._output_root], kind="file",
                    task_id=task_id, task_desc=task_desc,
                    task_owner="video_pipeline",
                )
                if delivery.delivered:
                    self._daily_usage[dk] = used + 1
                    try:
                        self._save_usage()
                    except OSError:
                        pass
                    await mirror_runtime_task_status(
                        sender_id,
                        task_id,
                        task_desc,
                        "done",
                        f"qq:{delivery.channel}",
                        owner="video_pipeline",
                    )
                else:
                    await mirror_runtime_task_status(
                        sender_id,
                        task_id,
                        task_desc,
                        "delivery_pending",
                        delivery.channel,
                        owner="video_pipeline",
                    )

                review_line = ""
                if result.review:
                    emoji = "✅" if result.review.passed else "⚠️"
                    review_line = (
                        f"\n评分：{result.review.overall:.1f}/10 {emoji}"
                    )
                    if result.review.suggestions:
                        review_line += f"\n建议：{'；'.join(result.review.suggestions[:2])}"

                title = result.script.title if result.script else topic[:20]
                yield event.plain_result(
                    f"🎬 视频工坊 «{title}» 完成{review_line}\n"
                    + (f"已发送到QQ。剩余：{daily_limit - used - 1}/{daily_limit}"
                       if delivery.delivered else
                       f"{'已加入重试队列' if delivery.channel == 'queued' else '文件保留，请稍后重试'}。"
                       f"本次次数不计。")
                )

        except Exception as exc:
            logger.warning("[VideoPipeline] unexpected: %s\n%s", exc, traceback.format_exc())
            await mirror_runtime_task_status(
                sender_id,
                task_id,
                task_desc,
                "failed",
                type(exc).__name__,
                owner="video_pipeline",
            )
            yield event.plain_result(f"视频工坊异常（{type(exc).__name__}），本次次数不计。")
        event.stop_event()

    @staticmethod
    def _is_natural_pipeline_request(text: str) -> bool:
        """Natural language detection for high-quality video requests.

        Matches:
          - "帮我做一个高质量的视频关于xxx" (prefix + verb + quality + 视频)
          - "高质量视频xxx" (bare quality + 视频)
          - "视频工坊xxx" / "视频工作室xxx" (explicit workshop naming)
        Does NOT match (passes through to video_command/video_agent):
          - "帮我做视频xxx" (no quality keyword)
          - "生成视频xxx" (AI generation intent)
          - "/视频" or "/做视频" (explicit other commands)
        """
        import re
        patterns = [
            # Prefix + quality + topic + 视频: "帮我做一个高质量的拿铁科普视频"
            r"(?:帮我|请|给我|来|想|想要?)\s*(?:做|制作|弄|搞|生成|整)\s*"
            r"(?:一?[个段部]\s*)?"
            r"(?:高质量|专业|精美|漂亮|好看|酷|炫|电影级)\s*"
            r"(?:的)?\s*.{1,80}?(?:视频|短片)",
            # Prefix + verb + quality + 视频: "帮我做一个高质量的视频xxx"
            r"(?:帮我|请|给我|来|想|想要?)\s*(?:做|制作|弄|搞|生成|整)\s*"
            r"(?:一?[个段部]\s*)?"
            r"(?:高质量|专业|精美|漂亮|好看|酷|炫|电影级)\s*"
            r"(?:的)?\s*"
            r"(?:视频|短片)",
            # Bare quality + 视频: "高质量视频xxx", "专业短片xxx"
            r"(?:高质量|专业|精美|电影级)\s*"
            r"(?:的)?\s*"
            r"(?:视频|短片)",
            # 视频 + workshop keyword: "视频工坊...", "短片工作室..."
            r"(?:视频|短片)\s*"
            r"(?:工坊|工作室|全流程)",
        ]
        for pat in patterns:
            if re.search(pat, text, re.I):
                # Exclude capability questions
                if re.search(r"(?:能不能|可不可以|可以|会|怎么|为什么|是不是|真的假的)\s*(?:做|制作|搞)?\s*(?:高质量|专业)?\s*(?:视频|短片)", text, re.I):
                    if not re.search(r"(?:帮我|请|想要?|想|来|要)\s*(?:做|制作|搞|生成)", text, re.I):
                        return False
                return True
        return False

    @staticmethod
    def _extract_natural_topic(text: str) -> str:
        """Extract topic from natural language pipeline request.

        Returns empty string if topic can't be extracted (will trigger help display).
        """
        import re
        patterns = [
            # "帮我做一个高质量的拿铁科普视频" → "拿铁科普"
            r"(?:帮我|请|给我|来|想|想要?)\s*(?:做|制作|弄|搞|生成|整)\s*"
            r"(?:一?[个段部]\s*)?"
            r"(?:高质量|专业|精美|漂亮|好看|酷|炫|电影级)\s*"
            r"(?:的)?\s*(.+?)\s*(?:视频|短片)\s*$",
            # "帮我做一个高质量的视频关于xxx" → "关于xxx"
            r"(?:帮我|请|给我|来|想|想要?)\s*(?:做|制作|弄|搞|生成|整)\s*"
            r"(?:一?[个段部]\s*)?"
            r"(?:高质量|专业|精美|漂亮|好看|酷|炫|电影级)\s*"
            r"(?:的)?\s*"
            r"(?:视频|短片)\s*"
            r"[,，:：\s]*"
            r"(.+)",
            # "高质量视频xxx" → "xxx"
            r"(?:高质量|专业|精美|电影级)\s*"
            r"(?:的)?\s*"
            r"(?:视频|短片)\s*"
            r"[,，:：\s]*"
            r"(.+)",
        ]
        for pat in patterns:
            m = re.search(pat, text, re.I)
            if m:
                topic = m.group(1).strip()
                # Strip noise tails
                topic = re.sub(r"[吗呢嘛吧啊][？?！!。.]*$", "", topic).strip()
                return topic if topic and len(topic) <= 500 else ""
        # Fallback: use full text minus command prefix
        cleaned = re.sub(r"^(?:/视频工坊|/video_workshop|/workshop)\s*", "", text).strip()
        return cleaned[:500]

    @staticmethod
    def _parse_topic_and_template(raw: str) -> tuple[str, str]:
        """Parse 'topic [template]' from raw input."""
        parts = raw.strip().rsplit(maxsplit=1)
        if len(parts) == 2 and parts[1].lower() in TEMPLATES:
            return parts[0].strip(), parts[1].lower()
        # Check Chinese template names
        cn_map = {v: k for k, v in TEMPLATE_NAMES.items()}
        if len(parts) == 2 and parts[1] in cn_map:
            return parts[0].strip(), cn_map[parts[1]]
        return raw.strip(), "knowledge"  # default template

    async def terminate(self):
        """Cleanup old output files."""
        try:
            now = time.time()
            for f in self._output_root.glob("pipeline-*.mp4"):
                if now - f.stat().st_mtime > 3 * 86400:
                    f.unlink(missing_ok=True)
        except Exception:
            pass
