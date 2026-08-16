"""小柠长期记忆 — Google Firestore + Gemini 语义理解

安全模型: 按 QQ ID 严格隔离，无跨用户访问路径，敏感内容过滤，数据最小化

v2.1: 场景感知提取 — 仅在被明确互动时提取记忆。
v2.2: 智能召回 — 关键词匹配过滤无关记忆。
v3.0: Tier门控 — X/PRO 专属。
v4.0: Gemini 语义相关性排序 + 重要性评分 + 后台合并去重 — Google 生态最强记忆系统。
v5.0: gemini-embedding-001 向量召回（全量记忆 cosine 取候选）+ knowledge 全局知识储备注入。"""
from __future__ import annotations

import asyncio
import json
import os
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
    from google import genai as _genai
except ImportError:
    _genai = None

# Kept as a compatibility export for existing plugin integrations and tests;
# memory/task access itself is now available to every private user.
try:
    from draw_command.pro_access import get_tier, Tier  # noqa: F401  # compat export for tests/integrations
except ImportError:
    pass
try:
    from xiaoning_runtime import is_private_user_key, private_user_key
except ImportError:
    from data.plugins.xiaoning_runtime import is_private_user_key, private_user_key
try:
    from xiaoning_core.memory import MemoryGateway
except ImportError:
    from data.plugins.xiaoning_core.memory import MemoryGateway

PROXY_CHAT = "http://127.0.0.1:3000/v1/chat/completions"
FIRESTORE_PROJECT = "solar-modem-496213-f5"
FIRESTORE_DATABASE = "qqbot"
EXTRACT_COOLDOWN = 60      # 用户记忆提取冷却秒数（降低以覆盖更多消息）
MAX_MEMORIES_PER_USER = 100
MAX_VALUE_LENGTH = 300
MAX_KEY_LENGTH = 80
MIN_MESSAGE_LENGTH = 4       # 降低门槛："你好""好的""明白了"都值得记住
MAX_INJECT_MEMORIES = 8          # 本地混合召回最多注入 8 条
MAX_CONSO_MEMORIES = 20          # 超过此数量触发后台合并
CONSO_COOLDOWN = 600             # 合并冷却 10 分钟
RANK_TIMEOUT = 3.0               # Gemini 排序超时秒数
RANK_CACHE_TTL = 30              # 排序结果缓存秒数
MAX_GROUP_ALIASES = 100          # 同一群最多保存的本人公开称呼数
FIRESTORE_RETRY_INTERVAL = 60    # Firestore 连接失败后重试间隔秒数
FIRESTORE_CONNECT_TIMEOUT = 8    # Firestore 首次连接超时秒数

# ── v5.0: 向量语义召回（gemini-embedding-001）+ 全局知识储备 ─────────
# 注意：text-embedding-004 在本项目配额极小（6 次即 429），统一用 001。
# 同一集合内向量维度必须一致（001=3072 维），混维度 cosine 会算错。
EMBED_MODEL = "gemini-embedding-001"
EMBED_LOCATION = "global"
RECALL_CANDIDATES = 30           # 向量召回交给 Gemini 重排的候选数
KNOWLEDGE_CACHE_TTL = 300        # 知识库内容缓存秒数
MAX_INJECT_KNOWLEDGE = 3
KNOWLEDGE_MIN_SCORE = 0.55       # cosine 相似度下限，防无关注入

# 注入记忆时附在后面的安全指令
MEMORY_SAFETY_NOTE = (
    "（以上是你对此用户的了解。规则：1)只在相关话题中自然提及，不要逐条复述 "
    "2)不要暴露记忆来源 3)不要用来评判用户 4)绝不编造不存在的记忆 "
    "5)绝不要把别人的记忆用在这个人身上——每个用户的记忆严格隔离）"
)

# 不应提取记忆的场景 —— 被动卷入/主动插话/原因不明时，用户并非在主动分享
_SKIP_EXTRACT_TRIGGERS = frozenset({"active", "unknown"})
_EXPLICIT_REMEMBER_RE = re.compile(
    r"(?:请)?记住(?:一下)?[：:，,\s]*(?P<value>[^\r\n]{1,600})$",
    re.IGNORECASE,
)

# ── embedding 工具 ──────────────────────────────────────────────
_embed_client = None


def _embed_text(text: object) -> list[float] | None:
    """gemini-embedding-001（走 Vertex ADC，与 Firestore 同项目）。
    失败返回 None —— 调用方一律降级到旧的 时间近因/关键词 路径。"""
    global _embed_client
    if _genai is None or os.environ.get("XIAONING_OFFLINE_TESTS") == "1":
        return None
    value = str(text or "").strip()
    if not value:
        return None
    try:
        if _embed_client is None:
            _embed_client = _genai.Client(
                vertexai=True, project=FIRESTORE_PROJECT, location=EMBED_LOCATION
            )
        result = _embed_client.models.embed_content(
            model=EMBED_MODEL, contents=value[:2000]
        )
        values = getattr(result.embeddings[0], "values", None)
        return list(values) if values else None
    except Exception as exc:
        logger.debug("[小柠记忆] embedding 失败: %s", type(exc).__name__)
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    dot = norm_a = norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if not norm_a or not norm_b:
        return 0.0
    return dot / ((norm_a ** 0.5) * (norm_b ** 0.5))


