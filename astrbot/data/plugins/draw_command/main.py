"""
/draw 命令 — 直接生图
"""
import urllib.parse, tempfile, os, requests
from astrbot.api.star import Context, Star
from astrbot.api.message_components import Plain, Image
from astrbot.api import logger

class DrawCommand(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)

    async def on_message(self, ctx: Context):
        msg = ctx.get_message_text().strip()
        if not msg.startswith("/draw ") and not msg.startswith("/画 "):
            return

        prompt = msg.split(" ", 1)[1].strip() if " " in msg else "cute cat"
        url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(prompt)}?width=512&height=512&nologo=true"

        try:
            # Download image first
            r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                yield ctx.reply(Plain(f"画失败了 待会再试"))
                return

            # Save to temp file
            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            tmp.write(r.content)
            tmp.close()

            yield ctx.reply(Plain(f"画好了~"))
            yield ctx.reply(Image(file=tmp.name))

        except Exception as e:
            logger.error(f"Draw failed: {e}")
            yield ctx.reply(Plain(f"画图出错了 {str(e)[:50]}"))
