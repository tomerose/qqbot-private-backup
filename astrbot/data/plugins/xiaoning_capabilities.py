"""Canonical public capability facts; execution stays in the owning plugins."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Capability:
    id: str
    owner: str
    tiers: tuple[str, ...]
    keywords: tuple[str, ...]
    example: str
    guide_token: str
    offer: str
    artifacts: tuple[str, ...] = ()
    proactive: bool = False


CAPABILITIES: tuple[Capability, ...] = (
    Capability("chat", "chat_router", ("ordinary", "x", "pro"), ("聊天", "陪我聊", "倾听"), "陪我聊聊最近有点累", "日常对话", "我在，直接说就行。"),
    Capability("voice", "voice_model_router", ("ordinary", "x", "pro"), ("语音回复", "发语音", "用语音说"), "请用语音回答我", "发语音", "这个我可以直接用语音回；群里已有回复时也有 10% 概率用语音送达。普通用户使用当前会话，X/Pro 还能结合已授权的本人记忆自然接话。"),
    Capability("search", "search_command", ("ordinary", "x", "pro"), ("查资料", "搜索", "最新", "哪里", "路线", "推荐"), "帮我查一下今天的 AI 新闻", "/search", "这个我能查清楚；把范围、地点或筛选条件补一句就行。", proactive=True),
    Capability("research", "search_command", ("x", "pro"), ("深度研究", "比较", "对比", "旅行规划", "行动包"), "帮我比较 A 和 B 并给出决策报告", "/research", "这个我能做成有来源的研究报告并作为 QQ 文件发回来。", (".md",), True),
    Capability("document", "claude_code_agent", ("x", "pro"), ("Word", "PDF", "PPT", "表格", "报告", "文档", "文件"), "帮我做一份暑假计划 Word", "/agent run", "这个我能直接做成目标文件；说清用途和内容，QQ 收到文件才算完成。", (".docx", ".pdf", ".pptx", ".xlsx", ".csv", ".md"), True),
    Capability("draw", "draw_command", ("ordinary", "x", "pro"), ("画图", "作图", "绘图", "海报", "头像", "图片"), "帮我画一只雨夜霓虹下的黑猫", "/draw", "这个我能直接画；把主体、风格和画幅说清楚就行。", (".png", ".jpg", ".webp"), True),
    Capability("image_edit", "draw_command", ("ordinary", "x", "pro"), ("改图", "编辑图片", "重画", "去字幕", "换背景", "调整图片"), "回复图片说：把这张图改成暖色背景", "/edit", "这个我能直接处理；原图和要求可分两条相邻消息发送，QQ 收到新图片才算完成。", (".png",), True),
    Capability("custom_draw", "custom_draw", ("pro",), ("定制图", "人工画", "人工绘制"), "帮我定制一张穿西装的猫油画", "/定制图", "这个会建立可跨对话恢复的人工待办，管理员回图且 QQ 真实转发后才算完成。", (".png", ".jpg", ".webp"), True),
    Capability("video_generate", "video_command", ("x", "pro"), ("生成视频", "AI视频", "Veo", "生成短片", "生成动画"), "帮我生成一段海边日落短片", "/video", "这个我能生成原创 AI 视频文件；说清画面、比例和节奏就行。", (".mp4", ".gif"), True),
    Capability("video_production", "video_agent", ("x", "pro"), ("做视频", "制作视频", "视频Agent", "完整短片"), "帮我做一段如何在家做拿铁的视频", "/做视频", "这个会走脚本、素材、配音和字幕的完整制作链路，QQ 收到视频才算完成。", (".mp4",), True),
    Capability("video_workshop", "video_pipeline", ("x", "pro"), ("高质量视频", "专业短片", "视频工坊"), "帮我做一个高质量的拿铁科普视频", "/视频工坊", "这个会走模板、评分和高清输出链路，QQ 收到视频才算完成。", (".mp4",), True),
    Capability("video_search", "video_command", ("ordinary", "x", "pro"), ("找视频", "视频搜索", "现场视频", "B站视频", "抖音视频"), "帮我找周杰伦现场视频", "/findvideo", "这个我能帮你找公开视频结果，把关键词再具体一点就行。", proactive=True),
    Capability("web", "web_studio", ("x", "pro"), ("做网页", "制作网页", "网站", "网页工坊"), "帮我做一个记账网页", "/web", "这个我能做成可操作网页，并返回预览、HTML 和公开链接。", (".html",), True),
    Capability("music_play", "music_command", ("ordinary", "x", "pro"), ("点歌", "歌曲", "网易云"), "帮我点歌《稻香》", "/music", "这个我能帮你找歌并发送歌曲卡片。", proactive=True),
    Capability("music_generate", "music_command", ("pro",), ("原创歌曲", "写歌", "生成音乐", "唱一首"), "帮我写一首温暖的生日歌", "/sing", "这个我能生成原创音乐并把音频文件发回来。", (".mp3", ".wav", ".m4a"), True),
    Capability("birthday_gift", "friend_core", ("ordinary", "x", "pro"), ("生日礼物", "邮寄礼物", "收礼物"), "/生日礼物 状态", "/生日礼物", "生日当天只能生成待管理员审批的候选；批准后会在私聊征得同意，再安全收集邮寄地址。"),
    Capability("translate", "smart_translate", ("ordinary", "x", "pro"), ("翻译", "英文怎么说", "中文怎么说"), "把这段话翻译成英文", "/tr", "这个我能直接翻译，把原文和目标语言发来就行。", proactive=True),
    Capability("link_summary", "link_summary", ("ordinary", "x", "pro"), ("总结链接", "网页总结", "这篇文章"), "总结一下这个公开链接", "/summary", "这个我能读取公开页面并提炼重点。", proactive=True),
    Capability("github", "github_tools", ("ordinary", "x", "pro"), ("GitHub", "开源项目", "仓库"), "帮我找 GitHub 上优秀的语音项目", "/gh", "这个我能查 GitHub 仓库并整理重点。", proactive=True),
    Capability("debate", "ai_debate", ("x", "pro"), ("圆桌辩论", "AI辩论", "多方观点"), "圆桌讨论 AI 会不会降低人的能力", "/debate", "这个适合让多个角色从不同立场讨论。", proactive=True),
    Capability("interview", "ai_interview", ("x", "pro"), ("模拟面试", "面试练习"), "帮我模拟产品经理面试", "/interview", "这个我能按真实面试连续追问并给反馈。", proactive=True),
    Capability("deep_think", "deep_think", ("x", "pro"), ("仔细分析", "深度思考", "推理"), "仔细分析这个方案的风险", "/think", "这个我能做更深入的分析。", proactive=True),
    Capability("ai_news", "xiaoning_scheduled", ("x", "pro"), ("AI早报", "每日早报", "早报订阅"), "开启 AI 早报", "/早报 开启", "这个可以订阅每天的 AI 早报。", proactive=True),
)


CAPABILITY_BY_ID = {item.id: item for item in CAPABILITIES}


def match_capability(text: object, *, proactive_only: bool = False) -> Capability | None:
    value = str(text or "").lower()
    matches = [
        item
        for item in CAPABILITIES
        if (item.proactive or not proactive_only)
        and any(keyword.lower() in value for keyword in item.keywords)
    ]
    if not matches:
        return None
    return max(
        matches,
        key=lambda item: max(
            len(word) for word in item.keywords if word.lower() in value
        ),
    )


def capability_prompt_block(*, delivery_channel: str = "QQ") -> str:
    lines = ["【可执行能力目录】需求明确时直接交给所有者功能，不要只口头承诺："]
    for item in CAPABILITIES:
        suffix = (
            f"；成品={','.join(item.artifacts)}，必须{delivery_channel}交付"
            if item.artifacts
            else ""
        )
        lines.append(
            f"- {item.id}：{item.owner}；资格={'/'.join(item.tiers)}；"
            f"示例={item.example}{suffix}"
        )
    return "\n".join(lines)
