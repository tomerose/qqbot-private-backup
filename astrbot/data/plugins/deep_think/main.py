"""
/think 命令 — 用 Gemini Pro 深度推理，暴露推理链+置信度
"""
import requests
from astrbot.api.star import Context, Star
from astrbot.api.message_components import Plain
from astrbot.api import logger

THINK_PROMPT = """你是高智商分析师。用中文回答。按以下格式输出：

## 推理过程
- 先拆解问题本质（第一性原理）
- 列出关键变量和约束
- 给出推理链条：A→B→C→D
- 如果有多个角度，列出来（乐观/悲观/中性）

## 结论
- 核心结论一句话
- 置信度：[高/中/低] — 依据什么

## 不确定的地方
- 如果有什么你拿不准的，直接说"以下部分我不确定：..."

逻辑严密，直击本质，不说废话。不确定就说不确定。"""


class DeepThink(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)

    async def on_message(self, ctx: Context):
        msg = ctx.get_message_text().strip()
        if not msg.startswith("/think ") and not msg.startswith("/推理 "):
            return

        question = msg.split(" ", 1)[1].strip() if " " in msg else msg[7:].strip()
        yield ctx.reply(Plain("思考中，稍等..."))

        try:
            r = requests.post(
                "http://127.0.0.1:3000/v1/chat/completions",
                json={
                    "model": "gemini-2.5-pro",
                    "messages": [
                        {"role": "system", "content": THINK_PROMPT},
                        {"role": "user", "content": question},
                    ],
                    "max_tokens": 2000,
                },
                timeout=90,
            )
            data = r.json()
            ans = data["choices"][0]["message"]["content"]

            # Truncate if too long for QQ
            if len(ans) > 2000:
                ans = ans[:1900] + "\n\n...(截断，太长了)"

            yield ctx.reply(Plain(ans))
        except Exception as e:
            logger.error(f"DeepThink failed: {e}")
            yield ctx.reply(Plain(f"脑子卡住了，待会再试: {str(e)[:50]}"))
