"""One-shot update broadcast after AstrBot is ready."""

import asyncio
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star

MSG = """【男性结扎（输精管结扎）科普｜请勿自行操作】

这是一种由泌尿外科/男科医生完成的永久性避孕手术：医生处理输精管，使精子不再进入精液。它通常不影响睾酮、性欲、勃起、射精感或长期性生活，但不能预防艾滋病等性传播感染。

术前：确认自己未来基本不再生育，并和伴侣充分沟通；向医生说明既往病史、正在用的药物和出血风险。复通手术更复杂，且不能保证恢复生育，所以不要把结扎当作“以后一定能恢复”的临时措施。

术后护理（以手术医生医嘱为准）：前24—48小时多休息，可隔着衣物冰敷，每次约15—20分钟；穿有支撑性的内裤；性生活/自慰通常至少暂停2—7天，跑步、骑车、提重物等剧烈活动通常暂停约1—2周。

最重要：术后不会立刻失去生育能力。复查精液确认“无精子/达到医生认可标准”前，必须继续使用其他避孕方法；复查时间常在术后约12周，但以医院安排为准。

常见短期反应：轻度疼痛、肿胀、淤青。若出现发热（约38℃或以上）、疼痛或肿胀越来越重、明显发红发热、脓性分泌物或持续出血，应尽快联系手术医院。

这只是健康科普，不替代面诊。需要考虑结扎，请挂正规医院泌尿外科/男科。"""
UPDATE_MARKER = Path(__file__).with_name(".vasectomy-v1-sent")


def _recent_user_ids(rows: list[dict], limit: int = 5) -> list[str]:
    result = []
    for row in rows:
        user_id = row.get("user_id") or row.get("peerUid") or row.get("uin")
        chat_type = row.get("chatType", row.get("chat_type", "private"))
        if user_id is None or str(chat_type).lower() in {"group", "2"}:
            continue
        user_id = str(user_id)
        if user_id not in result:
            result.append(user_id)
        if len(result) >= limit:
            break
    return result


class TempBroadcast(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self._done = False

    @filter.on_platform_loaded()
    async def broadcast(self):
        if self._done or UPDATE_MARKER.exists():
            return
        self._done = True

        try:
            bot = None
            for _ in range(12):
                bot = next(
                    (inst.get_client() for inst in self.context.platform_manager.platform_insts
                     if hasattr(inst.get_client(), "get_group_list")),
                    None,
                )
                if bot:
                    break
                await asyncio.sleep(5)
            if not bot:
                logger.error("[Broadcast] No aiocqhttp client")
                self._done = False
                return

            groups = None
            for _ in range(12):
                try:
                    groups = await bot.get_group_list()
                    break
                except Exception as exc:
                    logger.warning(f"[Broadcast] group list not ready: {exc!r}")
                    await asyncio.sleep(5)
            if groups is None:
                logger.error("[Broadcast] group list unavailable")
                self._done = False
                return
            for group in groups:
                try:
                    await bot.send_group_msg(group_id=group["group_id"], message=MSG)
                except Exception as exc:
                    logger.warning(f"[Broadcast] group {group.get('group_id')} failed: {exc}")
                await asyncio.sleep(0.6)

            UPDATE_MARKER.touch()
            logger.info(f"[Broadcast] pause notice sent to {len(groups)} groups")
        except Exception as exc:
            logger.error(f"[Broadcast] fatal: {exc}")

    async def on_message(self, ctx: Context):
        pass
