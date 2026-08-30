"""Generate an aggregate, privacy-safe conversation report.

Usage:
    py -3.12 scripts/user_report.py --db path/to/data_v4.db

The database path and optional platform filter are explicit inputs. The report
never prints QQ IDs, group IDs, raw messages, local paths, or provider secrets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path


POLLUTION_RE = re.compile(r"【[^】]{0,80}】")
INTENTS = {
    "技术": re.compile(r"bug|代码|报错|python|程序|服务器|部署|配置|安装|调试|api|接口|技术|编程|agent|模型|AI", re.I),
    "情感": re.compile(r"难过|失恋|喜欢|孤独|寂寞|空虚|抑郁|焦虑|痛苦|分手|难受|累|烦|不开心"),
    "功能探索": re.compile(r"你能|你会|可以帮我|能不能|可不可以|有什么功能|会什么|功能"),
    "思辨": re.compile(r"为什么|怎么看|你觉得|评价|分析|思考|哲学|意义|人生|是不是|对吗|真的"),
    "娱乐": re.compile(r"歌|音乐|明星|娱乐|游戏|唱|听|玩|电影|视频|画"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="输出不含身份信息的会话统计")
    parser.add_argument("--db", required=True, type=Path, help="AstrBot SQLite 数据库路径")
    parser.add_argument("--platform-id", default="", help="可选平台过滤值")
    return parser.parse_args()


def anonymize(value: object) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:10]


def extract_user_text(raw: object) -> str:
    try:
        messages = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    if not isinstance(messages, list):
        return ""
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        for part in message.get("content", []):
            if isinstance(part, dict) and part.get("type") == "text":
                text = POLLUTION_RE.sub("", str(part.get("text", ""))).strip()
                if text and not text.startswith(("<system", "<Quoted")):
                    return text[:400]
    return ""


def build_report(database: Path, platform_id: str = "") -> str:
    if not database.is_file():
        raise FileNotFoundError(f"数据库不存在: {database}")
    query = "SELECT user_id, content FROM conversations"
    params: tuple[str, ...] = ()
    if platform_id:
        query += " WHERE platform_id = ?"
        params = (platform_id,)
    counts: Counter[str] = Counter()
    intents: Counter[str] = Counter()
    sessions = 0
    with sqlite3.connect(database) as connection:
        for user_id, raw in connection.execute(query, params):
            text = extract_user_text(raw)
            if not text:
                continue
            sessions += 1
            counts[anonymize(user_id)] += 1
            for name, pattern in INTENTS.items():
                if pattern.search(text):
                    intents[name] += 1
    lines = [
        "小柠会话统计报告（隐私安全版）",
        f"有效用户标识数（不可逆摘要）: {len(counts)}",
        f"包含用户文本的会话数: {sessions}",
        "意图分布:",
    ]
    lines.extend(f"- {name}: {intents[name]}" for name in INTENTS)
    return "\n".join(lines)


if __name__ == "__main__":
    args = parse_args()
    print(build_report(args.db, args.platform_id))
