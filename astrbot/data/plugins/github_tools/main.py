"""
GitHub 工具 — /gh 搜索仓库/用户/trending
"""
import requests, json
from datetime import datetime, timedelta
from astrbot.api.star import Context, Star
from astrbot.api.message_components import Plain

class GitHubTools(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)

    async def on_message(self, ctx: Context):
        msg = ctx.get_message_text().strip()
        if not msg.startswith("/gh "):
            return

        args = msg[4:].strip()
        try:
            if args.startswith("repo "):
                repo = args[5:].strip()
                r = requests.get(f"https://api.github.com/repos/{repo}",
                    headers={"User-Agent": "XiaoNingBot", "Accept": "application/vnd.github+json"}, timeout=10)
                if r.status_code == 200:
                    d = r.json()
                    text = f"📦 {d['full_name']}\n⭐ {d['stargazers_count']} | 🍴 {d['forks_count']} | {d.get('language','?')}\n{d.get('description','无描述')[:200]}\n🔗 {d['html_url']}"
                else:
                    text = f"没找到 {repo}"
                yield ctx.reply(Plain(text))

            elif args.startswith("user "):
                user = args[5:].strip()
                r = requests.get(f"https://api.github.com/users/{user}",
                    headers={"User-Agent": "XiaoNingBot"}, timeout=10)
                if r.status_code == 200:
                    d = r.json()
                    text = f"👤 {d['login']} ({d.get('name','?')})\n followers: {d['followers']} | repos: {d['public_repos']}\n{d.get('bio','')[:150]}\n🔗 {d['html_url']}"
                else:
                    text = f"没找到 {user}"
                yield ctx.reply(Plain(text))

            elif args == "trending":
                since = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
                r = requests.get(f"https://api.github.com/search/repositories?q=created:>{since}&sort=stars&order=desc&per_page=5",
                    headers={"User-Agent": "XiaoNingBot"}, timeout=10)
                if r.status_code == 200:
                    repos = r.json().get("items", [])
                    lines = ["🔥 GitHub 本周热门:"]
                    for i, repo in enumerate(repos, 1):
                        lines.append(f"{i}. {repo['full_name']} ⭐{repo['stargazers_count']}")
                    text = "\n".join(lines)
                else:
                    text = "搜索失败 待会再试"
                yield ctx.reply(Plain(text))

            else:
                # Search repos
                r = requests.get(f"https://api.github.com/search/repositories?q={args}&sort=stars&per_page=5",
                    headers={"User-Agent": "XiaoNingBot"}, timeout=10)
                if r.status_code == 200:
                    repos = r.json().get("items", [])
                    if repos:
                        lines = [f"🔍 '{args}' 搜索结果:"]
                        for repo in repos[:5]:
                            lines.append(f"• {repo['full_name']} ⭐{repo['stargazers_count']} - {repo.get('description','')[:60]}")
                        text = "\n".join(lines)
                    else:
                        text = f"没搜到 {args}"
                else:
                    text = "搜索失败 待会再试"
                yield ctx.reply(Plain(text))

        except Exception as e:
            yield ctx.reply(Plain(f"出错: {str(e)[:50]}"))
