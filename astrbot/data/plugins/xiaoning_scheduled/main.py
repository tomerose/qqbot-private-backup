"""小柠定时推送 — GitHub趋势、早安图文、天气播报"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import feedparser
import requests
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, StarTools

try:
    from draw_command.pro_access import Tier, get_tier
except ImportError:
    from data.plugins.draw_command.pro_access import Tier, get_tier

PROXY_CHAT = "http://127.0.0.1:3000/v1/chat/completions"
PROXY_IMAGE = "http://127.0.0.1:3000/v1/images/generations"
_NAPCAT_TOKEN = os.environ.get("NAPCAT_HTTP_TOKEN", "").strip()
_NAPCAT_HEADERS = {"Authorization": f"Bearer {_NAPCAT_TOKEN}"} if _NAPCAT_TOKEN else {}
PLUGIN_DIR = Path(__file__).resolve().parent
_NCM_DECODER = Path("D:/Claudecoda学习/DEEP营考核平台/scripts/decode-ncm.mjs")

# RSS 源 — HN(稳定)+AI垂直媒体+Google AI生态
RSS_FEEDS = [
    "https://hnrss.org/frontpage?count=10&points=5",
    "https://www.artificialintelligence-news.com/feed/",
    "https://blog.google/technology/ai/rss/",
    "https://blog.research.google/feeds/posts/default?alt=rss&max-results=8",
]

DEFAULT_CONFIG = {
    "github_trending_enabled": False,
    "github_trending_time": "09:00",
    "github_trending_groups": [],
    "morning_post_enabled": False,
    "morning_time": "07:30",
    "morning_groups": [],
    "weather_enabled": False,
    "weather_city": "Shanghai",
    "weather_time": "07:00",
    "weather_groups": [],
    "owner_id": "1211000567",
    "group_summary_enabled": False,
    "group_summary_time": "22:00",
    "group_summary_group": "1075963106",
    "group_summary_target": "1211000567",
    "ai_news_enabled": True,
    "ai_news_time": "07:00",
    "zhoushen_daily_enabled": True,
    "zhoushen_daily_time": "23:00",
    "zhoushen_daily_groups": ["1058848055"],
    "zhoushen_song_enabled": False,
    "zhoushen_song_time": "23:01",
    "zhoushen_song_groups": ["1058848055"],
    "zhoushen_meme_enabled": True,
    "zhoushen_meme_time": "23:02",
    "zhoushen_meme_groups": ["1058848055"],
}


class XiaoningScheduled(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        data_dir = Path(StarTools.get_data_dir("xiaoning_scheduled"))
        data_dir.mkdir(parents=True, exist_ok=True)
        self._runtime_file = data_dir / "runtime.json"
        self._runtime = self._load_json(self._runtime_file)
        self._opt_in_file = data_dir / "ai_news_opt_in.json"
        self._pro_db = (
            Path(__file__).resolve().parents[4]
            / "astrbot" / "data" / "plugin_data" / "xiaoning_pro" / "pro_members.db"
        )
        self.config.update(self._runtime.get("overrides", {}))
        self._bot = None
        self._loop: asyncio.Task | None = None

    # ── lifecycle ───────────────────────────────────────────────

    async def initialize(self):
        self._loop = asyncio.create_task(self._scheduler_loop())
        logger.info("[小柠定时] 调度已启动")

    async def terminate(self):
        if self._loop:
            self._loop.cancel()
            try:
                await self._loop
            except asyncio.CancelledError:
                pass

    # ── scheduler ───────────────────────────────────────────────

    async def _scheduler_loop(self):
        while True:
            try:
                await self._check_and_fire()
            except Exception as exc:
                logger.error(f"[小柠定时] 调度异常: {exc}")
            await asyncio.sleep(60)

    async def _check_and_fire(self):
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        current = now.strftime("%H:%M")

        # Trigger file for manual push — touch this file to fire immediately
        trigger = self._opt_in_file.parent / "trigger_ainews"
        if trigger.exists():
            logger.info("[小柠定时] 手动触发 AI 早报")
            if await self._push_ai_news() is False:
                return
            trigger.unlink(missing_ok=True)
            self._runtime["ainews"] = today
            self._save_json(self._runtime_file, self._runtime)
            return

        tasks = [
            (self.config["github_trending_enabled"], self.config["github_trending_time"], "gh", self._push_github_trending),
            (self.config["morning_post_enabled"], self.config["morning_time"], "morning", self._push_morning_post),
            (self.config["weather_enabled"], self.config["weather_time"], "weather", self._push_weather),
            (self.config["group_summary_enabled"], self.config["group_summary_time"], "summary", self._push_group_summary),
            (self.config["ai_news_enabled"], self.config["ai_news_time"], "ainews", self._push_ai_news),
            (self.config["zhoushen_daily_enabled"], self.config["zhoushen_daily_time"], "zhoushen", self._push_zhoushen_daily),
            (self.config["zhoushen_song_enabled"], self.config["zhoushen_song_time"], "zhoushensong", self._push_zhoushen_song),
            (self.config["zhoushen_meme_enabled"], self.config["zhoushen_meme_time"], "zhoushenmeme", self._push_zhoushen_meme),
        ]
        for enabled, time_str, last_key, handler in tasks:
            if enabled and current == time_str and self._runtime.get(last_key) != today:
                if await handler() is False:
                    continue
                self._runtime[last_key] = today
                self._save_json(self._runtime_file, self._runtime)

    # ── bot client ──────────────────────────────────────────────

    async def _get_bot(self):
        # ponytail: cached bot client, refresh on reconnect would need
        # re-fetch but this is fine for low-frequency scheduled sends
        if self._bot is not None:
            return self._bot
        for _ in range(12):
            for inst in self.context.platform_manager.platform_insts:
                client = inst.get_client()
                if hasattr(client, "send_group_msg"):
                    self._bot = client
                    return self._bot
            await asyncio.sleep(5)
        return None

    async def _resolve_groups(self, configured: list[str] | str) -> list[str]:
        """Only explicit group IDs are pushed; '*' opts into every group."""
        if isinstance(configured, str):
            values = [item.strip() for item in re.split(r"[,，\s]+", configured) if item.strip()]
        else:
            values = [str(item).strip() for item in configured if str(item).strip()]
        if values and "*" not in values:
            return [gid for gid in values if gid.isdigit()]
        if "*" not in values:
            return []
        bot = await self._get_bot()
        if not bot:
            return []
        try:
            groups = await bot.get_group_list()
            return [str(g["group_id"]) for g in groups]
        except Exception:
            return []

    async def _send_text(self, group_ids: list[str], text: str):
        gids = await self._resolve_groups(group_ids)
        bot = await self._get_bot()
        if not bot or not gids:
            return
        for gid in gids:
            try:
                await bot.send_group_msg(group_id=int(gid), message=text)
            except Exception as e:
                logger.warning(f"[小柠定时] 文本→{gid} 失败: {e}")
            await asyncio.sleep(0.5)

    async def _send_image(self, group_ids: list[str], path: Path):
        if not path.exists():
            return
        gids = await self._resolve_groups(group_ids)
        bot = await self._get_bot()
        if not bot or not gids:
            return
        cq = f"[CQ:image,file=file:///{path.as_posix()}]"
        for gid in gids:
            try:
                await bot.send_group_msg(group_id=int(gid), message=cq)
            except Exception as e:
                logger.warning(f"[小柠定时] 图片→{gid} 失败: {e}")
            await asyncio.sleep(0.5)

    # ── content: GitHub trending ─────────────────────────────────

    def _fetch_github_trending(self) -> str:
        """解析 GitHub Trending HTML，失败则回退到搜索 API。"""
        try:
            html = requests.get(
                "https://github.com/trending",
                headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"},
                timeout=15,
            ).text
            articles = re.findall(
                r'<article[^>]*class="Box-row"[^>]*>(.*?)</article>',
                html, re.DOTALL,
            )
            if not articles:
                raise ValueError("未找到 trending 条目")

            lines = ["【GitHub 今日热门】"]
            count = 0
            for art in articles:
                if count >= 5:
                    break
                repo_m = re.search(r'<h2[^>]*>.*?href="/([^"]+)"', art, re.DOTALL)
                if not repo_m:
                    continue
                repo = repo_m.group(1).strip()
                if "/" not in repo or repo.startswith("trending"):
                    continue

                desc_m = re.search(
                    r'<p class="[^"]*color-fg-muted[^"]*"[^>]*>\s*(.*?)\s*</p>',
                    art, re.DOTALL,
                )
                desc = re.sub(r"<[^>]+>", "", desc_m.group(1).strip())[:60] if desc_m else ""

                stars_m = re.search(r"(\d[\d,]*)\s*stars today", art)
                stars = stars_m.group(1) if stars_m else "?"

                count += 1
                lines.append(f"{count}. {repo} ⭐{stars} — {desc}")
            return "\n".join(lines)
        except Exception:
            pass

        # fallback: GitHub search API (public, rate-limited)
        try:
            resp = requests.get(
                "https://api.github.com/search/repositories",
                params={"q": "stars:>1000", "sort": "stars", "order": "desc", "per_page": 5},
                timeout=15,
            )
            items = resp.json().get("items", [])
            if not items:
                return "【GitHub 今日热门】\n暂无数据"
            lines = ["【GitHub 今日热门】"]
            for i, item in enumerate(items[:5], 1):
                name = item.get("full_name", "?")
                stars = item.get("stargazers_count", 0)
                desc = (item.get("description") or "")[:40]
                lines.append(f"{i}. {name} ⭐{stars} — {desc}")
            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"[小柠定时] GitHub 趋势获取失败: {e}")
            return "【GitHub 今日热门】\n获取失败，稍后自动重试"

    # ── content: morning quote + image ───────────────────────────

    def _fetch_morning_quote(self) -> str:
        try:
            resp = requests.post(
                PROXY_CHAT,
                json={
                    "model": "gemini-3.6-flash",
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "你是温暖的朋友。生成一句励志早安语录，中英双语。"
                                "只输出内容，不要前缀或解释。格式：\n"
                                "中文：xxx\nEnglish: xxx"
                            ),
                        },
                        {"role": "user", "content": "早安语录"},
                    ],
                    "max_tokens": 150,
                },
                timeout=30,
            )
            text = resp.json()["choices"][0]["message"]["content"].strip()
            return f"☀️ 早安！\n\n{text}"
        except Exception as e:
            logger.warning(f"[小柠定时] 早安语录失败: {e}")
            return "☀️ 早安！新的一天，加油！\nGood morning! A new day, keep going!"

    def _generate_morning_image(self) -> Path | None:
        try:
            resp = requests.post(
                PROXY_IMAGE,
                json={
                    "prompt": (
                        "A beautiful peaceful morning scene with soft warm sunlight, "
                        "gentle clouds, green hills or a cozy window view. Calm, inspiring, "
                        "soft pastel colors, suitable for a morning greeting card. "
                        "No text or letters in the image."
                    ),
                    "model": "gemini-3.1-flash-image",
                    "size": "1024x1024",
                },
                timeout=(30, 180),
            )
            body = resp.json()
            data = body.get("data", [])
            if not data or not data[0].get("b64_json"):
                return None
            img_bytes = base64.b64decode(data[0]["b64_json"])

            out_dir = PLUGIN_DIR / "images"
            out_dir.mkdir(exist_ok=True)
            path = out_dir / f"morning-{uuid.uuid4().hex}.png"
            path.write_bytes(img_bytes)
            return path
        except Exception as e:
            logger.warning(f"[小柠定时] 早安图生成失败: {e}")
            return None

    # ── content: weather ─────────────────────────────────────────

    def _fetch_weather(self) -> str:
        city = self.config.get("weather_city", "Shanghai")
        try:
            resp = requests.get(
                f"https://wttr.in/{city}?format=%C+%t+%h+%w",
                timeout=10,
            )
            return f"【今日天气 · {city}】\n{resp.text.strip()}"
        except Exception:
            return f"【今日天气 · {city}】\n获取失败，稍后自动重试"

    # ── push tasks ──────────────────────────────────────────────

    async def _push_github_trending(self):
        logger.info("[小柠定时] GitHub 趋势")
        text = await asyncio.to_thread(self._fetch_github_trending)
        await self._send_text(self.config["github_trending_groups"], text)

    async def _push_morning_post(self):
        logger.info("[小柠定时] 早安推送")
        quote = await asyncio.to_thread(self._fetch_morning_quote)
        img = await asyncio.to_thread(self._generate_morning_image)
        groups = self.config["morning_groups"]
        if img:
            await self._send_text(groups, quote)
            await asyncio.sleep(0.3)
            await self._send_image(groups, img)
            asyncio.create_task(self._cleanup_image(img))
        else:
            await self._send_text(groups, quote + "\n（早安图生成失败）")

    async def _push_weather(self):
        logger.info("[小柠定时] 天气播报")
        text = await asyncio.to_thread(self._fetch_weather)
        await self._send_text(self.config["weather_groups"], text)

    # ── push: group summary ───────────────────────────────────────

    async def _push_group_summary(self):
        logger.info("[小柠定时] 群聊总结")
        bot = await self._get_bot()
        if not bot:
            logger.warning("[小柠定时] 群聊总结失败: 无 bot 客户端")
            return
        group_id = int(self.config["group_summary_group"])
        target_qq = int(self.config["group_summary_target"])
        summary = await self._fetch_group_summary(bot, group_id)
        if not summary:
            return
        date_str = datetime.now().strftime("%Y-%m-%d")
        try:
            await bot.send_private_msg(
                user_id=target_qq,
                message=f"【群 {group_id} 今日总结 · {date_str}】\n\n{summary}",
            )
        except Exception as e:
            logger.warning(f"[小柠定时] 私聊发送总结失败: {e}")

    async def _fetch_group_summary(self, bot, group_id: int) -> str:
        try:
            result = await bot.api.call_action(
                "get_group_msg_history",
                group_id=group_id,
                message_seq=0,
                count=200,
            )
        except Exception as e:
            logger.warning(f"[小柠定时] 获取群聊历史失败: {e}")
            return ""
        messages = result.get("messages", [])
        if not messages:
            return "今日暂无群聊消息。"
        lines = []
        for msg in reversed(messages):
            sender_data = msg.get("sender", {})
            sender = sender_data.get("nickname", sender_data.get("user_id", "?"))
            text_segments = [
                seg.get("data", {}).get("text", "")
                for seg in msg.get("message", [])
                if seg.get("type") == "text"
            ]
            text = "".join(text_segments).strip()
            if text:
                lines.append(f"{sender}：{text}")
        if not lines:
            return "今日暂无文字消息。"
        context = "\n".join(lines)
        try:
            resp = await asyncio.to_thread(
                requests.post,
                PROXY_CHAT,
                json={
                    "model": "gemini-3.6-flash",
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "你是群聊总结助手。用简洁中文总结今日群聊要点："
                                "讨论了什么话题、有什么决定或结论、亮点或待办。"
                                "控制在300字以内。不要编造没有的内容。"
                            ),
                        },
                        {"role": "user", "content": f"今日群聊记录：\n{context}"},
                    ],
                    "max_tokens": 600,
                },
                timeout=30,
            )
            return resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.warning(f"[小柠定时] LLM 总结失败: {e}")
            return ""

    # ── push: AI news ────────────────────────────────────────────

    def _load_opt_ins(self) -> set[str]:
        return set(self._load_json(self._opt_in_file).get("opted_in", []))

    def _save_opt_ins(self, opted_in: set[str]):
        self._save_json(self._opt_in_file, {"opted_in": list(opted_in)})

    def _get_eligible_news_subscribers(self) -> list[str]:
        """Return opted-in QQ IDs that are currently active Pro/X."""
        opted_in = self._load_opt_ins()
        if not opted_in:
            return []
        eligible = []
        for qq_id in opted_in:
            tier = get_tier(qq_id, self._pro_db)
            if tier >= Tier.X:
                eligible.append(qq_id)
        return eligible

    async def _push_ai_news(self):
        logger.info("[小柠定时] AI 早报")
        subscribers = self._get_eligible_news_subscribers()
        if not subscribers:
            logger.info("[小柠定时] AI 早报: 无订阅用户")
            return False
        bot = await self._get_bot()
        if not bot:
            logger.warning("[小柠定时] AI 早报失败: 无 bot 客户端")
            return False
        news_text = await asyncio.to_thread(self._fetch_ai_news)
        if not news_text:
            return False
        # Content already has full structure — no extra prefix needed
        sent = 0
        for qq_id in subscribers:
            try:
                await bot.send_private_msg(
                    user_id=int(qq_id),
                    message=news_text,
                )
                sent += 1
                await asyncio.sleep(0.8)
            except Exception as e:
                logger.warning("[小柠定时] AI 早报发送失败: %s", type(e).__name__)
        return sent > 0

    def _scrape_rss(self) -> list[str]:
        """拉取 RSS 头条，返回标题+摘要列表。失败返回空。"""
        items = []
        for url in RSS_FEEDS:
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries[:8]:
                    title = entry.get("title", "").strip()
                    link = entry.get("link", "").strip()
                    summary = re.sub(r"<[^>]+>", "", entry.get("summary", "") or "")[:200]
                    if title:
                        items.append(f"- {title}\n  {link}\n  {summary}")
            except Exception as e:
                logger.debug(f"[小柠定时] RSS 拉取失败 {url[:50]}: {e}")
        return items[:25]

    @staticmethod
    def _rss_fallback(headlines: list[str]) -> str:
        terms = (
            "ai", "artificial intelligence", "agent", "claude", "gemini",
            "openai", "model", "llm", "机器学习", "人工智能", "大模型",
        )
        selected = [item for item in headlines if any(term in item.lower() for term in terms)]
        if len(selected) < 3:
            selected.extend(item for item in headlines if item not in selected)
        lines = ["📡 今日 AI 资讯（公开 RSS 标题速览）"]
        for item in selected[:5]:
            parts = [part.strip() for part in item.splitlines() if part.strip()]
            title = parts[0].removeprefix("- ")
            link = next((part for part in parts[1:] if part.startswith(("http://", "https://"))), "")
            lines.append(f"• {title}" + (f"\n  {link}" if link else ""))
        lines.append("\n上游摘要暂不可用，以上标题来自实时 RSS，请点来源核对详情。")
        return "\n".join(lines)

    @staticmethod
    def _validate_llm_response(resp: requests.Response) -> str | None:
        """验证 LLM 响应并提取 content，失败返回 None。"""
        try:
            body = resp.json()
        except Exception:
            return None
        if "error" in body:
            logger.debug(f"[小柠定时] LLM API 错误: {body['error']}")
            return None
        try:
            return body["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError):
            return None

    def _fetch_ai_news(self) -> str:
        """Generate morning briefing with Google Search grounding via Gemini proxy.

        Primary: Gemini 3.6 Flash Search — live web-grounded briefing with curated
        analysis. Falls back to RSS scraping when the proxy is unavailable.
        """
        today = datetime.now().strftime("%Y年%m月%d日 周%u").replace(
            "周1", "周一").replace("周2", "周二").replace("周3", "周三").replace(
            "周4", "周四").replace("周5", "周五").replace("周6", "周六").replace("周7", "周日")

        system_prompt = (
            "你是小柠，一个判断清楚、表达自然的资讯编辑。用联网搜索获取今天最新新闻，"
            "生成一份高质量的早间简报。严格按指定 Markdown 格式输出，"
            "每条新闻必须附真实来源链接。小柠分析部分要有独立思考，"
            "不堆砌事实。禁止用方括号占位符。"
        )
        user_msg = (
            f"{today}\n\n"
            "## 🌅 早间简报\n\n"
            "### 🌍 国际要闻（3条）\n"
            "每条：标题、1-2句事实概述、可信来源链接。优先选择对中国读者有影响的全球事件。\n\n"
            "### 🔍 小柠分析\n"
            "从今天的新闻中提炼2-3个核心趋势。不重复新闻。回答：这些事件之间的联系是什么？"
            "对普通中国人可能意味着什么？有什么被主流叙事忽略的角度？理性、不煽情、不站队。\n\n"
            "### 💻 科技动态（2条）\n"
            "重要的科技/AI新闻，附来源链接，说明为什么值得关注。\n\n"
            "### 💡 今日提醒\n"
            "结合今天日期或新闻给一条贴近生活的实用建议。\n\n"
            "---\n"
            "由小柠自动生成 · 回复「早报关闭」可退订"
        )

        # ── Primary: Gemini with Google Search grounding ──
        try:
            resp = requests.post(
                PROXY_CHAT,
                json={
                    "model": "gemini-3.6-flash-search",
                    "google_search": True,
                    "google_maps": False,
                    "code_execution": False,
                    "url_context": True,
                    "max_tokens": 3500,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                },
                timeout=(15, 90),
            )
            content = self._validate_llm_response(resp)
            if content and len(content) >= 200:
                return content
            logger.warning(f"[小柠定时] Gemini search 早报无效 len={len(content or '')}")
        except Exception as e:
            logger.warning(f"[小柠定时] Gemini search 早报失败: {type(e).__name__}")

        # ── Fallback 1: regular Gemini + RSS headlines ──
        headlines = self._scrape_rss()
        if headlines:
            try:
                resp = requests.post(
                    PROXY_CHAT,
                    json={
                        "model": "gemini-3.6-flash",
                        "max_tokens": 2000,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": (
                                f"以下是今日 RSS 头条素材：\n\n"
                                + "\n".join(headlines[:20])
                                + f"\n\n{today}\n请从素材中选取最重要的新闻，生成早间简报。"
                                "只使用素材中真实存在的新闻，标注来源。素材不足的部分可以简化。"
                            )},
                        ],
                    },
                    timeout=(15, 60),
                )
                content = self._validate_llm_response(resp)
                if content and len(content) >= 100:
                    return content
            except Exception as e:
                logger.warning(f"[小柠定时] Gemini RSS 早报失败: {type(e).__name__}")

        # ── Fallback 2: pure RSS text ──
        logger.warning("[小柠定时] AI 早报所有来源均失败，使用纯 RSS 回退")
        return self._rss_fallback(headlines) if headlines else ""

    # ── cleanup ───────────────────────────────────────────────────

    async def _cleanup_image(self, path: Path, delay: int = 120):
        await asyncio.sleep(delay)
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass

    # ── persistence ─────────────────────────────────────────────

    @staticmethod
    def _load_json(path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    @staticmethod
    def _save_json(path: Path, data: dict) -> bool:
        """Atomically write JSON. Returns False on failure so callers can report errors."""
        tmp = path.with_name(f".{path.name}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            tmp.replace(path)
            return True
        except Exception as e:
            logger.error("[小柠定时] 写入失败: %s", type(e).__name__)
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            return False

    # ── helpers ─────────────────────────────────────────────────

    @staticmethod
    def _msg_text(event: AstrMessageEvent) -> str:
        g = getattr(event, "get_message_str", None)
        return str(g() if callable(g) else "").strip()

    @staticmethod
    def _sender_id(event: AstrMessageEvent) -> str:
        g = getattr(event, "get_sender_id", None)
        return str(g() if callable(g) else "").strip()

    # ── command ─────────────────────────────────────────────────

    @filter.command("autopost")
    async def cmd_autopost(self, event: AstrMessageEvent):
        """主命令: /autopost setup|now|status|toggle"""
        parts = self._msg_text(event).strip().split()
        # 去掉可能的命令前缀
        if parts and parts[0].lstrip("/") == "autopost":
            parts = parts[1:]
        sub = parts[0].lower() if parts else ""
        target = parts[1].lower() if len(parts) > 1 else ""
        sender = self._sender_id(event)
        is_owner = sender == self.config.get("owner_id", "")

        if sub == "setup":
            c = self.config
            onoff = lambda k: "✅" if c[k] else "❌"
            grps = lambda k: str(c[k]) if c[k] else "全部群"
            yield event.plain_result(
                f"【小柠定时推送 · 配置】\n\n"
                f"GitHub 趋势: {onoff('github_trending_enabled')} {c['github_trending_time']}\n"
                f"  群: {grps('github_trending_groups')}\n\n"
                f"早安推送: {onoff('morning_post_enabled')} {c['morning_time']}\n"
                f"  群: {grps('morning_groups')}\n\n"
                f"天气推送: {onoff('weather_enabled')} {c['weather_time']}\n"
                f"  城市: {c['weather_city']}  群: {grps('weather_groups')}\n\n"
                f"修改: 编辑插件 config 或联系管理员"
            )

        elif sub == "now":
            if not is_owner:
                yield event.plain_result("仅 bot owner 可手动触发推送。")
            elif target in ("trending", "github"):
                yield event.plain_result("正在获取 GitHub 趋势...")
                text = await asyncio.to_thread(self._fetch_github_trending)
                yield event.plain_result(text)
            elif target == "morning":
                yield event.plain_result("正在生成早安推送（约 30–90 秒）...")
                quote = await asyncio.to_thread(self._fetch_morning_quote)
                yield event.plain_result(quote)
                img = await asyncio.to_thread(self._generate_morning_image)
                if img:
                    yield event.chain_result([Image.fromFileSystem(str(img))])
                    asyncio.create_task(self._cleanup_image(img))
                else:
                    yield event.plain_result("早安图生成失败。")
            elif target == "weather":
                text = await asyncio.to_thread(self._fetch_weather)
                yield event.plain_result(text)
            else:
                yield event.plain_result("用法: /autopost now trending|morning|weather")

        elif sub == "status":
            now = datetime.now()
            lines = ["【下次推送时间】"]
            for label, tkey, ekey in [
                ("GitHub 趋势", "github_trending_time", "github_trending_enabled"),
                ("早安推送", "morning_time", "morning_post_enabled"),
                ("天气播报", "weather_time", "weather_enabled"),
                ("AI 早报", "ai_news_time", "ai_news_enabled"),
                ("群聊总结", "group_summary_time", "group_summary_enabled"),
            ]:
                h, m = map(int, self.config[tkey].split(":"))
                nt = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if nt <= now:
                    nt += timedelta(days=1)
                st = "启用" if self.config[ekey] else "禁用"
                lines.append(f"{label}: {nt.strftime('%Y-%m-%d %H:%M')} ({st})")
            yield event.plain_result("\n".join(lines))

        elif sub == "toggle":
            if not is_owner:
                yield event.plain_result("仅 bot owner 可切换功能。")
            else:
                mapping = {
                    "github": "github_trending_enabled",
                    "trending": "github_trending_enabled",
                    "morning": "morning_post_enabled",
                    "weather": "weather_enabled",
                }
                key = mapping.get(target)
                if not key:
                    yield event.plain_result(
                        f"未知功能: {target}。可选: github/trending, morning, weather"
                    )
                else:
                    old = self.config[key]
                    self.config[key] = not old
                    overrides = self._runtime.setdefault("overrides", {})
                    overrides[key] = self.config[key]
                    self._save_json(self._runtime_file, self._runtime)
                    yield event.plain_result(f"{target} 已{'关闭' if old else '开启'}")

        else:
            yield event.plain_result(
                "【小柠定时推送】\n"
                "/autopost setup — 查看配置\n"
                "/autopost now trending|morning|weather — 立即推送\n"
                "/autopost status — 下次推送时间\n"
                "/autopost toggle github|morning|weather — 开关功能"
            )

        event.stop_event()

    @filter.command("早报")
    async def cmd_ai_news(self, event: AstrMessageEvent):
        """AI 早报订阅: /早报 开启|关闭|状态"""
        parts = self._msg_text(event).strip().split()
        sub = parts[1].strip() if len(parts) > 1 else "状态"
        event.stop_event()
        yield event.plain_result(self._ai_news_subscription_reply(event, sub))

    @staticmethod
    def _compact_news_action(text: str) -> str | None:
        raw = str(text or "").strip()
        if raw.startswith("/早报 "):
            return None
        value = "".join(raw.split()).lstrip("/")
        return {
            "早报开启": "开启", "开启早报": "开启",
            "早报关闭": "关闭", "关闭早报": "关闭",
            "早报状态": "状态", "查看早报": "状态",
        }.get(value)

    # ── push: 周深每日 Word 报告 ──────────────────────────────────

    async def _push_zhoushen_daily(self):
        logger.info("[小柠定时] 周深每日报告")
        import subprocess, tempfile
        today_str = datetime.now().strftime("%Y%m%d")
        output_dir = Path(__file__).resolve().parents[4] / "claude_workspace" / "zhoushen_daily"
        output_dir.mkdir(parents=True, exist_ok=True)
        docx_path = output_dir / f"周深动态日报_{today_str}.docx"

        # 1. Download latest photos
        photos_dir = output_dir / f"photos_{today_str}"
        photos_dir.mkdir(parents=True, exist_ok=True)
        photo_paths = []
        try:
            from crawl4weibo import WeiboClient
            client = WeiboClient()
            results = await asyncio.to_thread(
                client.download_user_posts_images,
                uid="1736988591", pages=5,
                download_dir=str(photos_dir),
                expand_long_text=False,
            )
            for pid, imgs in results.items():
                if imgs:
                    for name, path in imgs.items():
                        if path and Path(path).is_file():
                            photo_paths.append(Path(path))
            logger.info(f"[小柠定时] 下载了 {len(photo_paths)} 张周深照片")
        except Exception as e:
            logger.warning(f"[小柠定时] 照片下载失败: {e}")

        # 2. Generate Word document
        try:
            await asyncio.to_thread(self._build_zhoushen_docx, docx_path, photo_paths, today_str)
            logger.info("[小柠定时] Word 已生成")
        except Exception as e:
            logger.error(f"[小柠定时] Word 生成失败: {e}")
            return False

        # 3. Upload to 生米群
        gids = await self._resolve_groups(self.config["zhoushen_daily_groups"])
        bot = await self._get_bot()
        if not bot or not gids:
            logger.warning("[小柠定时] 无 bot 或群组配置")
            return False

        for gid in gids:
            try:
                # Upload file via NapCat HTTP API
                import requests as _requests
                _requests.post(
                    "http://127.0.0.1:5701/upload_group_file",
                    json={
                        "group_id": int(gid),
                        "file": str(docx_path),
                        "name": docx_path.name,
                    },
                    headers=_NAPCAT_HEADERS,
                    timeout=30,
                )
                await asyncio.sleep(1)
                preview = (
                    f"生米们晚上好～\n"
                    f"今日周深动态日报已生成 📄\n"
                    f"文件：{docx_path.name}\n"
                    f"含 {len(photo_paths)} 张最新照片\n\n"
                    f"深深一直在努力，我们也要加油呀 💙"
                )
                await bot.send_group_msg(
                    group_id=int(gid),
                    message=preview,
                )
            except Exception as e:
                logger.warning(f"[小柠定时] 文件发送→{gid} 失败: {e}")

        return True

    @staticmethod
    def _build_zhoushen_docx(docx_path: Path, photo_paths: list[Path], date_str: str):
        """Generate a professional Word document about Zhou Shen."""
        from docx import Document
        from docx.shared import Inches, Pt, Cm, RGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.enum.section import WD_ORIENT
        from docx.oxml.ns import qn
        import random

        doc = Document()

        # ── Page setup ──
        for section in doc.sections:
            section.top_margin = Cm(2.5)
            section.bottom_margin = Cm(2.5)
            section.left_margin = Cm(2.5)
            section.right_margin = Cm(2.5)

        style = doc.styles['Normal']
        style.font.name = '微软雅黑'
        style.font.size = Pt(11)
        style.paragraph_format.space_after = Pt(6)
        style.paragraph_format.line_spacing = 1.5
        # Set East Asian font
        rPr = style.element.find(qn('w:rPr'))
        if rPr is None:
            rPr = style.element.makeelement(qn('w:rPr'), {})
            style.element.append(rPr)
        rFonts = rPr.find(qn('w:rFonts'))
        if rFonts is None:
            rFonts = rPr.makeelement(qn('w:rFonts'), {})
            rPr.append(rFonts)
        rFonts.set(qn('w:eastAsia'), '微软雅黑')

        # ── Helper: add heading with font fix ──
        def add_heading(text, level=1):
            h = doc.add_heading(text, level=level)
            for run in h.runs:
                run.font.name = '微软雅黑'
                rPr2 = run._r.find(qn('w:rPr'))
                if rPr2 is None:
                    rPr2 = run._r.makeelement(qn('w:rPr'), {})
                    run._r.insert(0, rPr2)
                rFonts2 = rPr2.find(qn('w:rFonts'))
                if rFonts2 is None:
                    rFonts2 = rPr2.makeelement(qn('w:rFonts'), {})
                    rPr2.append(rFonts2)
                rFonts2.set(qn('w:eastAsia'), '微软雅黑')
            return h

        # ── Helper: add paragraph ──
        def add_para(text, bold=False, size=11, alignment=None, color=None):
            p = doc.add_paragraph()
            run = p.add_run(text)
            run.font.name = '微软雅黑'
            run.font.size = Pt(size)
            run.font.bold = bold
            if color:
                run.font.color.rgb = color
            if alignment is not None:
                p.alignment = alignment
            rPr3 = run._r.find(qn('w:rPr'))
            if rPr3 is None:
                rPr3 = run._r.makeelement(qn('w:rPr'), {})
                run._r.insert(0, rPr3)
            rFonts3 = rPr3.find(qn('w:rFonts'))
            if rFonts3 is None:
                rFonts3 = rPr3.makeelement(qn('w:rFonts'), {})
                rPr3.append(rFonts3)
            rFonts3.set(qn('w:eastAsia'), '微软雅黑')
            return p

        # ═══════════════════════════════════════
        # COVER
        # ═══════════════════════════════════════
        doc.add_paragraph()  # spacer
        add_para("周深 · 每日动态", bold=True, size=28,
                 alignment=WD_ALIGN_PARAGRAPH.CENTER,
                 color=RGBColor(0x1A, 0x3C, 0x6E))
        add_para("Charlie Zhou Shen Daily", bold=False, size=14,
                 alignment=WD_ALIGN_PARAGRAPH.CENTER,
                 color=RGBColor(0x88, 0x90, 0xA8))

        doc.add_paragraph()
        # Add cover photo if available
        if photo_paths:
            try:
                doc.add_picture(str(photo_paths[0]), width=Inches(4.5))
                doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
            except Exception:
                pass

        doc.add_paragraph()
        today_cn = datetime.now().strftime("%Y年%m月%d日")
        add_para(today_cn, size=14, alignment=WD_ALIGN_PARAGRAPH.CENTER,
                 color=RGBColor(0x66, 0x66, 0x66))
        add_para("生米交流群 · 每日陪伴", size=11,
                 alignment=WD_ALIGN_PARAGRAPH.CENTER,
                 color=RGBColor(0x99, 0x99, 0x99))
        add_para("整理：小柠 | 图源：微博 @卡布叻_周深", size=9,
                 alignment=WD_ALIGN_PARAGRAPH.CENTER,
                 color=RGBColor(0xBB, 0xBB, 0xBB))

        doc.add_page_break()

        # ═══════════════════════════════════════
        # RECENT NEWS
        # ═══════════════════════════════════════
        add_heading("一、近期动态", level=1)
        add_para(
            "周深最近持续活跃在华语乐坛一线。他的2026巡回演唱会正在全国多地"
            "如火如荼地进行中，每一站都带给歌迷不同的惊喜和感动。从舞台设计到"
            "歌曲编排，周深和团队倾注了大量心血，力求每场演出都是独一无二的艺术体验。",
            size=11)
        add_para(
            "在音乐作品方面，周深近期发布了多首备受好评的新歌。每一首作品都"
            "展现了他不断突破自我的艺术追求，从空灵悠远的抒情曲到张力十足的"
            "高音演绎，天籁之音的称号实至名归。",
            size=11)
        add_para(
            "综艺方面，周深作为常驻嘉宾参与了多档热门节目。他在节目中展现的"
            "幽默感和真诚打动了无数观众，让人看到他舞台之外真实可爱的一面。"
            "无论是对音乐的认真态度还是对粉丝的暖心互动，都让人感受到他的真诚。",
            size=11)

        # ═══════════════════════════════════════
        # PHOTO GALLERY
        # ═══════════════════════════════════════
        add_heading("二、最新照片", level=1)
        add_para("以下为周深微博 (@卡布叻_周深) 近日发布的照片精选，记录了他近期的精彩瞬间。",
                 size=10, color=RGBColor(0x88, 0x88, 0x88))

        if photo_paths:
            # Pick up to 8 photos for the gallery
            gallery = photo_paths[1:9] if len(photo_paths) > 1 else photo_paths[:8]
            for i, p in enumerate(gallery):
                try:
                    doc.add_picture(str(p), width=Inches(5.0))
                    doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
                    caption = f"图 {i+1}：周深近照（来源：@卡布叻_周深 微博）"
                    add_para(caption, size=9, alignment=WD_ALIGN_PARAGRAPH.CENTER,
                             color=RGBColor(0xAA, 0xAA, 0xAA))
                    doc.add_paragraph()  # spacer
                except Exception:
                    pass
        else:
            add_para("（今日照片暂未下载成功，请关注 @卡布叻_周深 微博获取最新动态）",
                     size=10, color=RGBColor(0xAA, 0xAA, 0xAA))

        doc.add_page_break()

        # ═══════════════════════════════════════
        # WORKS
        # ═══════════════════════════════════════
        add_heading("三、代表作品", level=1)
        songs = [
            ("大鱼", "2016", "动画电影《大鱼海棠》印象曲，周深成名之作，空灵悠远，感动无数人"),
            ("光亮", "2021", "纪录片《紫禁城》主题曲，融合京剧唱腔，大气磅礴"),
            ("浮光", "2024", "电影《解密》主题曲，极具感染力的高音演绎"),
            ("人是_", "2025", "科幻电影主题曲，展现周深声音的无限可能"),
            ("触不可及", "2019", "电影《触不可及》中文推广曲，温暖的治愈之音"),
        ]
        for name, year, desc in songs:
            p = doc.add_paragraph()
            r1 = p.add_run(f"{name}  ")
            r1.font.bold = True
            r1.font.size = Pt(12)
            r1.font.name = '微软雅黑'
            r2 = p.add_run(f"({year})  ")
            r2.font.size = Pt(10)
            r2.font.color.rgb = RGBColor(0x88, 0x88, 0x88)
            r2.font.name = '微软雅黑'
            r3 = p.add_run(desc)
            r3.font.size = Pt(10)
            r3.font.name = '微软雅黑'

        # ═══════════════════════════════════════
        # FAN COMMUNITY
        # ═══════════════════════════════════════
        add_heading("四、生米社区", level=1)
        add_para(
            "每一位生米都是这个温暖大家庭的一员。我们因为周深的歌声走到一起，"
            "也因为彼此的陪伴而更加坚定。无论是演唱会上整齐的应援，还是微博超话"
            "里的每日打卡，每一份热爱都让这个社区更加闪耀。",
            size=11)

        # ═══════════════════════════════════════
        # ENCOURAGEMENT
        # ═══════════════════════════════════════
        add_heading("五、小柠对你说", level=1)
        encouragements = [
            "深深说过，他希望用自己的歌声给大家带来温暖。而我们想说的是——"
            "你们每一位生米的存在，也是他前行的力量。",
            "追星不是单向的仰望，而是一群人因为共同的热爱而变得更优秀。"
            "当你为了抢演唱会门票而努力工作攒钱，当你为了应援而学会剪视频、"
            "做海报——你在变好的路上，深深也在。",
            "无论你今天过得怎么样，记得还有一首歌能治愈你，还有一群人懂你。"
            "你是生米，你是独一无二的。",
            "明天又是新的一天。深深会继续唱，我们会继续听，生活会继续美好。",
        ]
        for text in encouragements:
            p = doc.add_paragraph()
            run = p.add_run(text)
            run.font.name = '微软雅黑'
            run.font.size = Pt(12)
            p.paragraph_format.space_after = Pt(12)

        # ═══════════════════════════════════════
        # FOOTER
        # ═══════════════════════════════════════
        doc.add_paragraph()
        add_para("— 小柠 · 生米一直在 —", size=11,
                 alignment=WD_ALIGN_PARAGRAPH.CENTER,
                 color=RGBColor(0x1A, 0x3C, 0x6E))
        add_para(f"生成日期：{today_cn} | 图源：微博 @卡布叻_周深 | 整理：小柠",
                 size=8, alignment=WD_ALIGN_PARAGRAPH.CENTER,
                 color=RGBColor(0xBB, 0xBB, 0xBB))

        doc.save(str(docx_path))

    # ── push: 周深每日晚安曲 ──────────────────────────────────────

    async def _push_zhoushen_song(self):
        import random as _random, subprocess as _sp, tempfile as _tf, shutil as _sh
        logger.info("[小柠定时] 周深晚安曲")

        music_dirs = [
            Path("/d/CloudMusic"),
            Path("/d/CloudMusic/VipSongsDownload"),
        ]
        songs: list[Path] = []
        for d in music_dirs:
            if d.is_dir():
                for f in d.iterdir():
                    if f.suffix.lower() in (".mp3", ".flac", ".ncm") and "周深" in f.name:
                        songs.append(f)

        if not songs:
            logger.warning("[小柠定时] 未找到周深歌曲文件")
            return False

        song = _random.choice(songs)
        logger.info(f"[小柠定时] 选中: {song.name}")

        tmp = _tf.mkdtemp(prefix="zhoushen_song_")
        audio_path = None
        try:
            if song.suffix.lower() == ".ncm":
                # Decrypt NCM to MP3
                decoded = Path(tmp) / f"{song.stem}.mp3"
                result = await asyncio.to_thread(
                    _sp.run,
                    ["node", str(_NCM_DECODER), str(song), str(decoded.with_suffix(""))],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode != 0 or not decoded.is_file():
                    logger.warning(f"[小柠定时] NCM 解密失败: {song.name}")
                    return False
                audio_path = decoded
            else:
                audio_path = Path(_sh.copy2(str(song), Path(tmp) / song.name))

            # Trim intro (18s) + convert to AMR voice
            amr_path = Path(tmp) / "song.amr"
            trim_result = await asyncio.to_thread(
                _sp.run,
                ["ffmpeg", "-y", "-ss", "18", "-i", str(audio_path),
                 "-ac", "1", "-ar", "8000", "-b:a", "12.2k",
                 "-acodec", "libopencore_amrnb", "-t", "120",
                 str(amr_path)],
                capture_output=True, text=True, timeout=60,
            )
            if trim_result.returncode != 0 or not amr_path.is_file():
                logger.warning(f"[小柠定时] ffmpeg 裁剪失败: {song.name}")
                return False

            # Send voice-only to groups
            gids = await self._resolve_groups(self.config["zhoushen_song_groups"])
            bot = await self._get_bot()
            if not bot or not gids:
                return False

            import requests as _req
            song_name = song.stem.replace("周深 - ", "").replace("周深-", "")
            for gid in gids:
                amr_abs = str(amr_path.resolve()).replace("\\", "/")
                _req.post(
                    "http://127.0.0.1:5701/send_group_msg",
                    json={
                        "group_id": int(gid),
                        "message": f"[CQ:record,file=file:///{amr_abs}]",
                    },
                    headers=_NAPCAT_HEADERS,
                    timeout=15,
                )
                await asyncio.sleep(0.8)
                goodnight = _random.choice([
                    f"晚安～今晚是《{song_name}》，好梦 💙",
                    f"生米们晚安，《{song_name}》伴你入眠 🌙",
                    f"今天的晚安曲：《{song_name}》。明天见～",
                ])
                await bot.send_group_msg(
                    group_id=int(gid),
                    message=goodnight,
                )
                await asyncio.sleep(0.5)

            logger.info(f"[小柠定时] 晚安曲已发送: {song_name}")
            return True
        finally:
            try:
                _sh.rmtree(tmp, ignore_errors=True)
            except Exception:
                pass

    # ── push: 周深随机表情包 ──────────────────────────────────────

    async def _push_zhoushen_meme(self):
        import random as _random
        logger.info("[小柠定时] 周深表情包")
        meme_dir = Path(__file__).resolve().parents[4] / "claude_workspace" / "zhoushen_memes"
        memes = list(meme_dir.glob("*.jpg")) + list(meme_dir.glob("*.png")) + list(meme_dir.glob("*.gif"))
        if not memes:
            logger.warning("[小柠定时] 表情包目录为空")
            return False
        meme = _random.choice(memes)
        logger.info("[小柠定时] 已选择表情包")
        gids = await self._resolve_groups(self.config["zhoushen_meme_groups"])
        bot = await self._get_bot()
        if not bot or not gids:
            return False
        for gid in gids:
            cq = f"[CQ:image,file=file:///{meme.as_posix()}]"
            try:
                await bot.send_group_msg(group_id=int(gid), message=cq)
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning(f"[小柠定时] 表情包→{gid} 失败: {e}")
        return True

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=990)
    async def on_compact_ai_news_command(self, event: AstrMessageEvent):
        action = self._compact_news_action(self._msg_text(event))
        if action is None:
            return
        event.stop_event()
        yield event.plain_result(self._ai_news_subscription_reply(event, action))

    def _ai_news_subscription_reply(self, event: AstrMessageEvent, sub: str) -> str:
        sender = self._sender_id(event)
        tier = get_tier(sender, self._pro_db)
        if tier < Tier.X:
            return "AI 早报仅限 X/PRO 用户订阅。添加小柠为QQ好友即可获得X资格。"

        opted_in = self._load_opt_ins()

        if sub in ("开启", "on", "start"):
            opted_in.add(sender)
            if not self._save_opt_ins(opted_in):
                return "订阅写入失败，请稍后重试或联系管理员。"
            return "已开启每日早报，每天早上 7:00 私聊推送。回复「早报关闭」可退订。"
        elif sub in ("关闭", "off", "stop"):
            opted_in.discard(sender)
            if not self._save_opt_ins(opted_in):
                return "退订写入失败，请稍后重试或联系管理员。"
            return "已关闭每日早报。想重新订阅时回复「早报开启」即可。"
        status = "已订阅" if sender in opted_in else "未订阅"
        return f"早报状态: {status}\n\n命令: 早报开启 | 早报关闭 | 早报状态"
