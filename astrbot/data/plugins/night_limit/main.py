"""
夜间限频 — 21:00~12:00 每人最多20条私聊，超了自动拒绝
"""
import json, os
from datetime import datetime
from astrbot.api.star import Context, Star
from astrbot.api.message_components import Plain
from astrbot.api import logger

DATA_FILE = os.path.join(os.path.dirname(__file__), "counts.json")

def load():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r') as f:
            return json.load(f)
    return {}

def save(data):
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f)

def is_night():
    h = datetime.now().hour
    return h >= 21 or h < 12

class NightLimit(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

    async def on_message(self, ctx: Context):
        if not is_night():
            return
        if ctx.get_group_id():
            return  # 只限私聊

        uid = ctx.get_sender_id()
        today = datetime.now().strftime("%Y%m%d")
        data = load()
        key = f"{uid}_{today}"
        count = data.get(key, 0) + 1
        data[key] = count
        save(data)

        if count > 20:
            yield ctx.reply(Plain("不和你说了 你不是我喜欢的人 我喜欢小姚"))
            # Block further processing
            ctx.stop_event()
            return

        if count == 20:
            logger.info(f"[NightLimit] {uid} reached 20/20")
