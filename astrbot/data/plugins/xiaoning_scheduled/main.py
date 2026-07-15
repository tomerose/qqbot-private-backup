"""小柠定时推送 — GitHub趋势、早安图文、天气播报"""
from __future__ import annotations

import asyncio
import base64
import json
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
DEEPSEEK_CHAT = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_KEY = "sk-991ee2d8b710420abe434f541a26164b"
PLUGIN_DIR = Path(__file__).resolve().parent

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
    "group_summary_enabled": True,
    "group_summary_time": "22:00",
    "group_summary_group": "1075963106",
    "group_summary_target": "1211000567",
    "ai_news_enabled": True,
    "ai_news_time": "07:00",
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

        # ponytail: 手动触发文件，用于立即推送
        trigger = self._opt_in_file.parent / "trigger_ainews"
        if trigger.exists():
            logger.info("[小柠定时] 手动触发 AI 早报")
            if await self._push_ai_news() is False:
                return
            trigger.unlink(missing_ok=True)
            self._runtime["ainews"] = today
            self._save_json(self._runtime_file, self._runtime)
            return

        # ponytail: 手动触发文件，用于立即推送
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
                    "model": "gemini-3.5-flash",
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
                seg["data"]["text"]
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
                    "model": "gemini-3.5-flash",
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
            return
        bot = await self._get_bot()
        if not bot:
            logger.warning("[小柠定时] AI 早报失败: 无 bot 客户端")
            return False
        news_text = await asyncio.to_thread(self._fetch_ai_news)
        if not news_text:
            return False
        date_str = datetime.now().strftime("%Y-%m-%d")
        sent = 0
        for qq_id in subscribers:
            try:
                await bot.send_private_msg(
                    user_id=int(qq_id),
                    message=f"🤖【小柠 AI 早报 · {date_str}】\n\n{news_text}",
                )
                sent += 1
                await asyncio.sleep(0.8)
            except Exception as e:
                logger.warning(f"[小柠定时] AI 早报→{qq_id} 失败: {e}")
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
        headlines = self._scrape_rss()
        if headlines:
            source_text = "\n".join(headlines)
            user_msg = (
                f"以下是今日科技/AI 领域真实头条：\n\n{source_text}\n\n"
                "请从以上素材中选取 3-5 条最重要的 AI 相关内容，按格式生成中文早报。"
                "只选取 AI 相关的，不要编造素材中没有的事实。"
            )
        else:
            user_msg = f"今日日期: {datetime.now().strftime('%Y-%m-%d')}。请生成今日AI早报。"

        # 优先用 DeepSeek API（长文本中文可靠），不可用时回退 Gemini proxy
        for name, url, model, key in (
            ("DeepSeek", DEEPSEEK_CHAT, "deepseek-v4-flash", DEEPSEEK_KEY),
            ("Gemini", PROXY_CHAT, "gemini-3.5-flash", "sk-gemini-vertex"),
        ):
            try:
                headers = {"Authorization": f"Bearer {key}"} if key else {}
                resp = requests.post(
                    url,
                    json={
                        "model": model,
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "你是AI领域资深编辑。用户订阅了每日AI早报。"
                                    "请生成今日AI领域最重要的3-5条资讯简报，每条80字以内。"
                                    "覆盖：大模型发布、开源动态、AI政策、重要论文、产品更新。"
                                    "用中文，格式清晰，每条前面加emoji标识类别。"
                                    "末尾附一句话洞见。总字数控制在400字以内。"
                                    "不要编造任何你没有把握的新闻，不确定的标[待核实]。"
                                ),
                            },
                            {"role": "user", "content": user_msg},
                        ],
                        "max_tokens": 800,
                    },
                    headers=headers,
                    timeout=30,
                )
                content = self._validate_llm_response(resp)
                if content:
                    return content
                logger.warning(f"[小柠定时] AI 早报 {name} 响应无效，尝试下一个")
            except Exception as e:
                logger.warning(f"[小柠定时] AI 早报 {name} 请求失败: {e}")

        logger.warning("[小柠定时] AI 早报所有 LLM 源均失败，使用 RSS 回退")
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
    def _save_json(path: Path, data: dict):
        try:
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

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
            self._save_opt_ins(opted_in)
            return "✅ AI 早报已开启。每天早上 7:00 私聊推送。"
        elif sub in ("关闭", "off", "stop"):
            opted_in.discard(sender)
            self._save_opt_ins(opted_in)
            return "❌ AI 早报已关闭。"
        status = "已订阅 ✅" if sender in opted_in else "未订阅 ❌"
        return f"AI 早报状态: {status}\n\n命令: /早报 开启 | /早报 关闭"
