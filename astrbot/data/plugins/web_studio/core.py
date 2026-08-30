"""Security boundary and persistent project store for Xiaoning Web Studio."""

from __future__ import annotations

import re
import secrets
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo


MAX_HTML_CHARS = 750_000
SHANGHAI = ZoneInfo("Asia/Shanghai")
_PAGE_ID = re.compile(r"^[a-f0-9]{10}$")
_BLOCKED_TAGS = {
    "applet", "base", "embed", "frame", "frameset", "iframe",
    "link", "object", "portal",
}
_URL_ATTRS = {"action", "formaction", "href", "poster", "src", "srcset", "xlink:href"}
_NETWORK_CODE = re.compile(
    r"(?:\bfetch\s*\(|\bXMLHttpRequest\b|\bWebSocket\b|\bEventSource\b|"
    r"\bsendBeacon\s*\(|navigator\.serviceWorker|\bimportScripts\s*\(|"
    r"navigator\.clipboard\.(?:read|readText)\s*\(|"
    r"\bwindow\.open\s*\(|(?:window\.|document\.)?location\s*=|"
    r"(?:window\.|document\.)?location\.(?:href|assign|replace)|"
    r"\bdocument\.cookie\b|\beval\s*\(|\bnew\s+Function\s*\()",
    re.I,
)
_EXTERNAL_SCHEME = re.compile(r"(?:https?|wss?|ftp):\s*//", re.I)
_INLINE_SVG_NAMESPACE = re.compile(
    r"\bxmlns(?::[\w.-]+)?\s*=\s*(['\"])http://www\.w3\.org/(?:2000/svg|1999/xlink)\1",
    re.I,
)
_CSS_URL = re.compile(r"(?<![\w$])url\s*\(", re.I)
_SAFE_CSS_DATA_IMAGE = re.compile(
    r"url\s*\(\s*(['\"]?)data:image/(?:png|jpe?g|gif|webp);base64,[a-z0-9+/=]+\1\s*\)",
    re.I,
)
_COVERAGE_RULES = (
    ("任务清单", ("任务", "待办", "清单"), ("任务", "待办", "清单")),
    ("番茄专注", ("番茄", "专注计时", "专注"), ("番茄", "专注")),
    ("饮水记录", ("饮水", "喝水"), ("饮水", "喝水", "杯")),
    ("进度", ("进度", "完成率"), ("进度", "完成率")),
    ("倒计时", ("倒计时",), ("倒计时", "剩余时间")),
    ("记账", ("记账", "开销", "收支"), ("记账", "开销", "收支", "支出")),
    ("预算", ("预算",), ("预算", "合计")),
    ("换算", ("换算", "单位转换"), ("换算", "转换")),
    ("随机选择", ("抽签", "随机选择", "随机抽"), ("抽签", "随机", "抽取")),
    ("日历", ("日历",), ("日历", "日期")),
    ("投票", ("投票",), ("投票", "票数")),
    ("计分", ("计分", "记分"), ("计分", "记分", "分数")),
    ("菜谱", ("菜谱", "食谱"), ("菜谱", "食谱", "食材")),
    ("习惯打卡", ("习惯", "打卡"), ("习惯", "打卡", "每日", "日常")),
    ("旅行", ("旅行", "行程"), ("旅行", "行程", "行李", "出行")),
    ("简历", ("简历",), ("简历", "经历")),
    ("名片", ("名片",), ("名片", "联系")),
    ("本地图片选择", ("图库", "选择图片", "多张图片"), ('type="file"', "accept=")),
    ("拖拽导入", ("拖入", "拖拽"), ("dragover", "drop")),
    ("缩略图", ("缩略图",), ("缩略图", "thumbnail", "createobjecturl")),
    ("搜索", ("搜索",), ("搜索", "search", "filter(")),
    ("排序", ("排序",), ("排序", "sort(")),
    ("批量选择", ("批量勾选", "批量选择"), ("checkbox", "批量")),
    ("JSON 导出", ("导出为json", "导出json"), ("json.stringify", "application/json")),
)

_CSP = (
    "default-src 'none'; img-src data: blob:; media-src data: blob:; "
    "font-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    "connect-src 'none'; frame-src 'none'; child-src 'none'; worker-src blob:; "
    "object-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'"
)
_HEAD_INJECTION = (
    '<meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    f'<meta http-equiv="Content-Security-Policy" content="{_CSP}">'
)
class UnsafePageError(ValueError):
    """HTML did not pass the public-page safety boundary."""


