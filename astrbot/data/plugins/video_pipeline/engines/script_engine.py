"""Gemini script generation with peer review."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import requests

PROXY_CHAT = "http://127.0.0.1:3000/v1/chat/completions"
GEMINI_RETRY_URL = PROXY_CHAT
import os
GEMINI_RETRY_KEY = "sk-gemini-vertex"


@dataclass
class Scene:
    narration: str
    visual: str
    duration: float


@dataclass
class Script:
    title: str
    scenes: list[Scene]
    total_duration: float = 0.0
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        self.total_duration = sum(s.duration for s in self.scenes)


@dataclass
class ScriptReview:
    score: int  # 1-10
    suggestions: list[str]
    passed: bool  # score >= 6


SYSTEM_PROMPT = """你是专业短视频脚本撰写专家。根据用户主题生成脚本。

返回 JSON：
{{
  "title": "吸引人的标题（≤20字）",
  "hook": "黄金3秒钩子文案（一句话抓住注意力）",
  "scenes": [
    {{"narration": "旁白文案（≤100字，口语化）", "visual": "画面英文关键词（Pexels搜索用）", "duration": 5}}
  ]
}}

规则：
- 总时长 {max_duration} 秒以内
- {max_scenes} 个场景以内
- 第1个场景必须用 hook 作为 narration
- narration 口语化、有节奏、适合朗读
- visual 用英文关键词，具体描述画面内容
- duration 每个场景 4-10 秒
- 结构：hook(3s) → 问题/悬念 → 展开 → 高潮 → 结尾
只返回 JSON，不要其他文字。"""

REVIEW_PROMPT = """你是严苛的视频脚本审核专家。从以下维度评分(1-10)：

1. 钩子吸引力：前3秒能抓住人吗？
2. 节奏控制：场景时长分配合理吗？
3. 画面多样性：visual 描述有多样性吗？
4. 叙事连贯性：场景之间有逻辑衔接吗？
5. 信息价值：看完后观众有收获吗？

返回 JSON：
{{"score": 7, "suggestions": ["建议1", "建议2"], "passed": true}}
passed 为 true 当 score >= 6。只返回 JSON。"""


class ScriptEngine:
    """Gemini script generation with one review and retry."""

    def generate(self, topic: str, max_duration: int = 60,
                 max_scenes: int = 6, template_hint: str = "") -> Script | None:
        system = SYSTEM_PROMPT.format(max_duration=max_duration, max_scenes=max_scenes)
        if template_hint:
            system += f"\n风格倾向：{template_hint}"

        # Step 1: Gemini generates draft
        draft = self._call_gemini(system, topic)
        if not draft:
            draft = self._call_gemini_retry(system, topic)
            if not draft:
                return None
            return self._parse_script(draft)

        # Step 2: Gemini review
        review = self._review(draft)
        if review and review.score < 6:
            retry = self._call_gemini_retry(system, topic)
            if retry:
                retry_script = self._parse_script(retry)
                if retry_script:
                    return retry_script

        return self._parse_script(draft)

    def _call_gemini(self, system: str, topic: str) -> dict | None:
        try:
            resp = requests.post(
                PROXY_CHAT,
                json={
                    "model": "gemini-3.6-flash",
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": f"主题：{topic}"},
                    ],
                    "max_tokens": 2048,
                },
                timeout=(15, 45),
            )
            resp.raise_for_status()
            raw = str(resp.json().get("choices", [{}])[0]
                      .get("message", {}).get("content", "")).strip()
            return self._parse_json(raw)
        except Exception:
            return None

    def _call_gemini_retry(self, system: str, topic: str) -> dict | None:
        if not GEMINI_RETRY_KEY:
            return None
        try:
            resp = requests.post(
                GEMINI_RETRY_URL,
                headers={"Authorization": f"Bearer {GEMINI_RETRY_KEY}"},
                json={
                    "model": "gemini-3.6-flash",
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": f"主题：{topic}"},
                    ],
                    "max_tokens": 2048,
                    "temperature": 0.8,
                },
                timeout=(15, 45),
            )
            resp.raise_for_status()
            raw = str(resp.json().get("choices", [{}])[0]
                      .get("message", {}).get("content", "")).strip()
            return self._parse_json(raw)
        except Exception:
            return None

    def _review(self, raw_script: dict) -> ScriptReview | None:
        if not GEMINI_RETRY_KEY:
            return None
        try:
            resp = requests.post(
                GEMINI_RETRY_URL,
                headers={"Authorization": f"Bearer {GEMINI_RETRY_KEY}"},
                json={
                    "model": "gemini-3.6-flash",
                    "messages": [
                        {"role": "system", "content": REVIEW_PROMPT},
                        {"role": "user", "content": json.dumps(raw_script, ensure_ascii=False)},
                    ],
                    "max_tokens": 512,
                    "temperature": 0.3,
                },
                timeout=(10, 20),
            )
            resp.raise_for_status()
            raw = str(resp.json().get("choices", [{}])[0]
                      .get("message", {}).get("content", "")).strip()
            parsed = self._parse_json(raw)
            if isinstance(parsed, dict):
                return ScriptReview(
                    score=int(parsed.get("score", 5)),
                    suggestions=parsed.get("suggestions", []) or [],
                    passed=parsed.get("passed", False) or int(parsed.get("score", 5)) >= 6,
                )
        except Exception:
            pass
        return None

    @staticmethod
    def _parse_json(raw: str) -> dict | None:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```\s*$", "", raw)
        raw = raw.strip()
        if not raw.startswith("{"):
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _parse_script(data: dict) -> Script | None:
        if not isinstance(data, dict):
            return None
        scenes_raw = data.get("scenes")
        if not isinstance(scenes_raw, list) or not scenes_raw:
            return None
        scenes = []
        for s in scenes_raw:
            if not isinstance(s, dict):
                continue
            scenes.append(Scene(
                narration=str(s.get("narration", "")),
                visual=str(s.get("visual", "")),
                duration=float(s.get("duration", 5) or 5),
            ))
        if not scenes:
            return None
        # Insert hook as first narration if present
        hook = data.get("hook", "")
        if hook and scenes:
            scenes[0].narration = str(hook)
        return Script(
            title=str(data.get("title", "")),
            scenes=scenes,
        )


__all__ = ["ScriptEngine", "Script", "Scene", "ScriptReview"]
