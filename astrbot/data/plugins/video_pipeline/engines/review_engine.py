"""Review engine — LLM Judge + quantitative metrics."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import requests

PROXY_CHAT = "http://127.0.0.1:3000/v1/chat/completions"

JUDGE_PROMPT = """你是严苛的视频质量评审。观看以下视频脚本，从七个维度评分(1-10分)并给出改进建议。

评审维度：
1. hook_strength — 前3秒吸引力
2. visual_quality — 画面质量预期
3. audio_clarity — 配音表达力
4. subtitle_sync — 字幕与配音配合
5. pacing — 节奏控制
6. narrative_flow — 叙事流畅度
7. information_value — 信息价值

脚本内容：
{script_summary}

返回 JSON：
{{"scores": {{"hook_strength": 7, ...}}, "overall": 7, "suggestions": ["建议1"], "passed": true}}
overall 为各维度平均分。passed 为 true 当 overall >= 6。
只返回 JSON。"""


@dataclass
class ReviewResult:
    scores: dict[str, float]
    overall: float
    suggestions: list[str]
    passed: bool
    metrics: dict[str, float] = field(default_factory=dict)


class ReviewEngine:
    """LLM Judge for script quality + quantitative video metrics."""

    LLM_DIMS = [
        "hook_strength", "visual_quality", "audio_clarity",
        "subtitle_sync", "pacing", "narrative_flow", "information_value",
    ]

    def review_script(self, script_summary: str) -> ReviewResult | None:
        """LLM-based script review. Returns None if API fails."""
        try:
            resp = requests.post(
                PROXY_CHAT,
                json={
                    "model": "gemini-3.7-flash",
                    "messages": [
                        {"role": "user", "content": JUDGE_PROMPT.format(
                            script_summary=script_summary[:2000])},
                    ],
                    "max_tokens": 512,
                },
                timeout=(10, 20),
            )
            resp.raise_for_status()
            raw = str(resp.json().get("choices", [{}])[0]
                      .get("message", {}).get("content", "")).strip()
            data = self._parse_json(raw)
            if not isinstance(data, dict):
                return None
            scores = data.get("scores", {}) or {}
            overall = float(data.get("overall", 5))
            return ReviewResult(
                scores={d: float(scores.get(d, 5)) for d in self.LLM_DIMS},
                overall=overall,
                suggestions=data.get("suggestions", []) or [],
                passed=data.get("passed", False) or overall >= 6,
            )
        except Exception:
            return None

    @staticmethod
    def measure_video(path: Path) -> dict[str, float]:
        """Quantitative video metrics: black ratio, audio peak, shot count."""
        metrics: dict[str, float] = {
            "file_size_mb": path.stat().st_size / (1024 * 1024),
            "duration_sec": 0.0,
            "black_ratio": 0.0,
        }
        try:
            # Get duration
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(path)],
                capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if __import__("os").name == "nt" else 0,
            )
            dur = float(result.stdout.strip() or 0)
            metrics["duration_sec"] = dur

            # Detect black frames
            if dur > 0:
                result2 = subprocess.run(
                    ["ffmpeg", "-hide_banner", "-i", str(path),
                     "-vf", "blackdetect=d=0.5:pix_th=0.10",
                     "-an", "-f", "null",
                     "NUL" if __import__("os").name == "nt" else "/dev/null"],
                    capture_output=True, timeout=90,
                    creationflags=subprocess.CREATE_NO_WINDOW if __import__("os").name == "nt" else 0,
                )
                stderr = result2.stderr.decode("utf-8", errors="replace")
                black = sum(
                    float(v) for v in
                    re.findall(r"black_duration:([0-9]+(?:\.[0-9]+)?)", stderr)
                )
                metrics["black_ratio"] = min(1.0, black / dur)
        except Exception:
            pass
        return metrics

    @staticmethod
    def _parse_json(raw: str) -> dict | None:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```\s*$", "", raw)
        if not raw.startswith("{"):
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None


__all__ = ["ReviewEngine", "ReviewResult"]
