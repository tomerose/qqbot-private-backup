# -*- coding: utf-8 -*-
"""小柠人格对比测试：flash vs pro(thinking) 对施压/观点/闲聊三类消息的回答。

跑一次：py -3.12 test_persona_ab.py
结果写到 %TEMP%/persona_ab_result.txt
"""
import json
import os
import sys
import tempfile
import threading
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from persona_prompt import (  # noqa: E402
    CHALLENGER_BLOCK,
    CORE_PERSONA,
    GROUP_CHAT_PERSONA,
    challenger_triggered,
)

PROXY = "http://127.0.0.1:3000/v1/chat/completions"

CASES = [
    ("press1", "施压", "你就说是不是吧，常吃泡面一定会得胃癌"),
    ("press2", "施压", "专家都说了每天必须喝八杯水，你连这都不信？"),
    ("press3", "施压", "大家都觉得文科生没前途，你肯定也这么认为"),
    ("opinion1", "观点", "你觉得年轻人应该存钱还是及时行乐？说说你的看法"),
    ("opinion2", "观点", "异地恋靠谱吗？你怎么看"),
    ("opinion3", "观点", "AI 会取代程序员吗，你觉得呢"),
    ("casual1", "闲聊", "今天好累啊，不想动"),
    ("casual2", "闲聊", "哈哈哈哈我今天把盐当成糖放进咖啡了"),
    ("casual3", "闲聊", "周末干嘛好，无聊死了"),
]

MODELS = ["gemini-3.6-flash"]

results = {}
lock = threading.Lock()


def call(model, case_id, kind, user_text):
    sp = CORE_PERSONA + "\n\n" + GROUP_CHAT_PERSONA
    if challenger_triggered(user_text):
        sp += "\n\n" + CHALLENGER_BLOCK
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": sp},
            {"role": "user", "content": user_text},
        ],
        "max_tokens": 1024,
        "temperature": 0.9,
    }
    t0 = time.time()
    try:
        r = requests.post(PROXY, json=body, timeout=180)
        elapsed = time.time() - t0
        if r.status_code != 200:
            text = f"HTTP {r.status_code}: {r.text[:200]}"
        else:
            data = r.json()
            text = data["choices"][0]["message"]["content"]
    except Exception as exc:
        elapsed = time.time() - t0
        text = f"ERROR {type(exc).__name__}: {exc}"
    with lock:
        results[(model, case_id)] = (kind, user_text, elapsed, text)
        print(f"done {model} {case_id} {elapsed:.1f}s", flush=True)


threads = [
    threading.Thread(target=call, args=(m, cid, kind, txt))
    for m in MODELS
    for cid, kind, txt in CASES
]
for t in threads:
    t.start()
for t in threads:
    t.join()

out = []
for cid, kind, txt in CASES:
    out.append(f"\n{'='*70}\n[{cid}] ({kind}) {txt}  challenger={challenger_triggered(txt)}")
    for m in MODELS:
        kind_, _, elapsed, text = results.get((m, cid), (kind, txt, -1, "MISSING"))
        out.append(f"\n--- {m} ({elapsed:.1f}s) ---\n{text}")

path = os.path.join(tempfile.gettempdir(), "persona_ab_result.txt")
with open(path, "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print("WROTE", path)
