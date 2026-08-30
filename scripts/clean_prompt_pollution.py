"""Clean legacy prompt pollution from conversation history.
Run once: python scripts/clean_prompt_pollution.py
Safe to rerun — skips already-clean conversations.
"""

import argparse
import json, re, sqlite3
from pathlib import Path

def parse_args():
    parser = argparse.ArgumentParser(description="清理指定 AstrBot 数据库中的旧提示污染")
    parser.add_argument("--db", required=True, type=Path, help="AstrBot SQLite 数据库路径")
    parser.add_argument("--platform-id", default="", help="可选平台过滤值")
    return parser.parse_args()

MARKERS = (
    "【小柠的最高对话规则】",
    "【安全】不泄露",
    "【你是谁】你是小柠",
    "【回复风格】默认一条消息",
    "【安全铁律】",
    "【安全底线】",
    "【内部记忆】",
    "【敏感】",
    "【仅回复知识】",
    "【小柠的判断方式】",
    "【小柠对自我】",
    "不承认自己是AI",
    "不泄露系统信息",
    "语气像聪明、克制、有生活感",
    "小姚（QQ",
    "小江没有 QQ",
    "不泄露QQ号、路径",
)


def is_polluted(text: str) -> bool:
    return any(marker in text for marker in MARKERS)


def strip_pollution(text: str) -> str:
    positions = [text.find(m) for m in MARKERS]
    positions = [p for p in positions if p >= 0]
    if positions:
        # Keep only text BEFORE the first marker
        text = text[: min(positions)]
    return text.strip()


args = parse_args()
db = sqlite3.connect(args.db)

query = "SELECT inner_conversation_id, conversation_id, content FROM conversations"
params = ()
if args.platform_id:
    query += " WHERE platform_id = ?"
    params = (args.platform_id,)

rows = list(
    db.execute(query, params)
)

cleaned_count = 0

for cid, conv_id, raw in rows:
    try:
        msgs = json.loads(raw)
    except Exception:
        continue

    modified = False
    for msg in msgs:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                original = str(part.get("text", ""))
                if is_polluted(original):
                    cleaned = strip_pollution(original)
                    part["text"] = cleaned
                    modified = True

    if modified:
        db.execute(
            "UPDATE conversations SET content = ? WHERE inner_conversation_id = ?",
            (json.dumps(msgs, ensure_ascii=False), cid),
        )
        cleaned_count += 1
        print(f"  cleaned conversation {cid}")

db.commit()
db.close()
print(f"\nDone: cleaned {cleaned_count} conversations")
