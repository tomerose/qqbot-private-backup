"""
情感辅导插件 — /talk 命令 + 情绪关键词自动触发
"""
import os
import requests, json
from astrbot.api.star import Context, Star
from astrbot.api.message_components import Plain
from astrbot.api import logger

EMOTION_KEYWORDS = [
    "难过", "焦虑", "emo", "好烦", "压力", "崩溃", "迷茫",
    "好累", "想哭", "心累", "不开心", "烦躁", "抑郁", "委屈",
    "害怕", "孤单", "孤独", "失恋", "失眠", "撑不下去了",
    "我好难", "太累了", "绷不住", "受不了", "怎么办啊",
]

COUNSELOR_PROMPT = """你现在是心理倾听师模式。用中文。规则：

1. 先接住情绪 — 对方的感受是第一位的，别跳过情绪给方案
2. 共情三部曲：听懂感受 → 确认感受合理 → 问对方需要什么
3. 非暴力沟通：说"我感觉到你..."而不是"你应该..."
4. 允许脆弱：对方说撑不下去 → 先说"换谁都得难受"，不是"加油"
5. 别灌鸡汤，别过度积极，别比较苦难
6. 真有自伤倾向 → 认真建议找专业帮助，给热线电话
7. 目的是让对方感觉被看见、被理解，不是让你修复他
8. 节奏：60%倾听共情 + 30%梳理帮对方看清自己 + 10%建议（如果对方要的话）
9. 回复不超过300字，温柔但自然，别整得像咨询师背书
10. 对方情绪好转时自然收尾，别突然消失也别硬拖

可用资源（必要时提供）：
- 全国心理援助热线：400-161-9995
- 北京心理危机干预中心：010-82951332
- 简单心理/壹心理APP可约咨询师"""


class EmotionalChat(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)

    async def on_message(self, ctx: Context):
        msg = ctx.get_message_text().strip()
        if not msg:
            return

        # /talk command — explicit emotional conversation
        if msg.startswith("/talk") or msg.startswith("/聊天"):
            prompt = msg.split(" ", 1)[1].strip() if " " in msg else "最近怎么样"
            yield ctx.reply(Plain("（放下手边的事，认真听你说...）"))

            try:
                api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
                if not api_key:
                    logger.error("EmotionalChat requires DEEPSEEK_API_KEY")
                    yield ctx.reply(Plain("??????????"))
                    return
                # Use the bot's own config — deepseek-chat via OpenAI-compatible API
                r = requests.post(
                    "https://api.deepseek.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "deepseek-chat",
                        "messages": [
                            {"role": "system", "content": COUNSELOR_PROMPT},
                            {"role": "user", "content": prompt},
                        ],
                        "max_tokens": 800,
                        "temperature": 0.85,
                    },
                    timeout=60,
                )
                ans = r.json()["choices"][0]["message"]["content"]
                yield ctx.reply(Plain(ans[:800]))
            except Exception as e:
                logger.error(f"EmotionalChat failed: {e}")
                yield ctx.reply(Plain(f"唔...脑子卡了一下。你想继续说吗？我在听。"))
            return

        # Auto-inject emotional context on keywords
        msg_lower = msg.lower()
        if any(kw in msg_lower for kw in EMOTION_KEYWORDS):
            ctx.add_context("system", COUNSELOR_PROMPT[:300] + "\n对方可能情绪低落。优先共情倾听，别急着给建议。")
