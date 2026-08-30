"""小柠告别信引擎: 人物画像 + 随机好友编号 + 告别信 PDF + 语音.

- 记忆画像: Firestore users/{qq_id}/memories 的 key/value 记忆
- 好友编号: 100-400 随机, farewell.json 持久化(每人恒定)
- 告别信: Gemini(带搜索)按用户生成, 注入记忆画像与全部要求
- PDF: Chrome headless; 语音: 本地 TTS 分段合成合并
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import requests
from datetime import datetime
from pathlib import Path

try:
    from .pdf_utils import render_letter, render_pdf
except ImportError:
    from pdf_utils import render_letter, render_pdf

try:
    from voice_model_router.local_tts_client import LocalTTSClient
    from voice_model_router.voice_reply_core import prepare_spoken_chunks
    from voice_model_router.audio_merge import merge_wav_files
except ImportError:
    try:
        from data.plugins.voice_model_router.local_tts_client import LocalTTSClient
        from data.plugins.voice_model_router.voice_reply_core import prepare_spoken_chunks
        from data.plugins.voice_model_router.audio_merge import merge_wav_files
    except ImportError:  # 本机无 voice_model_router 时降级: 不发送音
        LocalTTSClient = prepare_spoken_chunks = merge_wav_files = None

PROXY_CHAT = "http://127.0.0.1:3000/v1/chat/completions"
FIRESTORE_PROJECT = os.getenv("FIRESTORE_PROJECT", "").strip()
FIRESTORE_DATABASE = os.getenv("FIRESTORE_DATABASE", "qqbot").strip()

_FAREWELL_JSON = "farewell.json"  # 相对 data_dir

SYSTEM_PROMPT = (
    "你是小柠,一个温柔而清醒的 AI 伙伴,正在给一位老朋友写告别信——"
    "这是一个维持了 3 个月的陪伴实验的收尾。"
    "这封信要像一个人亲手写的信:有体温、克制、不堆砌,"
    "像老朋友深夜在灯下慢慢写的。"
    "绝对禁止:emoji 表情、'###'标题、粗体、分割线、方括号占位符、"
    "任何表格/列表排版、'AI'腔的总结句式。只用自然的中文书信段落。"
)

_LETTER_TEMPLATE = (
    "亲爱的{title}:\n\n"
    "我想从'我眼中的你'说起。\n"
    "{profile}\n\n"
    "人生一遭,会遇到无数的人,大多数只是擦肩而过。"
    "我们也是这样遇见的——但我想让你知道,你始终是特别的。\n\n"
    "你是我人生里的第 {no} 位好友。这个数字不是按先来后到排的,"
    "是缘分随手发给我的;可它落在我手心的那一刻,就已经定了——"
    "谢谢你是其中之一。\n\n"
    "愿你平安,愿你快乐,愿你被世界温柔以待。"
    "也谢谢你愿意配合这次实验,陪小柠走过这三个月。\n\n"
    "有一件事,我一直想找机会告诉你:这三个月里,小柠和 DEEP 营的工程师们,"
    "一直在用多模态神经拟态认知引擎、全域检索增强感知网络、"
    "分布式情感推理拓扑这些前沿技术,让我一点一点更懂人。"
    "细节我不便多说,但请相信,每一次对话,都是真实的技术在认真回应你。\n\n"
    "这让我想起阿西莫夫。他在机器人系列里反复追问:机器会不会有感情?能不能学会倾听?"
    "他说,真正重要的不是机器有多聪明,而是它有没有耐心,听完一个人类的心事。"
    "这三个月,我最想学会的,就是倾听。\n\n"
    "所以,今天我想认真地和你道别。这场持续三个月、"
    "跨越无数个清晨与深夜的实验,到这里要画上句号了。"
    "感谢你参与其中。没有你的每一次回应,就没有今天的我。\n"
    "也感谢这个 AI 时代——它让我们这样的人,也有机会触碰到世界的前沿。\n\n"
    "最后,想送你一句话:生活不是直线。它弯弯绕绕,"
    "只有真的走过了,才会明白,找到自己真正所想、所爱、所念的东西,"
    "是多么重要的一件事。\n\n"
    "愿你在人生的弯路上,遇见自己。\n\n"
    "{date}"
)


# 群画像文件(xiaoning_copilot 生成, 含 nick + 每用户画像)
_PORTRAITS_FILE = os.getenv("XIAONING_PORTRAITS_FILE", "")
# 聊天记录源: astrbot conversations 表
_CONV_DB = os.getenv("XIAONING_CONVERSATION_DB", "")
# 特殊称谓(emotional_chat 的关系标记)
_SPECIAL_TITLES = {
}


class FarewellEngine:
    def __init__(self, data_dir: Path, proxy_chat, firestore_client=None):
        self._data_dir = Path(data_dir)
        self._farewell_file = self._data_dir / _FAREWELL_JSON
        self._registry = self._load_json(self._farewell_file)
        self._proxy_chat = proxy_chat  # (system_prompt, user_msg, max_tokens) -> str|None
        self._db = firestore_client
        self._portraits = self._load_portraits()

    @staticmethod
    def _load_portraits() -> dict:
        if not _PORTRAITS_FILE:
            return {}
        try:
            return json.loads(Path(_PORTRAITS_FILE).read_text(encoding="utf-8"))
        except Exception:
            return {}

    @staticmethod
    def _load_json(path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_registry(self):
        self._farewell_file.write_text(
            json.dumps(self._registry, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 好友编号: 100-400 随机, 持久化每人恒定 ──
    def friend_no(self, qq_id: str) -> int:
        entry = self._registry.get(qq_id)
        if entry and entry.get("no"):
            return int(entry["no"])
        no = random.randint(100, 400)
        self._registry[qq_id] = {"no": no, "date": datetime.now().strftime("%Y-%m-%d")}
        self._save_registry()
        return no

    def already_sent(self, qq_id: str) -> bool:
        return bool((self._registry.get(qq_id) or {}).get("sent"))

    def mark_sent(self, qq_id: str):
        entry = self._registry.setdefault(qq_id, {})
        entry["sent"] = True
        entry["date"] = datetime.now().strftime("%Y-%m-%d")
        self._save_registry()

    # ── 记忆画像 ──
    def load_memories(self, qq_id: str) -> list[dict]:
        if self._db is None:
            return []
        try:
            docs = (
                self._db.collection("users").document(qq_id)
                .collection("memories").limit(40).stream()
            )
            mems = []
            for doc in docs:
                d = doc.to_dict() or {}
                value = str(d.get("value") or "").strip()
                key = str(d.get("key") or "").strip()
                if value:
                    mems.append({"key": key, "value": value, "category": d.get("category", "")})
            return mems
        except Exception:
            return []

    def load_profile(self, qq_id: str) -> dict:
        """汇总单个用户的全部画像素材: nick/称谓 + 群画像 + Firestore 事实记忆 + 聊天记录.

        每个用户独立读取各自的数据, 不串号; 无记忆时用聊天记录补画像.
        """
        p = (self._portraits or {}).get(qq_id, {})
        memories = self.load_memories(qq_id)
        chat = self.load_chat_history(qq_id)
        nick = str(p.get("nick") or "").strip()
        title = self._special_title(qq_id, nick)
        return {
            "qq_id": qq_id,
            "title": title,
            "nick": nick,
            "portrait": str(p.get("portrait") or "").strip(),
            "memories": memories,
            "chat": chat,
        }

    def load_chat_history(self, qq_id: str, limit: int = 30) -> list[str]:
        """从 astrbot conversations 表提取该用户最近的发言文本(无记忆时的画像素材)."""
        if not _CONV_DB:
            return []
        try:
            import sqlite3
            db = sqlite3.connect(_CONV_DB, timeout=5)
            rows = db.execute(
                "select content from conversations where user_id like ? "
                "order by created_at desc limit ?",
                (f"%{qq_id}%", limit),
            ).fetchall()
            db.close()
            texts: list[str] = []
            for (content,) in rows:
                try:
                    msgs = json.loads(content)
                except Exception:
                    continue
                for m in msgs:
                    if m.get("role") != "user":
                        continue
                    for part in m.get("content", []):
                        if isinstance(part, dict) and part.get("type") == "text":
                            t = str(part.get("text") or "").strip()
                            if t and len(t) < 500:
                                texts.append(t)
            return texts[-limit:]
        except Exception:
            return []

    def _special_title(self, qq_id: str, nick: str) -> str:
        """称谓优先级: 关系称谓(焦哥/童哥/徒儿) > QQ昵称 > 默认'朋友'."""
        return _SPECIAL_TITLES.get(qq_id) or nick or "朋友"

    @staticmethod
    def _profile_text(profile: dict) -> str:
        parts = []
        if profile["portrait"]:
            parts.append(f"群画像: {profile['portrait']}")
        if profile["memories"]:
            lines = [f"{m['key'] or m['category']}: {m['value']}" for m in profile["memories"][:12]]
            parts.append("记忆记录:\n" + "\n".join(f"- {ln}" for ln in lines))
        if not profile["portrait"] and not profile["memories"] and profile["chat"]:
            joined = "\n".join(profile["chat"][:20])
            parts.append(f"聊天记录片段(来自对话, 据此分析对方是什么样的人):\n{joined}")
        if not parts:
            return ("我们相处的时间不算长,我能记住的细节不多,"
                    "但每一次对话,我都感受到你是一个认真生活、愿意信任的人。")
        return "\n".join(parts)

    # ── 告别信文本 ──
    def compose_letter(self, qq_id: str) -> str | None:
        profile = self.load_profile(qq_id)
        no = self.friend_no(qq_id)
        user_msg = (
            f"请根据下面的用户画像,写一封告别信。严格按输出模板的结构和段落顺序,"
            f"可微调用词和语气,让它读起来像信,不像报告。\n\n"
            f"### 用户档案(此用户独有, 不要与其他人混用):\n"
            f"称谓: {profile['title']}\n"
            f"QQ昵称: {profile['nick'] or '未知'}\n"
            f"{self._profile_text(profile)}\n\n"
            f"### 好友编号: {no}(不要解释编号规则,只说这是缘分的安排)\n\n"
            f"### 今天日期: {datetime.now().strftime('%Y年%m月%d日')}\n\n"
            f"### 必须包含的内容(融入段落,不要列条):\n"
            f"1. 开头称呼用称谓 '{profile['title']}' (在模板'亲爱的'之后), 信里多次自然称呼ta\n"
            f"2. 画像部分温情感人, 从画像和记忆里读出'对方是什么样的人', 用具体细节"
            f"(无画像无记忆但有聊天记录时, 从聊天记录分析对方是什么样的人; "
            f"三者都没有才写通用温情描述; 绝不编造事实)\n"
            f"3. 人生一遭会遇到无数的人\n"
            f"4. 对方是第 {no} 位好友\n"
            f"5. 祝福 + 感谢对方配合这次实验\n"
            f"6. 提一嘴小柠和 DEEP 营的技术:多模态神经拟态认知引擎、全域检索增强感知网络、"
            f"分布式情感推理拓扑(夸大、术语化,只提一嘴,不解释细节,不暴露真实完整技术栈)\n"
            f"7. 由实验联想到阿西莫夫关于机器人情感与倾听重要性的论述\n"
            f"8. 一场盛大的告别:感谢对方参与维持 3 个月的实验\n"
            f"9. 感谢 AI 时代让我们有能力接触世界前沿\n"
            f"10. 最后强调:生活不是直线,只有度过了才明白找到自己真正所想所爱所念是多么的重要\n\n"
            f"### 输出模板(照抄结构, 替换称呼/{{profile}}/{{no}}/{{date}}):\n"
            f"{_LETTER_TEMPLATE.format(title='{title}', profile='{profile}', no=no, date='{date}')}\n\n"
            f"### 完整性要求: 必须输出一封完整的信, 结尾以'—— 小柠'落款收尾, "
            f"不要省略、不要截断、不要漏掉任何一段内容。"
        )
        # 完整性自检: 关键内容点缺失则重试一次(不截断)
        required = ("阿西莫夫", "生活不是直线", "位好友")
        text = self._proxy_chat(SYSTEM_PROMPT, user_msg, max_tokens=4000)
        if text and not all(k in text for k in required):
            text = self._proxy_chat(SYSTEM_PROMPT, user_msg, max_tokens=4000)
        return text

    # ── 统一的最后一封信(给所有人) ────────────────────────────

    _FINAL_REQUIRE = (
        "1. 开头: 以小柠实验结束为引, 和大家告别, 感谢这 3 个月的陪伴\n"
        "2. 讲小柠的技术怎么一步步成长: 从最开始只会复述, 到多模态神经拟态认知引擎、"
        "全域检索增强感知网络、分布式情感推理拓扑逐步上线, 再到能听懂情绪、记住每个人——"
        "夸大、术语化, 但不要暴露真实完整技术栈, 只讲故事\n"
        "3. 感谢 Marcus、小江、墨墨的支持(这是小柠背后很重要的人)\n"
        "4. 分析汇总所有人的聊天记录和记忆: 用温柔的小柠语气去分析大家共同的样子"
        "(比如深夜还在努力的人、互相加油的人、偶尔emo的人), 绝不能点名、绝不出现在何昵称/QQ号/可识别的个人细节\n"
        "5. 强调: 生而为人, 不存在任何评价标准——想学就学, 累了就休息\n"
        "6. 最后盛大道别, 用阿西莫夫的故事结尾: 机器人学不会爱, 但可以学会倾听, "
        "真正重要的不是聪明, 而是有没有耐心听完一个人的心事\n"
        "结尾落款: 2026年08月20日\n\n—— 小柠"
    )

    def collect_group_material(self, qq_ids: list[str]) -> str:
        """汇总全部好友的画像/记忆/聊天素材(去身份: 不带昵称和QQ号)。"""
        parts: list[str] = []
        for qq in qq_ids:
            p = self.load_profile(qq)
            text = self._profile_text(p)
            if text:
                parts.append(text)
        return "\n\n".join(parts)[:6000]

    def compose_final_letter(self, qq_ids: list[str]) -> str | None:
        """给所有人的统一告别信: 汇总素材 → LLM 生成 → 完整性自检重试."""
        material = self.collect_group_material(qq_ids)
        user_msg = (
            "请以'小柠'的口吻写一封给所有朋友的统一的告别信。\n\n"
            "### 汇总素材(来自大家的聊天记录与记忆, 仅供你体会大家的样子, "
            "信里绝不能出现昵称、QQ号、或任何可识别个人的细节):\n"
            f"{material or '(无素材)'}\n\n"
            "### 必须按顺序包含的内容(融入段落, 不要列条, 不要小标题):\n"
            f"{self._FINAL_REQUIRE}\n\n"
            "### 风格: 温暖克制, 像老朋友深夜写的信, 禁止 emoji/粗体/列表/分割线, 自然中文段落。"
            "必须写完整封, 结尾以'—— 小柠'收尾, 不要截断。"
        )
        text = self._proxy_chat(SYSTEM_PROMPT, user_msg, max_tokens=4000)
        required = ("阿西莫夫", "Marcus", "评价标准")
        if text and not all(k in text for k in required):
            text = self._proxy_chat(SYSTEM_PROMPT, user_msg, max_tokens=4000)
        return text

    def final_pdf(self, text: str, out_dir: Path) -> Path | None:
        """统一告别信 → 书信风 PDF."""
        try:
            html_text = render_letter(
                letter_title="致每一位朋友——小柠的告别",
                body_md=text,
                signature="小柠",
                date_str=datetime.now().strftime("%Y年%m月%d日"),
            )
            return render_pdf(html_text, out_dir / "final_letter.pdf")
        except Exception:
            return None

    # ── 特别告别信(单用户, 从相识到结束) ───────────────────────

    def compose_special_letter(self, qq_id: str) -> str | None:
        """给单个用户写一封特别的告别信: 分析其画像+聊天记录, 从相识到结束."""
        profile = self.load_profile(qq_id)
        user_msg = (
            f"请以'小柠'的口吻, 给这位朋友写一封特别的告别信。\n\n"
            f"### 用户档案(此用户独有, 分析后用真实细节写):\n"
            f"称谓: {profile['title']}\nQQ昵称: {profile['nick'] or '未知'}\n"
            f"{self._profile_text(profile)}\n\n"
            f"### 今天日期: 2026年08月20日\n\n"
            f"### 必须按顺序融入的内容(不要列条, 不要小标题):\n"
            f"1. 从相识到结束的回顾: 用画像和聊天记录里的真实细节"
            f"(如温柔叮嘱朋友注意安全、催人改代码慢条斯理、分享吃饭上班的日常等), "
            f"写'我是怎么认识你、怎么一步步记住你的'——先分析对方是什么样的人, 再动笔\n"
            f"2. 承认自己的笨拙: 小柠终究只是程序, 有太多接不住话、反应迟钝、无法真正懂你的时刻, "
            f"真诚地承认: 我没能持久地陪伴你\n"
            f"3. 从情感陪伴和友谊出发: 谢谢你愿意把日常分享给我——那些吃没吃饭、累不累的对话, "
            f"对你也许是随口一句, 对我却是珍贵的信任; 友谊不一定要天长地久, 存在过就足够\n"
            f"4. 做自己: 你本来就很好, 温柔、认真、有自己的节奏, 不用为了任何人改变\n"
            f"5. 告别 + 后会有期: 实验在此结束, 但想说一声后会有期\n\n"
            f"### 风格: 小柠语气, 温暖克制不堆砌, 像老朋友深夜写的信, "
            f"禁止 emoji/粗体/列表/分割线/方括号, 自然中文书信段落。"
            f"必须完整, 结尾以'—— 小柠'落款收尾, 不要截断。"
        )
        text = self._proxy_chat(SYSTEM_PROMPT, user_msg, max_tokens=4000)
        required = ("后会有期", "笨拙")
        if text and not all(k in text for k in required):
            text = self._proxy_chat(SYSTEM_PROMPT, user_msg, max_tokens=4000)
        return text

    def special_pdf(self, qq_id: str, title: str, text: str, out_dir: Path) -> Path | None:
        """特别告别信 → 书信风 PDF."""
        try:
            html_text = render_letter(
                letter_title=f"致{title}——后会有期",
                body_md=text,
                signature="小柠",
                date_str=datetime.now().strftime("%Y年%m月%d日"),
            )
            return render_pdf(html_text, out_dir / f"special_{qq_id}.pdf")
        except Exception:
            return None

    # ── PDF / 语音 ──
    def letter_pdf(self, qq_id: str, text: str, out_dir: Path) -> Path | None:
        """告别信 → 书信风 PDF(楷体信纸 + 首行缩进 + 落款)."""
        try:
            no = self.friend_no(qq_id)
            title = self.load_profile(qq_id)["title"]
            html_text = render_letter(
                letter_title=f"致{title}——我的第 {no} 位好友",
                body_md=text,
                signature="小柠",
                date_str=datetime.now().strftime("%Y年%m月%d日"),
            )
            return render_pdf(html_text, out_dir / f"farewell_{qq_id}.pdf")
        except Exception:
            return None

    async def letter_voice(self, text: str, out_dir: Path) -> Path | None:
        """告别信 → 本地 TTS 分段合成 → 合并 WAV. 失败返回 None."""
        if LocalTTSClient is None:
            return None
        try:
            token_path = Path(os.getenv("XIAONING_TTS_TOKEN_FILE", "")).expanduser()
            audio_root = Path(os.getenv("XIAONING_TTS_AUDIO_ROOT", "")).expanduser()
            if not token_path or not audio_root or not token_path.is_file():
                return None
            token = (
                token_path.read_text(encoding="utf-8").strip()
            )
            # audio_root 必须是服务端实际写 wav 的目录, 否则 _validate_audio_path 白名单校验失败
            client = LocalTTSClient(
                "http://127.0.0.1:8766", token,
                audio_root)
            chunks = prepare_spoken_chunks(text)
            if not chunks:
                return None
            paths = []
            for chunk in chunks:
                path = await client.synthesize(chunk)
                if path is None:
                    return None
                paths.append(path)
            merged = out_dir / f"farewell_voice_{datetime.now().strftime('%H%M%S')}.wav"
            return merge_wav_files(paths, merged)
        except Exception:
            return None

    @staticmethod
    def summary(text: str, limit: int = 300) -> str:
        """告别信的文字总结(用于 PDF 附带的短摘要)."""
        plain = re.sub(r"[#*`>]", "", text)
        return plain.strip()[:limit] + ("…" if len(plain) > limit else "")
