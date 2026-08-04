"""
GitHub 工具 — /gh 搜索仓库/用户/trending/动态
v2: 速率限制感知 + GraphQL 动态查询 + 结果缓存
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

API = "https://api.github.com"
GRAPHQL = "https://api.github.com/graphql"
HEADERS = {"User-Agent": "XiaoNingBot", "Accept": "application/vnd.github+json"}
CACHE_TTL = 300  # 5 min for trending / search results
REQUEST_TIMEOUT = 12
MAX_RESULTS = 5

# ── helpers ───────────────────────────────────────────────────────


def _rate_limit_ok(response) -> tuple[bool, str]:
    """Check GitHub rate-limit headers; returns (ok, hint)."""
    remaining = response.headers.get("X-RateLimit-Remaining", "")
    reset_epoch = response.headers.get("X-RateLimit-Reset", "")
    if remaining.isdigit() and int(remaining) == 0 and reset_epoch.isdigit():
        reset_time = datetime.fromtimestamp(int(reset_epoch), timezone.utc)
        left = max(0, (reset_time - datetime.now(timezone.utc)).total_seconds())
        minutes = max(1, round(left / 60))
        return False, f"GitHub API 速率已用尽，约 {minutes} 分钟后重置。"
    return True, ""


def _get_json(url: str, **kwargs) -> tuple[int, dict | list | None, str]:
    """Return (status, data, error_hint). Respects rate limits."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        return 0, None, f"请求失败: {type(exc).__name__}"

    ok, hint = _rate_limit_ok(resp)
    if not ok:
        return resp.status_code, None, hint
    if resp.status_code == 200:
        try:
            return 200, resp.json(), ""
        except ValueError:
            return 200, None, "响应格式异常"
    if resp.status_code == 404:
        return 404, None, "未找到"
    if resp.status_code == 403:
        return 403, None, hint or "访问被拒（可能触发速率限制）"
    return resp.status_code, None, f"请求失败 ({resp.status_code})"