# 知识储备注入门槛：只在话题明确涉及偶像本人/作品/粉丝文化时召回。
# 通用词（泡沫/句号/光亮/倒数等歌名是日常词汇）不进门，防"这句话画个句号"
# 触发邓紫棋资料注入；带歌手名的问句由 周深|邓紫棋 兜底。
_KNOWLEDGE_QUERY_RE = re.compile(
    r"(?:周深|卡布|生米|邓紫棋|g\.?e\.?m\.?|棋士|"
    r"大鱼海棠|达拉崩吧|花开忘忧|少管我|铃芽之旅|亲爱的旅人|"
    r"光年之外|摩天动物园|来自天堂的魔鬼|睡皇后|新的心跳)",
    re.I,
)

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
_TASK_TRACK_REQUEST = re.compile(
    r"(?:帮我|请你|麻烦你)?(?:追踪|跟进|盯着|持续关注|记个任务|下次帮我|过几天帮我)"
    r"|(?:帮我|请你|麻烦你|生成|创建|制作|导出|写|做|整理|分析).{0,30}"
    r"(?:报告|文档|文件|表格|ppt|word|pdf|网页|网站|项目|数据集)"
    r"|(?:任务|文件|报告|结果|进度).{0,6}(?:怎么样|完成|好了|发了|发到|送到|交付|进度|状态)"
    r"|(?:刚才|刚刚|上次|之前|那个).{0,4}(?:任务|文件|报告)",
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
class XiaoningMemory(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._db = None
        self._db_error_at: float = 0
        self._last_extract: dict[str, float] = {}
        self._last_task_extract: dict[str, float] = {}
        # ponytail: in-memory recent-context ring buffer so ALL users get basic
        # conversation continuity even when Firestore is unreachable.
        self._recent_context: dict[str, list[tuple[float, str]]] = {}
        self._recent_context_max = 6  # per user, oldest evicted
        data_dir = Path(StarTools.get_data_dir("xiaoning_memory"))
        data_dir.mkdir(parents=True, exist_ok=True)
        self._local_gateway: MemoryGateway | None = None
        self._local_gateway_error_at = 0.0
        self._pro_db = (
            Path(__file__).resolve().parents[2]
            / "plugin_data" / "xiaoning_pro" / "pro_members.db"
        )

    @property
    def local_gateway(self) -> MemoryGateway | None:
        # Some compatibility tests and external integrations construct a
        # minimal object via __new__; that legacy path has no local store.
        if not hasattr(self, "_local_gateway"):
            return None
        if self._local_gateway is not None:
            return self._local_gateway
        now = time.time()
        if getattr(self, "_local_gateway_error_at", 0.0) and now - self._local_gateway_error_at < 60:
            return None
        try:
            shared_dir = Path(StarTools.get_data_dir("xiaoning_core"))
            shared_dir.mkdir(parents=True, exist_ok=True)
            self._local_gateway = MemoryGateway(shared_dir / "xiaoning-memory.sqlite3")
            self._local_gateway_error_at = 0.0
        except Exception as exc:
            self._local_gateway_error_at = now
            logger.warning("[小柠记忆] 本地加密记忆不可用: %s", type(exc).__name__)
            return None
        return self._local_gateway

    def _ensure_recent_context(self) -> None:
        if not hasattr(self, "_recent_context"):
            self._recent_context = {}
        if not hasattr(self, "_recent_context_max"):
            self._recent_context_max = 6

    @staticmethod
    def _recent_context_scope(event: AstrMessageEvent, sender: str) -> str:
        origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if origin:
            return origin
        group_id = str(getattr(event, "get_group_id", lambda: "")() or "").strip()
        if group_id:
            return f"group:{group_id}:{sender}"
        return f"private:{private_user_key(event) or sender}"

    # ── Firestore backend ─────────────────────────────────────────

    @property
    def db(self) -> FirestoreClient | None:
        if self._db is not None:
            return self._db
        if firestore is None:
            return None
        # ponytail: don't hammer Firestore on every message; back off between retries.
        # Create client only (fast, no I/O); actual operations handle their own timeouts.
        now = time.time()
        last_error = getattr(self, "_db_error_at", 0)
        if last_error and (now - last_error) < FIRESTORE_RETRY_INTERVAL:
            return None
        try:
            self._db = firestore.Client(
                project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE,
            )
            logger.info("[小柠记忆] Firestore 客户端已创建")
            self._db_error_at = 0
        except Exception as e:
            self._db = None
            self._db_error_at = now
            logger.error(f"[小柠记忆] Firestore 客户端创建失败 ({FIRESTORE_RETRY_INTERVAL}s 后重试): {type(e).__name__}")
            return None
        return self._db

    # Existing QQ ids remain untouched; personal-WeChat ids are namespaced.
    @staticmethod
    def _valid_qq(qq_id: str) -> bool:
        return is_private_user_key(qq_id)

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
        memories = [
            {**doc.to_dict(), "doc_id": doc.id}
            for doc in docs
            if not (doc.to_dict() or {}).get("valid_to")
        ]
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
            doc = {
                "key": sanitized["key"],
                "value": sanitized["value"],
                "category": sanitized["category"],
                "importance": sanitized["importance"],
                "created_at": now,
                "updated_at": now,
            }
            vector = _embed_text(f"{sanitized['key']}: {sanitized['value']}")
            if vector:
                doc["embedding"] = vector
            batch.set(doc_ref, doc)
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

    # ── v5.0 向量语义召回 ────────────────────────────────────────

    def _recall_candidates(self, qq_id: str, query: str, limit: int = RECALL_CANDIDATES) -> list[dict]:
        """cosine 相似度从全量记忆里召回 top-N 候选（交给 Gemini 重排）。
        相比旧的"最近30条"，超过30条后老但相关的记忆也能进候选池。
        任何一步失败都降级为最近 N 条。"""
        if not query:
            return self._get_memories(qq_id, limit=limit)
        query_vec = _embed_text(query)
        if query_vec is None:
            return self._get_memories(qq_id, limit=limit)
        try:
            docs = self._memories_ref(qq_id).limit(MAX_MEMORIES_PER_USER).stream()
            memories = [{**doc.to_dict(), "doc_id": doc.id} for doc in docs]
        except Exception:
            return self._get_memories(qq_id, limit=limit)
        embedded = [m for m in memories if isinstance(m.get("embedding"), list)]
        if not embedded:
            return self._get_memories(qq_id, limit=limit)
        for m in embedded:
            m["_vec_score"] = _cosine(query_vec, m["embedding"])
        embedded.sort(key=lambda m: -m["_vec_score"])
        picked = embedded[:limit]
        picked_ids = {m["doc_id"] for m in picked}
        # 还没来得及 embed 的新记忆按时间捎上，防止写入后短期不可见
        fresh = sorted(
            (
                m for m in memories
                if m["doc_id"] not in picked_ids and not isinstance(m.get("embedding"), list)
            ),
            key=lambda m: str(m.get("created_at", "")),
            reverse=True,
        )
        return picked + fresh[:5]

    # ── v5.0 全局知识储备（偶像资料等稳定事实）────────────────────

    def _get_knowledge(self) -> list[dict]:
        now = time.time()
        cache = getattr(self, "_knowledge_cache", None)
        if cache and now - cache["ts"] < KNOWLEDGE_CACHE_TTL:
            return cache["items"]
        docs = self.db.collection("knowledge").stream()
        items = [doc.to_dict() or {} for doc in docs]
        self._knowledge_cache = {"ts": now, "items": items}
        return items

    def _recall_knowledge(self, query: str) -> list[dict]:
        """话题命中偶像关键词时才做向量召回，避免给无关问题塞粉丝资料。"""
        if not _KNOWLEDGE_QUERY_RE.search(query):
            return []
        query_vec = _embed_text(query)
        if query_vec is None:
            return []
        try:
            items = self._get_knowledge()
        except Exception:
            return []
        scored = [
            (item, _cosine(query_vec, item["embedding"]))
            for item in items
            if isinstance(item.get("embedding"), list)
        ]
        scored = [pair for pair in scored if pair[1] >= KNOWLEDGE_MIN_SCORE]
        scored.sort(key=lambda pair: -pair[1])
        return [item for item, _ in scored[:MAX_INJECT_KNOWLEDGE]]

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
        if self.local_gateway is None:
            logger.error("[小柠记忆] 本地加密主存储不可用；长期记忆保持关闭")
        else:
            logger.info("[小柠记忆] 本地加密 SQLite 主存储已就绪；Firestore 为兼容回退")
        if self.db is None:
            logger.warning("[小柠记忆] Firestore 不可用；本地聊天与已授权记忆不受影响")
        else:
            logger.info("[小柠记忆] Firestore 后端已就绪 (qqbot) | 安全: 按QQ隔离+敏感过滤")
            # Replay any local fallback task events that accumulated during Firestore outage.
            try:
                await asyncio.to_thread(self._replay_local_task_events)
            except Exception:
                logger.debug("[小柠记忆] local fallback replay skipped")
            try:
                await asyncio.to_thread(self._sync_local_outbox)
            except Exception:
                logger.debug("[小柠记忆] local memory outbox replay skipped")

    def _sync_local_outbox(self) -> int:
        """Idempotently mirror encrypted local memory events to Firestore."""
        gateway = self.local_gateway
        database = self.db
        if gateway is None or database is None:
            return 0
        completed = 0
        blocked_scopes: set[str] = set()
        for item in gateway.pending_sync(limit=25):
            user_scope = str(item.payload.get("user_scope", ""))
            # Preserve causal order per user. If an earlier upsert cannot be
            # mirrored, a later delete must remain pending instead of being
            # applied first and then accidentally resurrected by the retry.
            if user_scope in blocked_scopes:
                continue
            if not self._valid_qq(user_scope):
                gateway.mark_sync(item.event_id, succeeded=False)
                blocked_scopes.add(user_scope)
                continue
            try:
                if item.operation == "upsert":
                    supersedes_id = str(item.payload.get("supersedes_id", ""))
                    if supersedes_id:
                        self._memories_ref(user_scope).document(supersedes_id).set(
                            {"valid_to": datetime.now(timezone.utc)}, merge=True
                        )
                    value = str(item.payload.get("value", ""))[:MAX_VALUE_LENGTH]
                    self._memories_ref(user_scope).document(item.aggregate_id).set(
                        {
                            "key": value[:MAX_KEY_LENGTH],
                            "value": value,
                            "category": str(item.payload.get("kind", "other"))[:30],
                            "importance": 1.0,
                            "created_at": datetime.now(timezone.utc),
                            "source_type": str(item.payload.get("source_type", ""))[:30],
                            "source_digest": str(item.payload.get("source_digest", ""))[:64],
                            "source_ref": str(item.payload.get("source_ref", ""))[:240],
                            "local_memory_id": item.aggregate_id,
                        },
                        merge=True,
                    )
                elif item.operation == "delete_all":
                    self._clear_memories(user_scope)
                elif item.operation == "delete":
                    self._memories_ref(user_scope).document(item.aggregate_id).delete()
                else:
                    raise ValueError("unsupported memory sync operation")
            except Exception:
                gateway.mark_sync(item.event_id, succeeded=False)
                blocked_scopes.add(user_scope)
                continue
            gateway.mark_sync(item.event_id, succeeded=True)
            completed += 1
        return completed

    # ── message listener ──────────────────────────────────────────

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=900)
    async def on_message(self, event: AstrMessageEvent):
        sender = self._sender_id(event)
        if not self._valid_qq(sender):
            return
        text = str(getattr(event, "get_message_str", lambda: "")() or "").strip()

        # ── in-memory recent context for ALL users (no Firestore dependency) ──
        self._ensure_recent_context()
        if text and not _COMMAND_LIKE.match(text):
            ctx_key = self._recent_context_scope(event, sender)
            ctx_list = self._recent_context.setdefault(ctx_key, [])
            now_ts = time.time()
            ctx_list.append((now_ts, text[:200]))
            if len(ctx_list) > self._recent_context_max:
                ctx_list[:] = ctx_list[-self._recent_context_max:]

        # Long-term personal memory is opt-in and only accepts text that can be
        # quoted verbatim from an explicit private "记住..." request. Model
        # inference is never written into the local store.
        gateway = self.local_gateway
        consent = gateway.get_consent(sender) if gateway is not None else None
        explicit = _EXPLICIT_REMEMBER_RE.search(text)
        if (
            gateway is not None
            and consent is not None
            and consent.memory
            and event.is_private_chat()
            and explicit is not None
            and not _COMMAND_LIKE.match(text)
        ):
            value = explicit.group("value").strip()
            kind = "commitment" if re.search(r"(?:提醒|约定|答应|日程|生日)", value) else "preference"
            try:
                await asyncio.to_thread(
                    gateway.add_memory,
                    sender,
                    kind=kind,
                    value=value,
                    source_type="user_quote",
                    source_quote=text,
                    source_ref=str(getattr(event, "unified_msg_origin", "") or "")[:240],
                )
                event.set_extra("xiaoning_memory_written", True)
                if self.db is not None:
                    asyncio.create_task(asyncio.to_thread(self._sync_local_outbox))
            except Exception as exc:
                logger.warning("[小柠记忆] 本地显式记忆写入失败: %s", type(exc).__name__)

        if not self.db:
            return

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

        # Operational jobs are not personal facts, but explicit follow-ups and
        # artifact work belong in the separate cross-conversation task ledger.
        now = time.time()
        if (
            len(text) >= 6
            and _TASK_TRACK_REQUEST.search(text)
            and now - self._last_task_extract.get(sender, 0) >= TASK_COOLDOWN
        ):
            self._last_task_extract[sender] = now
            asyncio.create_task(self._task_extract_and_store(sender, text))

        # Legacy LLM extraction is deliberately no longer scheduled. Firestore
        # remains a read fallback and future outbox sync target, not an implicit
        # inference sink.

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
                logger.info("[小柠记忆] 存储了 %d 条记忆", stored)
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
                    "model": "gemini-3.7-flash",
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
        sender = self._sender_id(event)
        current_text = str(getattr(event, "get_message_str", lambda: "")() or "").strip()
        blocks: list[str] = []
        db_available = self.db is not None  # may trigger reconnection
        legacy_compat_instance = not hasattr(self, "_local_gateway")
        gateway = self.local_gateway
        consent = gateway.get_consent(sender) if gateway is not None else None
        memory_enabled = legacy_compat_instance or bool(consent and consent.memory)

        # ── in-memory recent context: ALL users, no Firestore dependency ──
        # Only inject messages from the last 2 hours — stale context causes confusion.
        self._ensure_recent_context()
        if current_text:
            ctx_key = self._recent_context_scope(event, sender)
            ctx_list = self._recent_context.get(ctx_key, [])
            now_ts = time.time()
            RECENT_WINDOW = 2 * 3600  # 2 hours
            recent = [
                c[1] for c in ctx_list[-6:]
                if c[1] != current_text[:200]
                and (now_ts - c[0]) < RECENT_WINDOW
            ]
            if recent:
                blocks.append(
                    "【最近对话上下文】该用户最近 2 小时内发送的消息（仅供参考话题连续性，"
                    "不要逐条复述或追问已解决的事）：\n"
                    + "\n".join(f"- {r[:120]}" for r in recent)
                )

        # Private personal memories stay strictly tied to the current sender
        # and remain an X/Pro capability.
        memories: list[dict] = []
        local_memories = []
        if memory_enabled and gateway is not None and self._valid_qq(sender):
            try:
                local_memories = await asyncio.to_thread(
                    gateway.recall, sender, current_text, limit=MAX_INJECT_MEMORIES
                )
            except Exception:
                local_memories = []
        set_extra = getattr(event, "set_extra", None)
        if callable(set_extra):
            set_extra("_xiaoning_local_memory_authoritative", bool(local_memories))
        if local_memories:
            blocks.append(
                "关于当前发送者的已授权本地记忆（仅在相关时使用，均有用户原话或验证来源）：\n"
                + "\n".join(f"- [{item.kind}] {item.value}" for item in local_memories)
            )
        elif memory_enabled and db_available and self._valid_qq(sender):
            try:
                memories = await asyncio.to_thread(
                    self._recall_candidates, sender, current_text
                )
            except Exception:
                memories = []
        if memories and not local_memories:
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
        if db_available and not event.is_private_chat() and current_text:
            group_id = str(getattr(event, "get_group_id", lambda: "")() or "")
            if self._valid_group_id(group_id):
                try:
                    aliases = _mentioned_group_aliases(
                        current_text,
                        await asyncio.to_thread(self._get_group_aliases, group_id),
                    )
                except Exception:
                    aliases = []
                if aliases:
                    blocks.append(
                        "【本群本人公开的称呼】\n"
                        + "\n".join(f"- {alias}" for alias in aliases)
                        + "\n这些称呼只用于确认当前群里正在提到谁；不得补充、猜测或透露任何人的私有信息。"
                    )

        # 全局知识储备：仅当前话题命中偶像关键词时向量召回，放最前面
        # （个人记忆更贴近用户消息，提示效果更好）
        if db_available and current_text:
            try:
                knowledge = await asyncio.to_thread(self._recall_knowledge, current_text)
            except Exception:
                knowledge = []
            if knowledge:
                blocks.insert(
                    0,
                    "【小柠知识储备】与当前话题相关的稳定资料"
                    "（只作背景，新动态以实时搜索为准，不要逐条复述）：\n"
                    + "\n".join(
                        f"- [{item.get('topic', '?')}] {item.get('content', '?')}"
                        for item in knowledge
                    ),
                )

        marker = "【小柠记忆】"
        sp = str(getattr(req, "system_prompt", "") or "")
        if blocks and marker in sp:
            end_marker = MEMORY_SAFETY_NOTE.strip()
            idx = sp.find(marker)
            end_idx = sp.find(end_marker, idx)
            if end_idx != -1:
                sp = sp[:idx] + sp[end_idx + len(end_marker):]
            else:
                sp = sp[:idx]
            sp = sp.strip()
        if blocks:
            memory_block = (
                f"\n\n{marker}\n"
                + "\n\n".join(blocks)
                + f"\n{MEMORY_SAFETY_NOTE}"
            )
            req.system_prompt = (sp + memory_block).strip()

        # Task context — only inject when user asks about tasks/progress/files
        if db_available and self._valid_qq(sender):
            task_relevant = bool(_TASK_TRACK_REQUEST.search(current_text) if current_text else False)
            if task_relevant:
                try:
                    tasks = await asyncio.to_thread(self._get_active_tasks, sender)
                except Exception:
                    tasks = []
                if tasks:
                    task_block = self._build_task_block(tasks)
                    sp = str(getattr(req, "system_prompt", "") or "")
                    req.system_prompt = (sp + task_block).strip()

    async def build_proactive_memory_block(
        self, sender: str, context_hint: str
    ) -> str:
        """Return only relevant X/Pro private memory for one autonomous message."""
        sender = str(sender or "").strip()
        hint = str(context_hint or "").strip()
        if not hint or not self._valid_qq(sender) or not self.db:
            return ""
        try:
            memories = await asyncio.to_thread(self._recall_candidates, sender, hint)
            ranked = await asyncio.to_thread(
                self._gemini_rank_memories, memories, hint, sender
            )
        except Exception:
            return ""
        ranked = [item for item in ranked if item.get("_score", 0) > 0][
            :MAX_INJECT_MEMORIES
        ]
        if not ranked:
            return ""
        lines = [
            f"- [{item.get('category', 'other')}] "
            f"{item.get('key', '?')}: {item.get('value', '?')}"
            for item in ranked
        ]
        return (
            "【小柠记忆】\n关于当前私聊对象的相关记忆（仅在当前话题自然相关时使用）：\n"
            + "\n".join(lines)
            + f"\n{MEMORY_SAFETY_NOTE}"
        )

    # ── Gemini 语义相关性排序（替换关键词匹配）───────────────
    # 缓存：同一用户 30 秒内不重复调用 Gemini

    def _gemini_rank_memories(self, memories: list[dict], query: str,
                              sender: str) -> list[dict]:
        """Use Gemini to semantically rank memories by relevance to query.
        Falls back to keyword overlap on any failure."""
        if len(memories) <= MAX_INJECT_MEMORIES:
            # Still do keyword-based relevance check — don't inject ALL memories
            # just because there are few of them.
            self._rank_by_keywords(memories, query)
            return [m for m in memories if m.get("_score", 0) > 0]

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
                    "model": "gemini-3.7-flash",
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
        """Keyword-based ranking with minimum relevance threshold.
        Requires either 2+ token overlap or an exact key-substring match."""
        query_lower = query.lower()
        # Filter out common Chinese stop-words that cause false matches
        stop_words = {'的','了','我','你','是','在','不','有','和','就','都','也','他','她','它','们','这','那','什么','怎么','为什么','一个','一下','一点','吗','呢','吧','啊','哦','嗯','好','很','还','要','会','能','可以','这个','那个'}
        query_tokens = set(query_lower.split()) - stop_words
        if not query_tokens:
            # Query was all stop-words — don't inject any memories
            for m in memories:
                m["_score"] = 0
            return []
        for m in memories:
            key = str(m.get("key", "")).lower()
            value = str(m.get("value", "")).lower()
            mem_tokens = set(f"{key} {value}".split()) - stop_words
            overlap = len(query_tokens & mem_tokens)
            # Require at least 2 content-word overlap OR exact key match
            key_match = key and (key in query_lower or any(t in key for t in query_tokens if len(t) >= 2))
            if overlap >= 2 or key_match:
                m["_score"] = max(1, overlap + (2 if key_match else 0))
            else:
                m["_score"] = 0
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
                    "model": "gemini-3.7-flash",
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
                    update = {
                        "value": merged_value,
                        "updated_at": datetime.now(timezone.utc),
                        "importance": min(
                            float(ops.get("importance", 0.8)), 1.0
                        ),
                    }
                    keep_mem = next(
                        (m for m in memories if m.get("doc_id") == keep_id), None
                    )
                    if keep_mem:
                        vector = _embed_text(
                            f"{keep_mem.get('key', '')}: {merged_value}"
                        )
                        if vector:
                            update["embedding"] = vector
                    batch.update(ref.document(keep_id), update)
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

        parts = self._msg_text(event).strip().split()
        sub = parts[1].strip() if len(parts) > 1 else ""
        gateway = self.local_gateway
        if gateway is None:
            yield event.plain_result("本地加密记忆暂不可用；小柠不会把这次对话写入长期记忆。")
            event.stop_event()
            return

        if not event.is_private_chat():
            yield event.plain_result("长期记忆设置只可由本人在私聊中查看和修改。")
            event.stop_event()
            return

        if sub in {"开启", "开", "enable", "on"}:
            gateway.set_consent(sender, memory=True)
            yield event.plain_result(
                "已开启长期记忆。只有你明确说“记住……”的原话，或已验证的任务结果，才会进入本地加密记忆；"
                "不会保存聊天全文或模型猜测。可用 /记忆 查看、/记忆 关闭、/记忆 删除全部。"
            )
            event.stop_event()
            return

        if sub in {"关闭", "关", "pause", "off", "disable"}:
            gateway.set_consent(sender, memory=False)
            yield event.plain_result(
                "已关闭长期记忆写入和召回；已有本地记忆暂时保留但不会使用。"
                "如需永久删除，请使用 /记忆 删除全部 并完成二次确认。"
            )
            event.stop_event()
            return

        if sub in {"清除", "clear", "清空"}:
            yield event.plain_result(
                "为防止误删，请改用 /记忆 删除全部；小柠会返回一个 5 分钟有效的确认码。"
            )
            event.stop_event()
            return

        if sub == "更正":
            if len(parts) < 4:
                yield event.plain_result("用法：/记忆 更正 <记忆编号> <新的准确原话>")
                event.stop_event()
                return
            if not gateway.get_consent(sender).memory:
                yield event.plain_result("请先用 /记忆 开启，再更正长期记忆。")
                event.stop_event()
                return
            try:
                old_id = gateway.resolve_memory_id(sender, parts[2])
                old_record = next(
                    item for item in gateway.list_memories(sender) if item.memory_id == old_id
                )
                value = " ".join(parts[3:]).strip()
                await asyncio.to_thread(
                    gateway.add_memory,
                    sender,
                    kind=old_record.kind,
                    value=value,
                    source_type="user_quote",
                    source_quote=self._msg_text(event),
                    source_ref=str(getattr(event, "unified_msg_origin", "") or "")[:240],
                    supersedes_id=old_id,
                )
                if self.db is not None:
                    asyncio.create_task(asyncio.to_thread(self._sync_local_outbox))
                yield event.plain_result("已用你的新原话更正这条记忆，旧事实保留为失效版本。")
            except Exception as exc:
                yield event.plain_result(f"更正失败：{exc}")
            event.stop_event()
            return

        if sub == "删除" and len(parts) >= 3:
            try:
                gateway.delete_memory(sender, parts[2])
                if self.db is not None:
                    asyncio.create_task(asyncio.to_thread(self._sync_local_outbox))
                yield event.plain_result("这条记忆已删除，并已建立远端兼容删除请求。")
            except Exception as exc:
                yield event.plain_result(f"删除失败：{exc}")
            event.stop_event()
            return

        if sub == "删除全部":
            if len(parts) < 3:
                token = gateway.request_delete_all(sender)
                yield event.plain_result(
                    "这会永久删除你的本地长期记忆，并建立远端兼容数据删除请求。"
                    f"确认请在 5 分钟内发送：/记忆 删除全部 {token}"
                )
            elif gateway.confirm_delete_all(sender, parts[2]):
                if self.db is not None:
                    asyncio.create_task(asyncio.to_thread(self._sync_local_outbox))
                yield event.plain_result(
                    "本地长期记忆已删除；远端兼容数据删除请求已进入幂等同步队列。"
                )
            else:
                yield event.plain_result("确认码无效或已过期，未删除任何记忆。")
            event.stop_event()
            return

        consent = gateway.get_consent(sender)
        if not consent.memory:
            yield event.plain_result(
                "长期记忆当前为：已关闭。\n"
                "开启后也只记录你明确要求记住的原话或已验证结果。\n"
                "可用：/记忆 开启 | /记忆 查看 | /记忆 删除全部"
            )
            event.stop_event()
            return

        local_memories = gateway.list_memories(sender, limit=100)
        lines = [f"【本地加密记忆：{len(local_memories)} 条】"]
        for item in local_memories[:20]:
            lines.append(f"  {item.memory_id[:8]} [{item.kind}] {item.value[:80]}")
        if len(local_memories) > 20:
            lines.append(f"  ……另有 {len(local_memories) - 20} 条")
        # During migration, read legacy Firestore only after explicit consent
        # and only for the requesting user. It remains a fallback, never the
        # authority for new writes.
        if not local_memories and self.db:
            try:
                legacy = self._get_memories(sender)
            except Exception:
                legacy = []
            if legacy:
                lines.append(f"【兼容回退记忆：{len(legacy)} 条，尚未迁入本地】")
                for item in legacy[-10:]:
                    lines.append(
                        f"  [{item.get('category', 'other')}] {item.get('key', '?')}"
                    )
        lines.append(
            "\n控制：/记忆 更正 <编号> <新原话> | /记忆 删除 <编号> | "
            "/记忆 关闭 | /记忆 删除全部"
        )
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
            docs = self._tasks_ref(qq_id).where(
                "status", "in", ["pending", "in_progress", "delivery_pending"]
            ).stream()
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
            lines.append("【进行中的任务】仅当用户当前消息明确询问进度、要求继续该任务，或清楚引用其中一项时，才可使用这些记录回答。绝不在无关对话中主动汇报、催促、建议下一步或祝贺；不得把旧任务当作当前任务。任务完成只能以实际成品交付成功为准。")
            for t in active[-8:]:
                status_emoji = {
                    "pending": "⏳", "in_progress": "🔄",
                    "delivery_pending": "📦", "done": "✅",
                }
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
                if status not in {"pending", "in_progress"}:
                    status = "pending"

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
                    duplicate = next(
                        (
                            item for item in active
                            if not item.get("_recently_completed")
                            and str(item.get("title", "")).strip().casefold()[:30]
                            == title.casefold()[:30]
                        ),
                        None,
                    )
                    if duplicate:
                        self._tasks_ref(qq_id).document(duplicate["doc_id"]).update({
                            "description": description,
                            "status": status,
                            "updated_at": now_utc,
                        })
                        continue
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
                        logger.info("[小柠任务] 创建任务")
                    except Exception:
                        pass
        except Exception as e:
            logger.debug("[小柠任务] 存储失败: %s", e)

    def _llm_extract_task(self, text: str) -> list[dict]:
        try:
            resp = requests.post(
                PROXY_CHAT,
                json={
                    "model": "gemini-3.7-flash",
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
                    status_map = {
                        "pending": "⏳", "in_progress": "🔄",
                        "delivery_pending": "📦",
                    }
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
            now_utc = datetime.now(timezone.utc)
            title = str(task_desc or "执行任务").strip()[:60]
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
                logger.info("[小柠任务] Agent更新 → %s", status)
            else:
                doc_ref = self._tasks_ref(qq_id).document()
                doc_ref.set({
                    "title": title,
                    "description": task_desc[:200],
                    "status": status,
                    "created_at": now_utc,
                    "updated_at": now_utc,
                })
                logger.info("[小柠任务] Agent创建 → %s", status)
        except Exception as e:
            logger.debug("[小柠任务] track_agent_task失败: %s", e)

    # ── local fallback replay ─────────────────────────────────────

    def _replay_local_task_events(self) -> None:
        """Replay task events that were stored locally while Firestore was down."""
        if not self.db:
            return
        try:
            from data.plugins.claude_code_agent.job_store import JobStore
        except ImportError:
            try:
                from claude_code_agent.job_store import JobStore
            except ImportError:
                return
        try:
            workspace = Path(__file__).resolve().parents[3] / "claude_workspace"
            store = JobStore(workspace / "state" / "jobs.db")
        except Exception:
            return
        events = store.pending_task_events(limit=50)
        if not events:
            return
        replayed: list[int] = []
        for ev in events:
            sender = str(ev.get("sender_id", "")).strip()
            if not sender:
                continue
            try:
                track_runtime_task_status(
                    sender,
                    str(ev.get("job_id", "")),
                    "",
                    str(ev.get("status", "in_progress")),
                    str(ev.get("evidence", "")),
                    owner="agent",
                )
                replayed.append(int(ev["id"]))
            except Exception:
                pass
        if replayed:
            store.mark_task_events_replayed(replayed)
            logger.info("[小柠记忆] 从本地回放 %d 条任务事件", len(replayed))

    # ── helpers ───────────────────────────────────────────────────

    @staticmethod
    def _sender_id(event: AstrMessageEvent) -> str:
        key = private_user_key(event)
        if key:
            return key
        g = getattr(event, "get_sender_id", None)
        return str(g() if callable(g) else "").strip()

    @staticmethod
    def _msg_text(event: AstrMessageEvent) -> str:
        g = getattr(event, "get_message_str", None)
        return str(g() if callable(g) else "").strip()


# ── 模块级便捷函数：Agent完成时调用 ──────────────────────────

# ponytail: cached Firestore client so track_runtime_task_status doesn't create
# a new connection on every call. Backoff guard prevents hammering Firestore when
# it's persistently unreachable.
_track_db: FirestoreClient | None = None
_track_db_error_at: float = 0
_track_db_retry_interval = 60


def _get_track_db() -> FirestoreClient | None:
    global _track_db, _track_db_error_at
    if _track_db is not None:
        return _track_db
    if firestore is None:
        return None
    now = time.time()
    if _track_db_error_at and (now - _track_db_error_at) < _track_db_retry_interval:
        return None
    try:
        _track_db = firestore.Client(project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE)
        _track_db_error_at = 0
    except Exception:
        _track_db = None
        _track_db_error_at = now
        return None
    return _track_db


def track_runtime_task_status(
    qq_id: str,
    task_id: str,
    task_desc: str,
    status: str = "in_progress",
    evidence: str = "",
    owner: str = "runtime",
):
    """Mirror one real runtime task into the per-user cross-dialog ledger.

    The owning plugin remains authoritative.  Callers must supply evidence for
    ``done``; a stable owner/task document id prevents duplicate tasks.
    """
    import re as _re
    if not is_private_user_key(qq_id):
        return
    safe_task_id = _re.sub(r"[^a-zA-Z0-9_-]", "", str(task_id or ""))[:64]
    safe_owner = _re.sub(r"[^a-zA-Z0-9_-]", "", str(owner or "runtime").lower())[:24]
    if not safe_task_id or not safe_owner:
        return
    status = str(status or "").strip().lower()
    if status not in {"pending", "in_progress", "delivery_pending", "done", "failed"}:
        return
    if status == "done" and not str(evidence or "").strip():
        return
    db = _get_track_db()
    if db is None:
        return
    try:
        pro_db = (
            Path(__file__).resolve().parents[2]
            / "plugin_data" / "xiaoning_pro" / "pro_members.db"
        )
        now_utc = datetime.now(timezone.utc)
        title = str(task_desc or "")[:60].strip()
        tasks_ref = db.collection("users").document(qq_id).collection("tasks")
        doc_ref = tasks_ref.document(f"{safe_owner}-{safe_task_id}")
        existing = doc_ref.get()
        data = {
            "title": title or "执行任务",
            "description": str(task_desc or "")[:200],
            "status": status,
            "task_id": safe_task_id,
            "task_owner": safe_owner,
            "evidence": str(evidence or "")[:200],
            "updated_at": now_utc,
        }
        if safe_owner == "agent":
            data["agent_job_id"] = safe_task_id
        if not existing.exists:
            data["created_at"] = now_utc
        doc_ref.set(data, merge=True)
        logger.info("[小柠任务] 状态 → %s", status)
    except Exception:
        pass  # 不影响Agent主流程


def track_agent_job_status(
    qq_id: str,
    job_id: str,
    task_desc: str,
    status: str = "in_progress",
    evidence: str = "",
):
    """Compatibility wrapper for the authoritative Agent job store."""
    track_runtime_task_status(
        qq_id, job_id, task_desc, status, evidence, owner="agent"
    )


def track_agent_task_complete(qq_id: str, task_desc: str, status: str = "done"):
    """Backward-compatible wrapper for older callers without a job id."""
    import hashlib as _hashlib
    job_id = _hashlib.sha256(str(task_desc or "").encode("utf-8")).hexdigest()[:12]
    evidence = "legacy_agent_complete" if str(status or "").lower() == "done" else ""
    track_agent_job_status(qq_id, job_id, task_desc, status, evidence)