class _SafetyParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.errors: list[str] = []
        self.has_html = False
        self.has_head = False
        self.has_body = False
        self._in_title = False
        self._title_parts: list[str] = []

    @property
    def title(self) -> str:
        return " ".join("".join(self._title_parts).split())[:80]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        self.has_html |= name == "html"
        self.has_head |= name == "head"
        self.has_body |= name == "body"
        self._in_title = name == "title"
        if name in _BLOCKED_TAGS:
            self.errors.append(f"禁止使用 <{name}>")
        values = {str(key).lower(): str(value or "").strip() for key, value in attrs}
        if name == "script" and values.get("src"):
            self.errors.append("禁止外链脚本")
        if name == "meta" and values.get("http-equiv", "").lower() == "refresh":
            self.errors.append("禁止页面跳转")
        if name == "input" and values.get("type", "text").lower() == "password":
            self.errors.append("禁止密码采集")
        for key, value in values.items():
            if key in _URL_ATTRS:
                allowed_fragment = key == "href" and (not value or value.startswith("#"))
                allowed_data = key in {"src", "poster"} and value.lower().startswith(
                    ("data:image/", "data:audio/", "data:video/")
                )
                allowed_download = (
                    name == "a"
                    and key == "href"
                    and "download" in values
                    and value.lower().startswith(
                        (
                            "data:text/plain", "data:text/csv", "data:text/json",
                            "data:application/json",
                        )
                    )
                )
                if value and not allowed_fragment and not allowed_data and not allowed_download:
                    self.errors.append("禁止外部链接或资源")
            if key == "style" and _CSS_URL.search(_SAFE_CSS_DATA_IMAGE.sub("", value)):
                self.errors.append("禁止 CSS 外部资源")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self._in_title = False

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)


def prepare_html(raw: object) -> tuple[str, str]:
    """Validate an offline single-file app and inject non-optional safeguards."""
    document = str(raw or "").strip()
    if not document or len(document) > MAX_HTML_CHARS:
        raise UnsafePageError("网页为空或过大")
    if _EXTERNAL_SCHEME.search(_INLINE_SVG_NAMESPACE.sub("", document)):
        raise UnsafePageError("网页包含外部地址")
    if _NETWORK_CODE.search(document):
        raise UnsafePageError("网页包含联网、跳转或动态执行代码")
    if _CSS_URL.search(_SAFE_CSS_DATA_IMAGE.sub("", document)):
        raise UnsafePageError("网页包含外部样式资源")
    if any(
        marker in document.lower()
        for marker in ("xiaoning-web-studio-mark", "xiaoning-shell-mark")
    ):
        raise UnsafePageError("网页包含保留标记")

    parser = _SafetyParser()
    try:
        parser.feed(document)
        parser.close()
    except (ValueError, AssertionError) as exc:
        raise UnsafePageError("HTML 结构无法解析") from exc
    if parser.errors:
        raise UnsafePageError(parser.errors[0])
    if not (parser.has_html and parser.has_head and parser.has_body):
        raise UnsafePageError("网页必须包含 html、head 和 body")
    if not parser.title:
        raise UnsafePageError("网页必须有清晰标题")

    head_match = re.search(r"<head(?:\s[^>]*)?>", document, re.I)
    body_close = list(re.finditer(r"</body\s*>", document, re.I))
    if head_match is None or not body_close:
        raise UnsafePageError("HTML 结构不完整")
    document = (
        document[: head_match.end()]
        + _HEAD_INJECTION
        + document[head_match.end() :]
    )
    if not re.match(r"\s*<!doctype\s+html", document, re.I):
        document = "<!doctype html>\n" + document
    return document, parser.title


def requirement_gaps(request: object, html: object) -> list[str]:
    """Return explicit requested feature families missing from the artifact."""
    requirement = "".join(str(request or "").lower().split())
    document = "".join(str(html or "").lower().split())
    gaps = [
        label
        for label, triggers, evidence in _COVERAGE_RULES
        if any(trigger in requirement for trigger in triggers)
        and not any(token in document for token in evidence)
    ]
    wants_persistence = any(
        phrase in requirement
        for phrase in ("刷新后仍保留", "刷新后保留", "保留数据", "本地保存", "保存记录")
    )
    if wants_persistence and "localstorage" not in document:
        gaps.append("本地保存")
    return gaps


def new_page_id() -> str:
    return secrets.token_hex(5)


@dataclass(frozen=True)
class PageRecord:
    id: str
    owner: str
    title: str
    prompt: str
    tier: str
    created_at: float
    updated_at: float

    @property
    def page_id(self) -> str:
        return self.id


