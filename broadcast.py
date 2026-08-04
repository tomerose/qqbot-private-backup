"""小柠回归广播 — 发所有群+最近5个私聊"""
import os

import requests

BASE = os.environ.get("NAPCAT_HTTP_BASE", "http://127.0.0.1:5700").strip().rstrip("/")
TOKEN = os.environ.get("NAPCAT_HTTP_TOKEN", "").strip()
HEADERS = {"Content-Type": "application/json"}
if TOKEN:
    HEADERS["Authorization"] = f"Bearer {TOKEN}"

MSG = "小柠回来啦！成长了变得更聪明了，想和我聊天吗？"

# 1. Get group list
r = requests.get(f"{BASE}/get_group_list", headers=HEADERS, timeout=10)
print(f"Groups: {r.status_code}")
groups = r.json().get("data", [])
print(f"Found {len(groups)} groups")

# 2. Send to all groups
for g in groups:
    gid = g["group_id"]
    name = g.get("group_name", "?")
    try:
        r = requests.post(f"{BASE}/send_group_msg", headers=HEADERS, json={
            "group_id": gid,
            "message": MSG
        }, timeout=10)
        ok = r.json().get("status") == "ok"
        print(f"  [{gid}] {name}: {'OK' if ok else r.text[:60]}")
    except Exception as e:
        print(f"  [{gid}] {name}: FAIL - {e}")

# 3. Get recent contacts (friend list, take first 5)
r = requests.get(f"{BASE}/get_friend_list", headers=HEADERS, timeout=10)
print(f"\nFriends: {r.status_code}")
friends = r.json().get("data", [])
recent = friends[:5]
print(f"Sending to {len(recent)} friends")

for f in recent:
    uid = f["user_id"]
    name = f.get("nickname", f.get("remark", "?"))
    try:
        r = requests.post(f"{BASE}/send_private_msg", headers=HEADERS, json={
            "user_id": uid,
            "message": MSG
        }, timeout=10)
        ok = r.json().get("status") == "ok"
        print(f"  [{uid}] {name}: {'OK' if ok else r.text[:60]}")
    except Exception as e:
        print(f"  [{uid}] {name}: FAIL - {e}")

print("\nDone!")