def _graphql(query: str, variables: dict | None = None) -> tuple[dict | None, str]:
    """Minimal GraphQL call — only if token available."""
    token = _github_token()
    if not token:
        return None, ""
    headers = {**HEADERS, "Authorization": f"Bearer {token}"}
    try:
        resp = requests.post(
            GRAPHQL,
            json={"query": query, "variables": variables or {}},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        return None, f"GraphQL 请求失败: {type(exc).__name__}"
    if resp.status_code != 200:
        return None, f"GraphQL 错误 ({resp.status_code})"
    body = resp.json()
    if "errors" in body:
        msg = body["errors"][0].get("message", "GraphQL error")
        return None, f"GraphQL: {msg[:120]}"
    return body.get("data"), ""


def _github_token() -> str:
    """Try env GITHUB_TOKEN, fall back to empty (unauthenticated)."""
    import os
    return str(os.getenv("GITHUB_TOKEN", "") or "").strip()


# ── cache ──────────────────────────────────────────────────────────


def _cache_path(key: str) -> Path:
    cache_dir = Path(StarTools.get_data_dir("github_tools")) / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{key}.json"


def _cache_get(key: str) -> dict | None:
    import json

    path = _cache_path(key)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - float(data.get("_ts", 0)) < CACHE_TTL:
            return data.get("_payload")
    except Exception:
        pass
    return None


def _cache_set(key: str, payload) -> None:
    import json
    _cache_path(key).write_text(
        json.dumps({"_ts": time.time(), "_payload": payload}, ensure_ascii=False),
        encoding="utf-8",
    )


# ── formatting ─────────────────────────────────────────────────────


def _trunc(s: str, n: int = 200) -> str:
    text = str(s or "").strip()
    return text[:n] + ("…" if len(text) > n else "")


class GitHubTools(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        msg = str(getattr(event, "get_message_str", lambda: "")() or "").strip()
        if not msg.startswith("/gh "):
            return

        event.stop_event()
        args = msg[4:].strip()

        # Natural GitHub URL detection — extract owner/repo from pasted links
        url_match = re.match(r'https?://github\.com/([^/\s]+)/([^/\s?#]+)', args)
        if url_match:
            args = f"repo {url_match.group(1)}/{url_match.group(2)}"

        result = await self._dispatch(args)
        yield event.plain_result(result)

    async def _dispatch(self, args: str) -> str:
        try:
            lowered = args.lower()

            if lowered.startswith("repo "):
                return await self._repo(args[5:].strip())
            if lowered.startswith("user "):
                return await self._user(args[5:].strip())
            if lowered in ("trending", "热门"):
                return await self._trending()
            if lowered.startswith("events ") or lowered.startswith("动态 "):
                return await self._events(args.split(" ", 1)[1].strip())
            if lowered.startswith("readme "):
                return await self._readme(args[7:].strip())
            if lowered in ("help", "帮助", "?"):
                return self._help()
            # Default: repo search
            return await self._search(args)
        except Exception as exc:
            logger.warning("[GitHubTools] dispatch error: %s", type(exc).__name__)
            return f"出错: {_trunc(str(exc), 80)}"

    # ── commands ──────────────────────────────────────────────────

    async def _repo(self, full_name: str) -> str:
        if "/" not in full_name:
            return "格式：/gh repo owner/repo"
        status, data, err = await asyncio.to_thread(
            _get_json, f"{API}/repos/{full_name}"
        )
        if err:
            return err
        d = data if isinstance(data, dict) else {}
        return (
            f"📦 {d.get('full_name', full_name)}\n"
            f"⭐ {d.get('stargazers_count', 0)} | 🍴 {d.get('forks_count', 0)}"
            f" | 🐛 {d.get('open_issues_count', 0)} | {d.get('language', '?')}\n"
            f"{_trunc(d.get('description', '无描述'), 200)}\n"
            f"🔗 {d.get('html_url', '')}"
        )

    async def _user(self, login: str) -> str:
        status, data, err = await asyncio.to_thread(
            _get_json, f"{API}/users/{login}"
        )
        if err:
            return err
        d = data if isinstance(data, dict) else {}
        return (
            f"👤 {d.get('login', login)} ({_trunc(d.get('name', ''), 40) or '?'})\n"
            f"👥 {d.get('followers', 0)} followers | 📦 {d.get('public_repos', 0)} repos"
            f" | 📝 {d.get('public_gists', 0)} gists\n"
            f"{_trunc(d.get('bio', ''), 150)}\n"
            f"🔗 {d.get('html_url', '')}"
        )

    async def _trending(self) -> str:
        cached = _cache_get("trending")
        if cached and isinstance(cached, str):
            return cached

        since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        status, data, err = await asyncio.to_thread(
            _get_json,
            f"{API}/search/repositories?q=created:>{since}&sort=stars&order=desc&per_page={MAX_RESULTS}",
        )
        if err:
            return err
        repos = (data or {}).get("items", []) if isinstance(data, dict) else []
        if not repos:
            return "本周暂无热门项目数据。"
        lines = ["🔥 GitHub 本周热门:"]
        for i, repo in enumerate(repos[:MAX_RESULTS], 1):
            lines.append(
                f"{i}. {repo.get('full_name', '?')} ⭐{repo.get('stargazers_count', 0)}"
                f" — {_trunc(repo.get('description', ''), 50)}"
            )
        result = "\n".join(lines)
        _cache_set("trending", result)
        return result

    async def _search(self, query: str) -> str:
        cached = _cache_get(f"search:{query}")
        if cached and isinstance(cached, str):
            return cached

        status, data, err = await asyncio.to_thread(
            _get_json,
            f"{API}/search/repositories?q={query}&sort=stars&order=desc&per_page={MAX_RESULTS}",
        )
        if err:
            return err
        repos = (data or {}).get("items", []) if isinstance(data, dict) else []
        if not repos:
            return f"没搜到与 '{_trunc(query, 50)}' 相关的仓库。"
        lines = [f"🔍 '{_trunc(query, 30)}' 搜索结果:"]
        for repo in repos[:MAX_RESULTS]:
            desc = _trunc(repo.get('description', ''), 60)
            lines.append(
                f"• {repo.get('full_name', '?')} ⭐{repo.get('stargazers_count', 0)}"
                f"{' — ' + desc if desc else ''}"
            )
        result = "\n".join(lines)
        _cache_set(f"search:{query}", result)
        return result

    async def _events(self, login: str) -> str:
        """Recent public events for a user (last 10)."""
        status, data, err = await asyncio.to_thread(
            _get_json, f"{API}/users/{login}/events/public?per_page=10"
        )
        if err:
            return err
        events = data if isinstance(data, list) else []
        if not events:
            return f"{login} 最近没有公开动态。"

        lines = [f"📡 {login} 最近动态:"]
        count = 0
        for ev in events:
            if count >= 5:
                break
            etype = str(ev.get("type", "") or "").replace("Event", "")
            repo_name = str(ev.get("repo", {}).get("name", "?"))
            if etype == "Push":
                commits = len(ev.get("payload", {}).get("commits", []))
                lines.append(f"  ⬆ Push {commits} commit 到 {repo_name}")
            elif etype == "Create":
                ref_type = ev.get("payload", {}).get("ref_type", "repo")
                lines.append(f"  ✨ 创建了 {ref_type} 在 {repo_name}")
            elif etype == "Watch":
                lines.append(f"  ⭐ Star了 {repo_name}")
            elif etype == "Fork":
                lines.append(f"  🍴 Fork了 {repo_name}")
            elif etype == "Issues":
                action = ev.get("payload", {}).get("action", "?")
                lines.append(f"  🐛 {action} issue 在 {repo_name}")
            elif etype == "PullRequest":
                action = ev.get("payload", {}).get("action", "?")
                lines.append(f"  🔀 {action} PR 在 {repo_name}")
            else:
                lines.append(f"  📌 {etype} → {repo_name}")
            count += 1
        return "\n".join(lines)

    async def _readme(self, full_name: str) -> str:
        """Fetch the README (first 1500 chars)."""
        if "/" not in full_name:
            return "格式：/gh readme owner/repo"
        status, data, err = await asyncio.to_thread(
            _get_json, f"{API}/repos/{full_name}/readme"
        )
        if err:
            return err
        import base64
        d = data if isinstance(data, dict) else {}
        content_b64 = str(d.get("content", "") or "")
        try:
            text = base64.b64decode(content_b64).decode("utf-8", errors="replace")
        except Exception:
            return "README 解码失败。"
        preview = _trunc(text, 1500)
        html_url = str(d.get("html_url", ""))
        link = f"\n\n🔗 {html_url}" if html_url else ""
        return f"📄 {full_name} README:\n\n{preview}{link}"

    @staticmethod
    def _help() -> str:
        return (
            "【GitHub 工具】\n"
            "/gh repo owner/repo — 仓库信息\n"
            "/gh user <用户名> — 用户信息\n"
            "/gh trending — 本周热门\n"
            "/gh events <用户名> — 用户动态\n"
            "/gh readme owner/repo — 项目 README\n"
            "/gh <关键词> — 搜索仓库\n"
            "公开数据，无需登录。"
        )
