"""小柠长期记忆 — Google Firestore + Gemini 语义理解

安全模型: 按 QQ ID 严格隔离，无跨用户访问路径，敏感内容过滤，数据最小化

v2.1: 场景感知提取 — 仅在被明确互动时提取记忆。
v2.2: 智能召回 — 关键词匹配过滤无关记忆。
v3.0: Tier门控 — X/PRO 专属。
v4.0: Gemini 语义相关性排序 + 重要性评分 + 后台合并去重 — Google 生态最强记忆系统。"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

try:
    from google.cloud import firestore
    from google.cloud.firestore import Client as FirestoreClient
except ImportError:
    firestore = None
    FirestoreClient = None

try:
    from draw_command.pro_access import get_tier, Tier
except ImportError:
    from data.plugins.draw_command.pro_access import get_tier, Tier

PROXY_CHAT = "http://127.0.0.1:3000/v1/chat/completions"
FIRESTORE_PROJECT = "solar-modem-496213-f5"
FIRESTORE_DATABASE = "qqbot"
EXTRACT_COOLDOWN = 120
MAX_MEMORIES_PER_USER = 50
MAX_VALUE_LENGTH = 300
MAX_KEY_LENGTH = 80
MIN_MESSAGE_LENGTH = 10
MAX_INJECT_MEMORIES = 5          # 最多注入 5 条最相关记忆
MAX_CONSO_MEMORIES = 20          # 超过此数量触发后台合并
CONSO_COOLDOWN = 600             # 合并冷却 10 分钟
RANK_TIMEOUT = 3.0               # Gemini 排序超时秒数
RANK_CACHE_TTL = 30              # 排序结果缓存秒数
MAX_GROUP_ALIASES = 100          # 同一群最多保存的本人公开称呼数

# 注入记忆时附在后面的安全指令
MEMORY_SAFETY_NOTE = (
    "（以上是你对此用户的了解。规则：1)只在相关话题中自然提及，不要逐条复述 "
    "2)不要暴露记忆来源 3)不要用来评判用户 4)绝不编造不存在的记忆 "
    "5)绝不要把别人的记忆用在这个人身上——每个用户的记忆严格隔离）"
)

# 不应提取记忆的场景 —— 被动卷入/主动插话/原因不明时，用户并非在主动分享
_SKIP_EXTRACT_TRIGGERS = frozenset({"active", "unknown"})

# ── 跨对话任务记忆 ────────────────────────────────────────────
MAX_TASKS_PER_USER = 20
TASK_COOLDOWN = 300  # 5 min between task extraction attempts

TASK_EXTRACT_PROMPT = """你是任务追踪器。分析最近的对话，判断用户是否提到了需要跨对话追踪的任务。

值得追踪的任务：
- 用户明确说"帮我追踪/跟进/盯着"的事
- 用户让Agent执行的任务（生成报告、做网页、分析数据、写文件等）
- 用户提到的待办事项或计划（"下次帮我XX""过几天XX"）
- 进行中的项目或长期事项

返回JSON数组，每个任务包含：
- action: "create"/"update"/"none"
- task_id: 如果是更新已有任务则提供id，新任务留空
- title: 任务简短标题（≤20字）
- description: 任务详情（≤100字）
- status: "pending"/"in_progress"

⚠️ 绝对不要用action="complete"。任务只能由用户手动标记完成或Agent实际交付文件后系统自动完成。你只负责创建和更新。

[{"action": "create", "task_id": "", "title": "...", "description": "...", "status": "pending"}]

如果没有需要追踪的任务，返回 []。只返回JSON数组。"""

# 不应提取记忆的场景 —— 被动卷入/主动插话/原因不明时，用户并非在主动分享

# 用于判断消息是否可能是对 Bot 的互动
_INTERACTION_MARKERS = re.compile(
    r"(?:小柠|柠柠|xiao\s*ning|lemon)",
    re.I,
)
_COMMAND_LIKE = re.compile(r"^\s*[/!！]\w+")
# A request to operate a feature is not a personal fact. Keeping it out of
# durable memory prevents one-off file jobs from resurfacing as user context.
_OPERATIONAL_REQUEST = re.compile(
    r"^\s*(?:小柠[，,：:\s]*)?"
    r"(?:帮我|请你|麻烦你|请|帮忙|生成|创建|制作|导出|写|做|查|搜索|"
    r"翻译|画|下载|整理|总结|分析|部署|运行)",
    re.I,
)

# 群内称呼不是私人长期记忆：仅接受本人在明确与小柠互动时公开的称呼，
# 仅在同一个群、且消息真正提到该称呼时使用。这样既能识别关键成员，也不
# 会把任何用户的私有记忆暴露给群聊中的其他人。
_SELF_DECLARED_ALIAS = re.compile(
    r"^\s*(?:@?\s*小柠[，,:：\s]*)?(?:我叫|我的名字是|我的昵称是|大家叫我|可以叫我|叫我)"
    r"\s*(?P<alias>[\u4e00-\u9fffA-Za-z0-9][\u4e00-\u9fffA-Za-z0-9 _-]{1,15})\s*[。！!，,]?\s*$",
    re.I,
)
_GROUP_ID_RE = re.compile(r"^[1-9]\d{4,12}$")


def _self_declared_alias(text: str) -> str | None:
    """Return a safe self-declared group alias, never infer one from a message."""
    match = _SELF_DECLARED_ALIAS.match(str(text or ""))
    if not match:
        return None
    alias = match.group("alias").strip()
    if alias.casefold() in {"小柠", "柠柠", "xiaoning", "lemon"}:
        return None
    return alias


def _mentioned_group_aliases(text: str, aliases: list[dict]) -> list[str]:
    """Return only same-group aliases literally mentioned in this message."""
    message = str(text or "")
    matched: list[str] = []
    for item in aliases:
        alias = str(item.get("alias", "")).strip()
        if alias and alias in message and alias not in matched:
            matched.append(alias)
        if len(matched) >= 3:
            break
    return matched

# 提取 prompt —— 明确禁止提取敏感凭证
EXTRACT_PROMPT = """你是记忆提取器。分析以下用户消息，判断是否包含值得长期记住的个人信息。
只提取明确陈述的事实，不要推测。如果没有值得记住的信息，返回空列表。

