"""Werewolf/Mafia host. Ordinary groups get 3 games/day; X/Pro get unlimited AI narration."""

from __future__ import annotations

import asyncio
import random
import time
from pathlib import Path

import requests
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
try:
    from xiaoning_runtime import defer_stop_event
except ImportError:
    from data.plugins.xiaoning_runtime import defer_stop_event

try:
    from draw_command.pro_access import get_tier, is_active_pro_group, Tier
except ImportError:
    from data.plugins.draw_command.pro_access import get_tier, is_active_pro_group, Tier

PROXY = "http://127.0.0.1:3000/v1/chat/completions"
PRO_MSG = "普通版每日 3 局。X/Pro 不限局数并启用 AI 旁白。发送 /pro status 查看资格。"

ROLES_8 = ["狼人", "狼人", "预言家", "女巫", "猎人", "村民", "村民", "村民"]
VOTE_SECONDS = 90
DISCUSS_SECONDS = 120


class WerewolfGame(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}
        project_root = Path(__file__).resolve().parents[4]
        self._pro_db = (
            project_root / "astrbot" / "data" / "plugin_data"
            / "xiaoning_pro" / "pro_members.db"
        )
        self._games: dict[str, dict] = {}  # group_id -> game state
        self._daily_free: dict[str, int] = {}  # group_id:date -> count

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=940)
    @defer_stop_event
    async def on_message(self, event: AstrMessageEvent):
        text = self._msg(event)
        group_id = str(getattr(event, "get_group_id", lambda: "")() or "")
        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
        if not group_id or not sender_id:
            return

        # Start game
        if text.startswith("/werewolf") or text.startswith("/狼人杀"):
            event.stop_event()
            if text.split()[-1].isdigit():
                player_count = int(text.split()[-1])
            else:
                player_count = 8
            if player_count != 8:
                yield event.plain_result("当前稳定版仅支持 8 人局。用法：/werewolf 8")
                return

            if self._games.get(group_id):
                yield event.plain_result("本群已有一局狼人杀在进行中。")
                return

            # Daily free limit — skip if personal Pro or Pro group
            tier = get_tier(sender_id, self._pro_db)
            in_pro_group = bool(group_id) and is_active_pro_group(group_id, self._pro_db)
            if tier < Tier.X and not in_pro_group:
                today = time.strftime("%Y%m%d")
                key = f"{group_id}:{today}"
                used = self._daily_free.get(key, 0)
                if used >= 3:
                    yield event.plain_result(PRO_MSG)
                    return
                self._daily_free[key] = used + 1

            game = {
                "group_id": group_id,
                "players": [],
                "roles": {},
                "alive": set(),
                "phase": "joining",
                "round": 0,
                "witch_has_antidote": True,
                "witch_has_poison": True,
                "narrator_pro": tier >= Tier.X or in_pro_group,
                "started_at": time.time(),
                "player_count": player_count,
            }
            self._games[group_id] = game

            yield event.plain_result(
                f"🐺 狼人杀 {player_count}人局 开始组队！\n"
                f"回复 '/join' 加入。人满自动开始。\n"
                f"当前：0/{player_count}"
            )
            return

        game = self._games.get(group_id)
        if game is None:
            return

        # Join game
        if text.strip() == "/join":
            event.stop_event()
            if sender_id in game["players"]:
                yield event.plain_result("你已在游戏中了。")
                return
            game["players"].append(sender_id)
            target = game["player_count"]
            yield event.plain_result(
                f"✅ {sender_id[-4:]} 加入了游戏。当前：{len(game['players'])}/{target}"
            )
            if len(game["players"]) >= target:
                async for msg in self._start_game(event, group_id):
                    yield msg
            return

        # Vote during day phase
        if text.startswith("/vote") or text.startswith("/投票"):
            event.stop_event()
            if game.get("phase") != "day_vote":
                return
            if sender_id not in game["alive"]:
                yield event.plain_result("只有存活玩家可以投票。")
                return
            parts = text.split()
            raw_target = parts[1] if len(parts) > 1 else ""
            digits = "".join(ch for ch in raw_target if ch.isdigit())
            matches = [pid for pid in game["alive"] if pid == digits or pid.endswith(digits)]
            if len(matches) != 1:
                yield event.plain_result("目标无效，请用 /vote QQ号末4位 投票。")
                return
            target = matches[0]
            votes = game.setdefault("votes", {})
            votes[sender_id] = target
            game["votes"] = votes
            yield event.plain_result(f"🗳️ 已投票。当前 {len(votes)}/{len(game['alive'])} 票。")
            return

    async def _start_game(self, event: AstrMessageEvent, group_id: str):
        game = self._games[group_id]
        players = game["players"][:8]
        random.shuffle(players)

        roles = dict(zip(players, ROLES_8))
        game["roles"] = roles
        game["alive"] = set(players)
        game["phase"] = "night"
        game["round"] = 1

        yield event.plain_result(
            f"🐺 游戏开始！{len(players)} 位玩家，角色已私聊分配。\n"
            f"🌙 天黑请闭眼…"
        )

        # DM roles to each player
        for pid, role in roles.items():
            try:
                platform = "aiocqhttp"
                origin = str(getattr(event, "unified_msg_origin", "") or "")
                if ":" in origin:
                    platform = origin.split(":", 1)[0]
                session = f"{platform}:FriendMessage:{pid}"
                await self.context.send_message(
                    session,
                    [Plain(f"🎭 你的角色是：{role}\n"
                           f"狼人同伴：{'、'.join(p[-4:] for p, r in roles.items() if r == '狼人' and p != pid) if role == '狼人' else '无'}")],
                )
            except Exception:
                pass

        # --- NIGHT PHASE (simplified: random victim) ---
        await asyncio.sleep(3)

        wolves = [p for p, r in roles.items() if r == "狼人" and p in game["alive"]]
        victims = [p for p in game["alive"] if p not in wolves]
        if victims:
            victim = random.choice(victims)
            # Witch can save (simplified: 50% chance to save if has antidote)
            if game["witch_has_antidote"] and random.random() < 0.5:
                game["witch_has_antidote"] = False
            else:
                game["alive"].discard(victim)

        # --- DAY PHASE ---
        game["phase"] = "day"
        dead_this_round = [p for p in players if p not in game["alive"]]
        last_dead = dead_this_round[-1] if dead_this_round else None

        if last_dead:
            last_role = roles.get(last_dead, "?")
            msg = f"☀️ 天亮了。昨晚 @{last_dead[-4:]} 被杀了，身份是【{last_role}】。"
        else:
            msg = "☀️ 天亮了。昨晚是平安夜，没有人死亡。"

        # Narrator AI (Pro only)
        if game.get("narrator_pro"):
            try:
                resp = await asyncio.to_thread(
                    requests.post,
                    PROXY,
                    json={
                        "model": "gemini-2.5-flash",
                        "messages": [
                            {"role": "system", "content": "你是狼人杀的旁白主持人。用戏剧化的语言描述夜晚发生的事情，50 字以内。"},
                            {"role": "user", "content": f"昨晚死亡：{last_dead[-4:] if last_dead else '无人'}"},
                        ],
                        "max_tokens": 200,
                    },
                    timeout=20,
                )
                resp.raise_for_status()
                narration = resp.json()["choices"][0]["message"]["content"].strip()
                msg += f"\n\n📖 旁白：{narration}"
            except Exception:
                pass

        msg += f"\n\n存活玩家：{'、'.join(p[-4:] for p in game['alive'])}"
        msg += f"\n🗳️ 投票阶段：回复 '/vote @玩家' 投票，{VOTE_SECONDS} 秒后截止。"

        game["phase"] = "day_vote"
        yield event.plain_result(msg)

        # Wait for votes
        await asyncio.sleep(VOTE_SECONDS)

        game["phase"] = "results"

        # Tally votes
        votes = game.get("votes", {})
        tally: dict[str, int] = {}
        for _, target in votes.items():
            tally[target] = tally.get(target, 0) + 1

        if tally:
            eliminated = max(tally, key=tally.get)
            game["alive"].discard(eliminated)
            erole = roles.get(eliminated, "?")
            result = (
                f"📊 投票结束。@{eliminated[-4:]} 被票选出局，身份是【{erole}】。"
            )
        else:
            result = "📊 无人投票，今天不淘汰任何人。"

        # Check win conditions
        wolves_alive = [p for p in game["alive"] if roles.get(p) == "狼人"]
        villagers_alive = [p for p in game["alive"] if roles.get(p) != "狼人"]

        if not wolves_alive:
            result += "\n\n🏆 游戏结束！村民阵营胜利！所有狼人已被消灭。"
        elif len(wolves_alive) >= len(villagers_alive):
            result += "\n\n🐺 游戏结束！狼人阵营胜利！狼人占领了村庄。"
        else:
            result += f"\n\n🌙 进入第 {game['round'] + 1} 轮夜晚…（简化版仅1轮，游戏结束）"
            result += "\n\n🏆 游戏结束！简化版仅支持 1 轮。完整版即将上线。"

        yield event.plain_result(result)

        # Cleanup
        self._games.pop(group_id, None)

    @staticmethod
    def _msg(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_message_str", None)
        if callable(getter):
            return str(getter() or "")
        return str(getattr(event, "message_str", "") or "")