class PageStore:
    """SQLite ownership, metadata and atomic daily usage; no public QQ data."""

    def __init__(self, path: Path, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS pages ("
                "id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL, "
                "prompt TEXT NOT NULL, tier TEXT NOT NULL, created_at REAL NOT NULL, "
                "updated_at REAL NOT NULL, deleted INTEGER NOT NULL DEFAULT 0)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS pages_owner_active "
                "ON pages(owner, deleted, updated_at)"
            )
            connection.execute("UPDATE pages SET tier = 'x' WHERE tier = 'go'")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS daily_usage ("
                "owner TEXT NOT NULL, day TEXT NOT NULL, used INTEGER NOT NULL, "
                "PRIMARY KEY(owner, day))"
            )

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def _day(self) -> str:
        return datetime.fromtimestamp(float(self.clock()), SHANGHAI).strftime("%Y-%m-%d")

    @staticmethod
    def _owner(value: object) -> str:
        owner = str(value or "").strip()
        if not owner:
            raise ValueError("owner is required")
        return owner

    @staticmethod
    def _id(value: object) -> str:
        page_id = str(value or "").strip().lower()
        if not _PAGE_ID.fullmatch(page_id):
            raise ValueError("invalid page id")
        return page_id

    @staticmethod
    def _record(row: sqlite3.Row | None) -> PageRecord | None:
        if row is None:
            return None
        return PageRecord(
            id=str(row["id"]), owner=str(row["owner"]), title=str(row["title"]),
            prompt=str(row["prompt"]), tier=str(row["tier"]),
            created_at=float(row["created_at"]), updated_at=float(row["updated_at"]),
        )

    def consume(self, owner: object, limit: int) -> tuple[bool, int, str]:
        identity = self._owner(owner)
        ceiling = max(0, int(limit))
        day = self._day()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT used FROM daily_usage WHERE owner = ? AND day = ?",
                (identity, day),
            ).fetchone()
            used = int(row[0]) if row else 0
            if used >= ceiling:
                return False, used, day
            used += 1
            connection.execute(
                "INSERT INTO daily_usage(owner, day, used) VALUES (?, ?, ?) "
                "ON CONFLICT(owner, day) DO UPDATE SET used = excluded.used",
                (identity, day, used),
            )
            return True, used, day

    def refund(self, owner: object, day: object | None = None) -> None:
        identity = self._owner(owner)
        usage_day = str(day or self._day()).strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", usage_day):
            raise ValueError("invalid usage day")
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE daily_usage SET used = CASE WHEN used > 0 THEN used - 1 ELSE 0 END "
                "WHERE owner = ? AND day = ?", (identity, usage_day)
            )

    def active_count(self, owner: object) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM pages WHERE owner = ? AND deleted = 0",
                (self._owner(owner),),
            ).fetchone()
            return int(row[0])

    def create(
        self, page_id: object, owner: object, title: object, prompt: object, tier: object
    ) -> PageRecord:
        identity = self._owner(owner)
        key = self._id(page_id)
        clean_title = " ".join(str(title or "").split())[:80]
        clean_prompt = str(prompt or "").strip()[:1200]
        clean_tier = str(tier or "").strip().lower()
        if not clean_title or not clean_prompt or clean_tier not in {"x", "pro"}:
            raise ValueError("invalid page metadata")
        now = float(self.clock())
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO pages(id, owner, title, prompt, tier, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key, identity, clean_title, clean_prompt, clean_tier, now, now),
            )
        record = self.get(key, identity)
        assert record is not None
        return record

    def update(
        self, page_id: object, owner: object, title: object, prompt: object | None = None
    ) -> PageRecord | None:
        key, identity = self._id(page_id), self._owner(owner)
        clean_title = " ".join(str(title or "").split())[:80]
        if not clean_title:
            raise ValueError("title is required")
        now = float(self.clock())
        with closing(self._connect()) as connection, connection:
            if prompt is None:
                connection.execute(
                    "UPDATE pages SET title = ?, updated_at = ? "
                    "WHERE id = ? AND owner = ? AND deleted = 0",
                    (clean_title, now, key, identity),
                )
            else:
                connection.execute(
                    "UPDATE pages SET title = ?, prompt = ?, updated_at = ? "
                    "WHERE id = ? AND owner = ? AND deleted = 0",
                    (clean_title, str(prompt).strip()[:1200], now, key, identity),
                )
        return self.get(key, identity)

    def get(self, page_id: object, owner: object) -> PageRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT id, owner, title, prompt, tier, created_at, updated_at "
                "FROM pages WHERE id = ? AND owner = ? AND deleted = 0",
                (self._id(page_id), self._owner(owner)),
            ).fetchone()
        return self._record(row)

    def list(self, owner: object) -> list[PageRecord]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT id, owner, title, prompt, tier, created_at, updated_at "
                "FROM pages WHERE owner = ? AND deleted = 0 "
                "ORDER BY updated_at DESC, id ASC",
                (self._owner(owner),),
            ).fetchall()
        return [record for row in rows if (record := self._record(row)) is not None]

    def delete(self, page_id: object, owner: object) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "UPDATE pages SET deleted = 1, updated_at = ? "
                "WHERE id = ? AND owner = ? AND deleted = 0",
                (float(self.clock()), self._id(page_id), self._owner(owner)),
            )
            return cursor.rowcount == 1