值得记住的信息类型：
- 用户偏好/喜好/厌恶（奶茶口味、喜欢的音乐、讨厌的食物等）
- 个人信息（职业/技能/所在地/学校/年龄/生日/宠物等，但不要提取联系方式）
- 重要事件/经历（换工作、搬家、手术、旅行、分手等——但不要提取考试/成绩）
- 对某人/某事的看法（观点、态度、价值观）
- 与小柠的关系（怎么称呼小柠、对小柠的态度、互动方式）
- 情绪模式（长期压力来源、常见情绪触发点）

⚠️ 不要提取"考试""成绩""备考""分数"相关的内容——考试是临时事件，不值得长期记忆。

importance 评分标准（0.0-1.0）：
- 0.8-1.0: 长期稳定的偏好、身份、关系（如职业、恋爱状态、核心喜好）
- 0.5-0.7: 中期计划、近期重要事件、观点态度
- 0.2-0.4: 临时状态、一次性需求、短期情绪
- 低于 0.2 的内容不会存入长期记忆，你不用返回

严禁提取：
- 密码、令牌、密钥、API key、access token
- 银行卡号、身份证号
- 其他人的隐私信息
- 请求小柠执行的功能、一次性文件任务、命令、操作步骤或任务进度

返回JSON数组，每个元素有key(简短标签，≤15字)、value(完整事实，≤100字)、category(preference/fact/skill/relationship/plan/emotion/other)、importance(0.0-1.0的浮点数)：
[{"key": "...", "value": "...", "category": "...", "importance": 0.8}]
如果没有值得记住的内容，返回 []。只返回JSON数组，不要其他文字。"""

# 本地敏感内容正则 —— 存储前做最后一道防线
_SENSITIVE_RE = re.compile(
    r"(?i)\b(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|credential|authorization|sk-[a-z0-9]{16,}|"
    r"gh[oprsu]_[a-z0-9]{20,}|AIza[a-z0-9_-]{20,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)\b"
)
_QQ_ID_RE = re.compile(r"^[1-9]\d{4,11}$")


class XiaoningMemory(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._db = None
        self._last_extract: dict[str, float] = {}
        data_dir = Path(StarTools.get_data_dir("xiaoning_memory"))
        data_dir.mkdir(parents=True, exist_ok=True)
        self._pro_db = (
            Path(__file__).resolve().parents[2]
            / "plugin_data" / "xiaoning_pro" / "pro_members.db"
        )

    # ── Firestore backend ─────────────────────────────────────────

    @property
    def db(self) -> FirestoreClient | None:
        if self._db is not None:
            return self._db
        if firestore is None:
            logger.warning("[小柠记忆] google-cloud-firestore 未安装")
            return None
        try:
            self._db = firestore.Client(
                project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE
            )
        except Exception as e:
            logger.error(f"[小柠记忆] Firestore ADC 连接失败: {e}")
            return None
        return self._db

    # QQ ID 验证 —— 非数字/长度不对的 ID 拒绝访问
    @staticmethod
    def _valid_qq(qq_id: str) -> bool:
        return bool(_QQ_ID_RE.match(qq_id))

    @staticmethod
    def _valid_group_id(group_id: str) -> bool:
        return bool(_GROUP_ID_RE.match(group_id))

    def _memories_ref(self, qq_id: str):
        if not self._valid_qq(qq_id):
            raise ValueError(f"无效 QQ ID: {qq_id[:6]}...")
        return (
            self.db.collection("users")
            .document(qq_id)
            .collection("memories")
        )

    def _get_memories(self, qq_id: str, limit: int = 30) -> list[dict]:
        if not self._valid_qq(qq_id):
            return []
        docs = self._memories_ref(qq_id).order_by(
            "created_at", direction="DESCENDING"
        ).limit(limit).stream()
        memories = [{**doc.to_dict(), "doc_id": doc.id} for doc in docs]
        memories.reverse()  # oldest first for consistent injection
        return memories

    def _count_memories(self, qq_id: str) -> int:
        if not self._valid_qq(qq_id):
            return MAX_MEMORIES_PER_USER
        return len(list(self._memories_ref(qq_id).limit(MAX_MEMORIES_PER_USER + 1).stream()))

    def _store_memories(self, qq_id: str, facts: list[dict]):
        if not self._valid_qq(qq_id):
            return
        now = datetime.now(timezone.utc)
        batch = self.db.batch()
        stored = 0
        existing = {
            (
                str(item.get("category", "other")).strip().casefold(),
                str(item.get("key", "")).strip().casefold(),
                str(item.get("value", "")).strip().casefold(),
            )
            for item in self._get_memories(qq_id, limit=MAX_MEMORIES_PER_USER)
        }
        for fact in facts:
            sanitized = self._sanitize_fact(fact)
            if sanitized is None:
                continue
            identity = (
                sanitized["category"].casefold(),
                sanitized["key"].casefold(),
                sanitized["value"].casefold(),
            )
            if identity in existing:
                continue
            doc_ref = self._memories_ref(qq_id).document()
            batch.set(doc_ref, {
                "key": sanitized["key"],
                "value": sanitized["value"],
                "category": sanitized["category"],
                "importance": sanitized["importance"],
                "created_at": now,
                "updated_at": now,
            })
            stored += 1
            existing.add(identity)
        if stored > 0:
            batch.commit()
        return stored

    def _clear_memories(self, qq_id: str):
        if not self._valid_qq(qq_id):
            return
        for doc in self._memories_ref(qq_id).stream():
            doc.reference.delete()

    def _group_aliases_ref(self, group_id: str):
        if not self._valid_group_id(group_id) or not self.db:
            raise ValueError("invalid group_id or db unavailable")
        return self.db.collection("groups").document(group_id).collection("public_aliases")

    def _store_group_alias(self, group_id: str, sender_id: str, alias: str) -> None:
        """Store one self-declared public alias, scoped to this group and sender."""
        if not (self._valid_group_id(group_id) and self._valid_qq(sender_id)):
            return
        safe_alias = _self_declared_alias(f"我叫{alias}")
        if safe_alias is None:
            return
        self._group_aliases_ref(group_id).document(sender_id).set(
            {
                "alias": safe_alias,
                "source": "self_declared",
                "updated_at": datetime.now(timezone.utc),
            },
            merge=True,
        )

    def _get_group_aliases(self, group_id: str) -> list[dict]:
        if not self._valid_group_id(group_id):
            return []
        docs = self._group_aliases_ref(group_id).limit(MAX_GROUP_ALIASES).stream()
        aliases: list[dict] = []
        for doc in docs:
            data = doc.to_dict() or {}
            alias = str(data.get("alias", "")).strip()
            if _self_declared_alias(f"我叫{alias}"):
                aliases.append({"alias": alias})
        return aliases

    def _clear_group_alias(self, group_id: str, sender_id: str) -> None:
        if self._valid_group_id(group_id) and self._valid_qq(sender_id):
            self._group_aliases_ref(group_id).document(sender_id).delete()

    # ── sanitization ──────────────────────────────────────────────

    def _sanitize_fact(self, fact: dict) -> dict | None:
        """最后一道防线：过滤敏感内容+低价值记忆。返回 None 表示应丢弃。"""
        key = str(fact.get("key", "")).strip()
        value = str(fact.get("value", "")).strip()
        category = str(fact.get("category", "other")).strip()
        importance = float(fact.get("importance", 0.5))

        if not key or not value:
            return None
        if len(key) < 2 or len(value) < 4:
            return None
        # 过滤低价值记忆（importance < 0.2 不存储）
        if importance < 0.2:
            return None

        combined = f"{key} {value}"
        if _SENSITIVE_RE.search(combined):
            logger.info(f"[小柠记忆] 已过滤含敏感内容的记忆: {key[:30]}...")
            return None

        return {
            "key": key[:MAX_KEY_LENGTH],
            "value": value[:MAX_VALUE_LENGTH],
            "category": category[:30],
            "importance": min(max(importance, 0.0), 1.0),
        }

    # ── lifecycle ─────────────────────────────────────────────────

    async def initialize(self):
        if self.db is None:
            logger.warning("[小柠记忆] Firestore 不可用，记忆功能已降级")
        else:
            logger.info("[小柠记忆] Firestore 后端已就绪 (qqbot) | 安全: 按QQ隔离+敏感过滤")

    # ── message listener ──────────────────────────────────────────

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=900)
    async def on_message(self, event: AstrMessageEvent):
        if not self.db:
            return
        sender = str(getattr(event, "get_sender_id", lambda: "")() or "")
        if not self._valid_qq(sender):
            return
        text = str(getattr(event, "get_message_str", lambda: "")() or "").strip()

        # All users may publish their own group nickname, but only when they
        # explicitly address the bot. It is a same-group alias, never a private
        # memory and never an inferred fact about another person.
        if not event.is_private_chat() and getattr(event, "is_at_or_wake_command", False):
            group_id = str(getattr(event, "get_group_id", lambda: "")() or "")
            alias = _self_declared_alias(text)
            if alias and self._valid_group_id(group_id):
                asyncio.create_task(
                    asyncio.to_thread(self._store_group_alias, group_id, sender, alias)
                )

        # v3.0: 记忆仅限 X/PRO — 普通用户不提取
        try:
            tier = get_tier(sender, self._pro_db)
            if tier < Tier.X:
                return
        except Exception:
            return
        if len(text) < MIN_MESSAGE_LENGTH:
            return
        if _COMMAND_LIKE.match(text) or _OPERATIONAL_REQUEST.match(text):
            return

        # ── 场景感知：被动卷入/主动插话时不提取记忆 ──────────────
        if not event.is_private_chat():
            if not self._is_meaningful_interaction(event, text):
                return

        now = time.time()
        if now - self._last_extract.get(sender, 0) < EXTRACT_COOLDOWN:
            return
        self._last_extract[sender] = now
        asyncio.create_task(self._extract_and_store(sender, text))

    @staticmethod
    def _is_meaningful_interaction(event: AstrMessageEvent, text: str) -> bool:
        """Only extract memories when user is clearly interacting with the bot."""
        # Commands — user is asking bot to do something, not sharing about themselves
        if _COMMAND_LIKE.match(text):
            return False
        # Explicit reference to bot name — likely interacting
        if _INTERACTION_MARKERS.search(text):
            return True
        # Check context_aware trigger if available
        trigger = ""
        try:
            record = event.get_extra("_context_aware_current_message_record", None)
            if record is not None:
                trigger = str(getattr(record, "trigger_type", "") or "")
        except Exception:
            pass
        if trigger in _SKIP_EXTRACT_TRIGGERS:
            return False
        # @bot or wake command — interacting
        if getattr(event, "is_at_or_wake_command", False):
            return True
        # Substantial message (not just "好的"/"嗯") — might be sharing
        if len(text) >= 30:
            return True
        return False

    async def _extract_and_store(self, qq_id: str, text: str):
        facts = await asyncio.to_thread(self._llm_extract, text)
        if not facts:
            return
        try:
            current = self._count_memories(qq_id)
            # ── LRU eviction: delete oldest 5 when cap hit ──────────
            if current >= MAX_MEMORIES_PER_USER:
                docs = list(
                    self._memories_ref(qq_id)
                    .order_by("created_at")
                    .limit(5)
                    .stream()
                )
                if docs:
                    batch = self.db.batch()
                    for doc in docs:
                        batch.delete(doc.reference)
                    batch.commit()
                    logger.info(
                        "[小柠记忆] %s LRU 淘汰 %d 条旧记忆 (%d→%d)",
                        qq_id, len(docs), current, current - len(docs),
                    )
            stored = self._store_memories(qq_id, facts)
            if stored:
                logger.info(f"[小柠记忆] 为 {qq_id} 存储了 {stored} 条记忆")
                # v4.0: 记忆数超过阈值触发后台合并去重
                if self._count_memories(qq_id) >= MAX_CONSO_MEMORIES:
                    asyncio.create_task(self._consolidate_memories(qq_id))
        except Exception as e:
            logger.warning(f"[小柠记忆] 存储失败: {e}")

    def _llm_extract(self, text: str) -> list[dict]:
        try:
            resp = requests.post(
                PROXY_CHAT,
                json={
                    "model": "gemini-2.5-flash",
                    "messages": [
                        {"role": "system", "content": EXTRACT_PROMPT},
                        {"role": "user", "content": text[:2000]},
                    ],
                    "max_tokens": 500,
                },
                timeout=15,
            )
            raw = resp.json()["choices"][0]["message"]["content"].strip()
            raw = raw.lstrip("```json").lstrip("```").rstrip("```").strip()
            result = json.loads(raw)
            return result if isinstance(result, list) else []
        except Exception as e:
            logger.debug(f"[小柠记忆] LLM 提取失败: {e}")
            return []

    # ── LLM context injection ─────────────────────────────────────
    # 关键安全保证: 注入的记忆仅来自当前 sender，绝不跨用户
    # v2.2: 轻量关键词匹配过滤无关记忆，最多注入 8 条最相关的

    @filter.on_llm_request(priority=-10)
    async def inject_memories(self, event: AstrMessageEvent, req) -> None:
        if not self.db:
            return
        sender = str(getattr(event, "get_sender_id", lambda: "")() or "")
        current_text = str(getattr(event, "get_message_str", lambda: "")() or "").strip()
        blocks: list[str] = []
        tier = Tier.ORDINARY

        # Private personal memories stay strictly tied to the current sender
        # and remain an X/Pro capability.
        memories: list[dict] = []
        if self._valid_qq(sender):
            try:
                tier = get_tier(sender, self._pro_db)
                if tier >= Tier.X:
                    memories = self._get_memories(sender)
            except Exception:
                memories = []
        if memories:
            if current_text:
                memories = self._gemini_rank_memories(memories, current_text, sender)
                memories = [m for m in memories if m.get("_score", 0) > 0]
            memories = sorted(
                memories, key=lambda m: -float(m.get("importance", 0.5))
            )[:MAX_INJECT_MEMORIES]
            if memories:
                lines = [
                    f"- [{m.get('category', 'other')}] "
                    f"{m.get('key', '?')}: {m.get('value', '?')}"
                    for m in memories
                ]
                blocks.append("关于当前发送者的私有记忆（仅在相关时使用）：\n" + "\n".join(lines))

        # A public alias is deliberately narrower than memory: it is written
        # only by the named person, stays in this group, and is injected only
        # when this message literally mentions it. This works for ordinary
        # users without granting access to anyone's private memory.
        if not event.is_private_chat() and current_text:
            group_id = str(getattr(event, "get_group_id", lambda: "")() or "")
            if self._valid_group_id(group_id):
                try:
                    aliases = _mentioned_group_aliases(
                        current_text, self._get_group_aliases(group_id)
                    )
                except Exception:
                    aliases = []
                if aliases:
                    blocks.append(
                        "【本群本人公开的称呼】\n"
                        + "\n".join(f"- {alias}" for alias in aliases)
                        + "\n这些称呼只用于确认当前群里正在提到谁；不得补充、猜测或透露任何人的私有信息。"
                    )

        if not blocks:
            return
        marker = "【小柠记忆】"
        memory_block = (
            f"\n\n{marker}\n"
            + "\n\n".join(blocks)
            + f"\n{MEMORY_SAFETY_NOTE}"
        )
        sp = str(getattr(req, "system_prompt", "") or "")
        if marker in sp:
            end_marker = MEMORY_SAFETY_NOTE.strip()
            idx = sp.find(marker)
            end_idx = sp.find(end_marker, idx)
            if end_idx != -1:
                sp = sp[:idx] + sp[end_idx + len(end_marker):]
            else:
                sp = sp[:idx]
            sp = sp.strip()
        req.system_prompt = (sp + memory_block).strip()

        # 进行中的跨对话任务仍只属于当前 X/Pro 用户。
        if tier >= Tier.X and self._valid_qq(sender):
            try:
                tasks = await asyncio.to_thread(self._get_active_tasks, sender)
            except Exception:
                tasks = []
            if tasks:
                task_block = self._build_task_block(tasks)
                req.system_prompt = (req.system_prompt + task_block).strip()

    # ── Gemini 语义相关性排序（替换关键词匹配）───────────────
    # 缓存：同一用户 30 秒内不重复调用 Gemini

    def _gemini_rank_memories(self, memories: list[dict], query: str,
                              sender: str) -> list[dict]:
        """Use Gemini to semantically rank memories by relevance to query.
        Falls back to keyword overlap on any failure."""
        if len(memories) <= MAX_INJECT_MEMORIES:
            for m in memories:
                m["_score"] = 1
            return memories

        # 缓存检查
        now = time.time()
        cache = getattr(self, "_rank_cache", {})
        entry = cache.get(sender)
        if entry and (now - entry["ts"]) < RANK_CACHE_TTL and entry["query"] == query:
            return entry["result"]

        # 构建 Gemini 排序请求
        mem_lines = []
        for i, m in enumerate(memories):
            imp = float(m.get("importance", 0.5))
            mem_lines.append(
                f"[{i}] [{m.get('category', '?')}] ★{imp:.1f} "
                f"{m.get('key', '?')}: {m.get('value', '?')}"
            )
        rank_prompt = (
            f"当前用户消息：「{query[:500]}」\n\n"
            f"已知记忆列表：\n" + "\n".join(mem_lines) + "\n\n"
            f"选出与当前消息最相关的 0-{MAX_INJECT_MEMORIES} 条记忆。"
            f"返回 JSON 数组，只包含相关记忆的索引号（数字）。"
            f"完全不相关的记忆不要包含。如果没有相关记忆，返回 []。\n"
            f'例如：[0, 3, 7] 表示第 0、3、7 条相关。\n只返回 JSON 数组。'
        )

        try:
            resp = requests.post(
                PROXY_CHAT,
                json={
                    "model": "gemini-2.5-flash",
                    "messages": [{"role": "user", "content": rank_prompt}],
                    "max_tokens": 100,
                },
                timeout=RANK_TIMEOUT,
            )
            raw = resp.json()["choices"][0]["message"]["content"].strip()
            raw = raw.lstrip("```json").lstrip("```").rstrip("```").strip()
            indices = json.loads(raw)
            if isinstance(indices, list) and all(isinstance(i, int) for i in indices):
                for m in memories:
                    m["_score"] = 0
                for idx in indices:
                    if 0 <= idx < len(memories):
                        memories[idx]["_score"] = 1
                # 按原始顺序返回有分数的 + 重要性排序
                result = sorted(
                    [m for m in memories if m["_score"] > 0],
                    key=lambda m: -float(m.get("importance", 0.5)),
                )
                self._rank_cache = {sender: {"ts": now, "query": query, "result": result}}
                return result
        except Exception as e:
            logger.debug("[小柠记忆] Gemini 排序失败，降级关键词: %s", type(e).__name__)

        # ── 降级：关键词重叠 ──
        return self._rank_by_keywords(memories, query)

    @staticmethod
    def _rank_by_keywords(memories: list[dict], query: str) -> list[dict]:
        """Keyword-based fallback ranking."""
        query_lower = query.lower()
        query_tokens = set(query_lower.split())
        for m in memories:
            key = str(m.get("key", "")).lower()
            value = str(m.get("value", "")).lower()
            mem_tokens = set(f"{key} {value}".split())
            overlap = len(query_tokens & mem_tokens)
            bonus = 2 if key and key in query_lower else 0
            m["_score"] = overlap + bonus
        scored = [m for m in memories if m["_score"] > 0]
        scored.sort(key=lambda m: (-m["_score"], -float(m.get("importance", 0.5))))
        return scored

    # ── 记忆合并去重（后台异步）──────────────────────────────────

    async def _consolidate_memories(self, qq_id: str):
        """Periodic Gemini-powered consolidation: merge duplicates,
        devalue obsolete facts, extract key themes."""
        if not self.db or not self._valid_qq(qq_id):
            return
        now = time.time()
        last = getattr(self, "_last_conso", {}).get(qq_id, 0)
        if now - last < CONSO_COOLDOWN:
            return
        self._last_conso = getattr(self, "_last_conso", {})
        self._last_conso[qq_id] = now

        memories = self._get_memories(qq_id, limit=MAX_CONSO_MEMORIES)
        if len(memories) < 10:
            return  # too few to consolidate

        mem_text = "\n".join(
            f"[{m.get('doc_id','?')}] [{m.get('category','?')}] "
            f"★{float(m.get('importance',0.5)):.1f} "
            f"{m.get('key','?')}: {m.get('value','?')}"
            for m in memories
        )
        conso_prompt = (
            "你是记忆管家。分析以下用户记忆列表，执行合并去重：\n"
            "1. 找出内容重复或高度相似的记忆，标记要合并的（保留最完整那条，删除重复的）\n"
            "2. 找出已经过时或与当前状态矛盾的记忆，标记要降级（importance 设 0.1）或删除\n"
            "3. 如果所有记忆质量都高、没有需要合并的，返回空操作\n\n"
            f"记忆列表：\n{mem_text}\n\n"
            "返回 JSON：\n"
            '{"merge": [{"keep": "doc_id_1", "delete": ["doc_id_2", "doc_id_3"], '
            '"merged_value": "合并后文本"}], '
            '"demote": ["doc_id_4"], "delete": ["doc_id_5"]}\n'
            "只返回 JSON，不要其他文字。没有需要操作的返回空对象 {}。"
        )

        try:
            resp = await asyncio.to_thread(
                requests.post,
                PROXY_CHAT,
                json={
                    "model": "gemini-2.5-flash",
                    "messages": [{"role": "user", "content": conso_prompt}],
                    "max_tokens": 800,
                },
                timeout=15,
            )
            raw = resp.json()["choices"][0]["message"]["content"].strip()
            raw = raw.lstrip("```json").lstrip("```").rstrip("```").strip()
            ops = json.loads(raw)
        except Exception as e:
            logger.debug("[小柠记忆] 合并失败: %s", type(e).__name__)
            return

        if not isinstance(ops, dict) or not ops:
            return

        batch = self.db.batch()
        ref = self._memories_ref(qq_id)
        merged_count = 0
        deleted_count = 0

        # 处理合并
        for merge_op in ops.get("merge", [])[:5]:
            keep_id = str(merge_op.get("keep", ""))
            delete_ids = [str(d) for d in merge_op.get("delete", [])[:5]]
            merged_value = str(merge_op.get("merged_value", ""))[:MAX_VALUE_LENGTH]
            if keep_id and delete_ids and merged_value:
                try:
                    batch.update(ref.document(keep_id), {
                        "value": merged_value,
                        "updated_at": datetime.now(timezone.utc),
                        "importance": min(
                            float(ops.get("importance", 0.8)), 1.0
                        ),
                    })
                    for did in delete_ids:
                        batch.delete(ref.document(did))
                    merged_count += len(delete_ids)
                except Exception:
                    pass

        # 处理降级
        for did in ops.get("demote", [])[:10]:
            try:
                batch.update(ref.document(str(did)), {
                    "importance": 0.1,
                    "updated_at": datetime.now(timezone.utc),
                })
            except Exception:
                pass

        # 处理直接删除
        for did in ops.get("delete", [])[:10]:
            try:
                batch.delete(ref.document(str(did)))
                deleted_count += 1
            except Exception:
                pass

        if merged_count or deleted_count:
            batch.commit()
            logger.info(
                "[小柠记忆] %s 合并 %d 条 / 删除 %d 条",
                qq_id, merged_count, deleted_count,
            )

    # ── command ───────────────────────────────────────────────────
    @filter.command("清除群称呼")
    async def clear_group_alias(self, event: AstrMessageEvent):
        """Let any user remove the alias they explicitly published in this group."""
        if event.is_private_chat():
            yield event.plain_result("这个操作只在群里使用，用来清除你在当前群公开的称呼。")
            event.stop_event()
            return
        sender = self._sender_id(event)
        group_id = str(getattr(event, "get_group_id", lambda: "")() or "")
        if not (self.db and self._valid_qq(sender) and self._valid_group_id(group_id)):
            yield event.plain_result("暂时无法识别当前群或你的账号，请稍后重试。")
            event.stop_event()
            return
        try:
            self._clear_group_alias(group_id, sender)
            yield event.plain_result("已清除你在这个群公开的称呼。")
        except Exception:
            yield event.plain_result("清除失败，请稍后重试。")
        event.stop_event()

    # 关键安全保证: 仅返回命令发送者自己的记忆

    @filter.command("记忆")
    async def cmd_memory(self, event: AstrMessageEvent):
        sender = self._sender_id(event)
        if not self._valid_qq(sender):
            yield event.plain_result("无法识别用户身份。")
            event.stop_event()
            return

        # v3.0: 普通用户引导升级
        try:
            tier = get_tier(sender, self._pro_db)
        except Exception:
            tier = Tier.ORDINARY
        if tier < Tier.X:
            yield event.plain_result(
                "记忆功能是 X/PRO 专属。\n"
                "添加小柠为 QQ 好友即可自动获得 X资格，解锁长期记忆 —— "
                "小柠会记住你分享的喜好、经历和计划，在聊天中自然地提起。"
            )
            event.stop_event()
            return

        parts = self._msg_text(event).strip().split()
        sub = parts[1].strip() if len(parts) > 1 else ""

        if not self.db:
            yield event.plain_result("记忆功能暂不可用（Firestore 未连接）。")
            event.stop_event()
            return

        if sub in ("清除", "clear", "清空"):
            try:
                self._clear_memories(sender)
                yield event.plain_result("已清除所有记忆。")
            except Exception as e:
                yield event.plain_result(f"清除失败: {e}")
            event.stop_event()
            return

        try:
            memories = self._get_memories(sender)
        except Exception as e:
            yield event.plain_result(f"读取记忆失败: {e}")
            event.stop_event()
            return

        if not memories:
            yield event.plain_result(
                "暂无关于你的记忆。当你在对话中分享信息时，小柠会自动记住。"
            )
        else:
            lines = [f"【小柠记住了关于你的 {len(memories)} 件事】"]
            for m in memories[-20:]:
                lines.append(f"  [{m.get('category', 'other')}] {m.get('key', '?')}")
            lines.append(f"\n共 {len(memories)} 条 | /记忆 清除 — 删除所有记忆")
            yield event.plain_result("\n".join(lines))
        event.stop_event()

    # ── 跨对话任务记忆 ─────────────────────────────────────────

    def _tasks_ref(self, qq_id: str):
        if not self._valid_qq(qq_id) or not self.db:
            raise ValueError("invalid qq_id or db unavailable")
        return self.db.collection("users").document(qq_id).collection("tasks")

    def _get_active_tasks(self, qq_id: str) -> list[dict]:
        if not self._valid_qq(qq_id) or not self.db:
            return []
        try:
            docs = self._tasks_ref(qq_id).where("status", "in", ["pending", "in_progress"]).stream()
            tasks = [{**doc.to_dict(), "doc_id": doc.id} for doc in docs]
            # Also include 3 most recent "done" tasks so the bot remembers
            # what it just completed (context continuity).
            try:
                done_docs = (
                    self._tasks_ref(qq_id)
                    .where("status", "==", "done")
                    .order_by("updated_at", direction="DESCENDING")
                    .limit(3)
                    .stream()
                )
                for doc in done_docs:
                    d = {**doc.to_dict(), "doc_id": doc.id}
                    d["_recently_completed"] = True
                    tasks.append(d)
            except Exception:
                pass  # Firestore may not support compound queries; best-effort
            return tasks
        except Exception:
            return []

    def _build_task_block(self, tasks: list[dict]) -> str:
        if not tasks:
            return ""
        active = [t for t in tasks if not t.get("_recently_completed")]
        recent_done = [t for t in tasks if t.get("_recently_completed")]
        lines = []
        if active:
            lines.append("【进行中的任务】仅当用户当前消息明确询问进度、要求继续该任务，或清楚引用其中一项时，才可使用这些记录回答。绝不在无关对话中主动汇报、催促、建议下一步或祝贺；不得把旧任务当作当前任务。任务完成只能以实际 QQ 文件交付成功为准。")
            for t in active[-8:]:
                status_emoji = {"pending": "⏳", "in_progress": "🔄", "done": "✅"}
                emoji = status_emoji.get(t.get("status", ""), "📌")
                title = str(t.get("title", "?"))[:40]
                desc = str(t.get("description", ""))[:100]
                lines.append(f"{emoji} [{t.get('doc_id','?')}] {title}")
                if desc:
                    lines.append(f"   {desc}")
        if recent_done:
            lines.append("\n【最近完成的任务】仅在用户明确问起同一任务时，可说明已完成；不要主动提起或引导用户继续。")
            for t in recent_done[-3:]:
                title = str(t.get("title", "?"))[:40]
                desc = str(t.get("description", ""))[:100]
                lines.append(f"✅ {title}")
                if desc:
                    lines.append(f"   {desc}")
        return "\n" + "\n".join(lines)

    async def _task_extract_and_store(self, qq_id: str, text: str):
        """LLM-powered task extraction, same pattern as memory extraction."""
        facts = await asyncio.to_thread(self._llm_extract_task, text)
        if not facts:
            return
        try:
            for fact in facts:
                action = str(fact.get("action", "")).strip().lower()
                task_id = str(fact.get("task_id", "")).strip()
                title = str(fact.get("title", "")).strip()[:60]
                description = str(fact.get("description", "")).strip()[:200]
                status = str(fact.get("status", "pending")).strip()

                if not title or action in ("none", "complete"):
                    continue  # complete is disabled — only via /任务 完成 or Agent delivery

                now_utc = datetime.now(timezone.utc)
                if action == "update" and task_id:
                    try:
                        doc_ref = self._tasks_ref(qq_id).document(task_id)
                        doc = doc_ref.get()
                        if doc.exists:
                            doc_ref.update({
                                "title": title or doc.to_dict().get("title", ""),
                                "description": description,
                                "status": status,
                                "updated_at": now_utc,
                            })
                    except Exception:
                        pass
                elif action == "create":
                    # Check limit
                    active = self._get_active_tasks(qq_id)
                    if len(active) >= MAX_TASKS_PER_USER:
                        # Delete oldest completed task
                        try:
                            old = list(
                                self._tasks_ref(qq_id)
                                .where("status", "==", "done")
                                .order_by("updated_at")
                                .limit(3).stream()
                            )
                            if old:
                                batch = self.db.batch()
                                for o in old:
                                    batch.delete(o.reference)
                                batch.commit()
                        except Exception:
                            pass
                    try:
                        doc_ref = self._tasks_ref(qq_id).document()
                        doc_ref.set({
                            "title": title,
                            "description": description,
                            "status": status,
                            "created_at": now_utc,
                            "updated_at": now_utc,
                        })
                        logger.info("[小柠任务] %s 创建任务: %s", qq_id, title)
                    except Exception:
                        pass
        except Exception as e:
            logger.debug("[小柠任务] 存储失败: %s", e)

    def _llm_extract_task(self, text: str) -> list[dict]:
        try:
            resp = requests.post(
                PROXY_CHAT,
                json={
                    "model": "gemini-2.5-flash",
                    "messages": [
                        {"role": "system", "content": TASK_EXTRACT_PROMPT},
                        {"role": "user", "content": text[:2000]},
                    ],
                    "max_tokens": 400,
                },
                timeout=15,
            )
            raw = resp.json()["choices"][0]["message"]["content"].strip()
            raw = raw.lstrip("```json").lstrip("```").rstrip("```").strip()
            result = json.loads(raw)
            return result if isinstance(result, list) else []
        except Exception as e:
            logger.debug("[小柠任务] LLM 提取失败: %s", e)
            return []

    # ── 任务命令 ──────────────────────────────────────────────

    @filter.command("任务")
    async def cmd_task(self, event: AstrMessageEvent):
        sender = self._sender_id(event)
        if not self._valid_qq(sender):
            yield event.plain_result("无法识别用户身份。")
            event.stop_event()
            return

        try:
            tier = get_tier(sender, self._pro_db)
        except Exception:
            tier = Tier.ORDINARY
        if tier < Tier.X:
            yield event.plain_result(
                "跨对话任务记忆是 X/PRO 专属。\n"
                "添加小柠为 QQ 好友即可自动获得 X资格 —— "
                "小柠会追踪你的跨对话任务并在完成时把结果发给你。"
            )
            event.stop_event()
            return

        if not self.db:
            yield event.plain_result("任务功能暂不可用（Firestore 未连接）。")
            event.stop_event()
            return

        parts = self._msg_text(event).strip().split()
        sub = parts[1].strip() if len(parts) > 1 else ""

        if sub in ("完成", "done") and len(parts) >= 3:
            task_id = parts[2].strip()
            try:
                doc_ref = self._tasks_ref(sender).document(task_id)
                doc = doc_ref.get()
                if doc.exists:
                    now_utc = datetime.now(timezone.utc)
                    doc_ref.update({"status": "done", "updated_at": now_utc})
                    yield event.plain_result(f"任务「{doc.to_dict().get('title', task_id)}」已标记完成 ✅")
                else:
                    yield event.plain_result(f"未找到任务 {task_id}")
            except Exception as e:
                yield event.plain_result(f"操作失败: {e}")
            event.stop_event()
            return

        try:
            tasks = self._get_active_tasks(sender)
        except Exception as e:
            yield event.plain_result(f"读取任务失败: {e}")
            event.stop_event()
            return

        # Check pending deliveries for this user
        pending_deliveries = 0
        try:
            from data.plugins.friend_core.delivery_queue import get_queue
            pending_deliveries = get_queue().pending_count(sender)
        except Exception:
            pass

        if not tasks and not pending_deliveries:
            yield event.plain_result(
                "暂无进行中的任务。\n"
                "当你在对话中提到需要追踪的事，或让Agent执行任务时，小柠会自动记录。\n"
                "使用 /任务 完成 <id> 标记任务完成。"
            )
        else:
            lines = []
            if tasks:
                lines.append(f"【进行中的任务 · {len(tasks)} 个】")
                for t in tasks[-15:]:
                    status_map = {"pending": "⏳", "in_progress": "🔄"}
                    emoji = status_map.get(t.get("status", ""), "📌")
                    title = str(t.get("title", "?"))[:50]
                    desc = str(t.get("description", ""))[:100]
                    lines.append(f"{emoji} [{t.get('doc_id','?')}] {title}")
                    if desc:
                        lines.append(f"   {desc}")
                lines.append(f"\n/任务 完成 <id> — 标记完成")
            if pending_deliveries:
                lines.append(f"\n📦 {pending_deliveries} 个文件在后台重试发送中，稍后自动送达。")
            yield event.plain_result("\n".join(lines))
        event.stop_event()

    # ── Agent 集成：记录Agent任务完成 ──────────────────────────

    async def track_agent_task(self, qq_id: str, task_desc: str, status: str = "in_progress"):
        """Agent调用：记录/更新一个跨对话任务。完成后文件交付时会再次调用status='done'。"""
        if not self._valid_qq(qq_id) or not self.db:
            return
        try:
            tier = get_tier(qq_id, self._pro_db)
            if tier < Tier.X:
                return
        except Exception:
            return
        now_utc = datetime.now(timezone.utc)
        title = task_desc[:60].strip()
        try:
            active = self._get_active_tasks(qq_id)
            # Check if similar task exists → update
            existing = [t for t in active if str(t.get("title", "")).strip()[:30] == title[:30]]
            if existing:
                doc_ref = self._tasks_ref(qq_id).document(existing[0]["doc_id"])
                doc_ref.update({
                    "status": status,
                    "description": task_desc[:200],
                    "updated_at": now_utc,
                })
                logger.info("[小柠任务] Agent更新: %s → %s", qq_id, status)
            else:
                doc_ref = self._tasks_ref(qq_id).document()
                doc_ref.set({
                    "title": title,
                    "description": task_desc[:200],
                    "status": status,
                    "created_at": now_utc,
                    "updated_at": now_utc,
                })
                logger.info("[小柠任务] Agent创建: %s → %s", qq_id, status)
        except Exception as e:
            logger.debug("[小柠任务] track_agent_task失败: %s", e)

    # ── helpers ───────────────────────────────────────────────────

    @staticmethod
    def _sender_id(event: AstrMessageEvent) -> str:
        g = getattr(event, "get_sender_id", None)
        return str(g() if callable(g) else "").strip()

    @staticmethod
    def _msg_text(event: AstrMessageEvent) -> str:
        g = getattr(event, "get_message_str", None)
        return str(g() if callable(g) else "").strip()


# ── 模块级便捷函数：Agent完成时调用 ──────────────────────────

def track_agent_task_complete(qq_id: str, task_desc: str, status: str = "done"):
    """claude_code_agent 在文件交付成功后调用，标记任务完成。

    直接写 Firestore，不依赖 XiaoningMemory 实例。"""
    import re as _re
    if not _re.match(r"^[1-9]\d{4,11}$", str(qq_id or "")):
        return
    if firestore is None:
        return
    try:
        db = firestore.Client(project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE)
        now_utc = datetime.now(timezone.utc)
        title = str(task_desc or "")[:60].strip()
        tasks_ref = db.collection("users").document(qq_id).collection("tasks")
        # Check if similar task exists
        existing = list(
            tasks_ref.where("status", "in", ["pending", "in_progress"]).stream()
        )
        match = [t for t in existing if str(t.to_dict().get("title", "")).strip()[:30] == title[:30]]
        if match:
            match[0].reference.update({"status": status, "updated_at": now_utc})
            logger.info("[小柠任务] Agent完成: %s → %s", qq_id, status)
        else:
            doc_ref = tasks_ref.document()
            doc_ref.set({
                "title": title,
                "description": task_desc[:200],
                "status": status,
                "created_at": now_utc,
                "updated_at": now_utc,
            })
            logger.info("[小柠任务] Agent创建+完成: %s → %s", qq_id, status)
    except Exception:
        pass  # 不影响Agent主流程
