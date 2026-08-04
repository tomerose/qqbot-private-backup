"""RAG 维护工具：知识储备灌库 + 记忆 embedding 回填。

用法（用 AstrBot 的 Python312 环境跑）:
    python scripts/rag_maintenance.py seed       # 灌 knowledge_seed.json 到 Firestore knowledge 集合
    python scripts/rag_maintenance.py backfill   # 给所有缺 embedding 的记忆补向量
    python scripts/rag_maintenance.py check      # 统计：记忆带向量率 / 知识条数
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from google import genai
from google.cloud import firestore

PROJECT = "solar-modem-496213-f5"
DATABASE = "qqbot"
# text-embedding-004 在本项目配额极小（429），统一 gemini-embedding-001（3072 维）。
EMBED_MODEL = "gemini-embedding-001"
SEED_FILE = Path(__file__).resolve().parent / "knowledge_seed.json"

_db = firestore.Client(project=PROJECT, database=DATABASE)
_genai = genai.Client(vertexai=True, project=PROJECT, location="global")


def embed(text: str) -> list[float]:
    for attempt, wait in enumerate((0, 20, 60)):
        if wait:
            time.sleep(wait)
        try:
            result = _genai.models.embed_content(model=EMBED_MODEL, contents=text[:2000])
            return list(result.embeddings[0].values)
        except Exception:
            if attempt == 2:
                raise
    raise AssertionError("unreachable")


def seed() -> None:
    entries = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    ref = _db.collection("knowledge")
    existing = {
        (doc.to_dict() or {}).get("topic")
        for doc in ref.stream()
    }
    written = 0
    for entry in entries:
        topic = str(entry.get("topic", "")).strip()
        content = str(entry.get("content", "")).strip()
        if not topic or not content or topic in existing:
            continue
        ref.document().set({
            "topic": topic,
            "content": content,
            "category": entry.get("category", "other"),
            "embedding": embed(f"{topic}: {content}"),
            "updated_at": firestore.SERVER_TIMESTAMP,
        })
        written += 1
        print(f"  + {topic}")
        time.sleep(0.2)
    print(f"seed 完成：新写入 {written} 条，跳过已存在 {len(entries) - written} 条")


def backfill() -> None:
    total = done = 0
    for user_doc in _db.collection("users").stream():
        mem_ref = user_doc.reference.collection("memories")
        missing = [
            d for d in mem_ref.stream()
            if not isinstance((d.to_dict() or {}).get("embedding"), list)
        ]
        for doc in missing:
            data = doc.to_dict() or {}
            text = f"{data.get('key', '')}: {data.get('value', '')}".strip(": ")
            if not text:
                continue
            try:
                doc.reference.update({
                    "embedding": embed(text),
                    "updated_at": firestore.SERVER_TIMESTAMP,
                })
                done += 1
                time.sleep(0.2)
            except Exception as exc:
                print(f"  ! {user_doc.id}/{doc.id}: {type(exc).__name__}")
        total += len(missing)
    print(f"backfill 完成：补了 {done}/{total} 条记忆的 embedding")


def check() -> None:
    users = have = miss = 0
    for user_doc in _db.collection("users").stream():
        users += 1
        for doc in user_doc.reference.collection("memories").stream():
            if isinstance((doc.to_dict() or {}).get("embedding"), list):
                have += 1
            else:
                miss += 1
    knowledge = len(list(_db.collection("knowledge").stream()))
    print(f"用户 {users}，记忆带向量 {have}，缺向量 {miss}，知识储备 {knowledge} 条")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "check"
    {"seed": seed, "backfill": backfill, "check": check}[command]()
