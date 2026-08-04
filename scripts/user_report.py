"""小柠Bot 用户心理画像与需求分析报告"""
import sqlite3, json, re, sys
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding='utf-8')
db = sqlite3.connect(r'D:\Claudecoda学习\qqbot\astrbot\data\data_v4.db')

dm_users = defaultdict(list)
groups = defaultdict(list)
user_first_dates = {}

prompt_pollution = re.compile(r'【安全[^】]*】|【你是谁】[^！。]*[！。]|【回复风格】[^。]*。|【内部记忆】[^。]*。|【安全铁律】[^。]*。')

for row in db.execute(
    "SELECT user_id, content, created_at FROM conversations "
    "WHERE platform_id='llbot-3806573022' ORDER BY created_at"
):
    uid = str(row[0])
    created = row[1]
    try:
        msgs = json.loads(row[2])
    except Exception:
        continue

    parts = uid.split(':')
    if len(parts) < 3:
        continue
    msg_type = parts[1]
    target = parts[2]

    if target not in user_first_dates:
        user_first_dates[target] = created[:10]

    user_texts = []
    for m in msgs:
        if m.get('role') != 'user':
            continue
        content_list = m.get('content', [])
        if not isinstance(content_list, list):
            continue

        # Take the first non-system text part as the actual user message
        real_msg = ''
        for p in content_list:
            if isinstance(p, dict) and p.get('type') == 'text':
                t = str(p.get('text', ''))
                if '<system' in t or '<Quoted' in t:
                    continue
                # Skip polluted text blocks (old persona prompts)
                t = prompt_pollution.sub('', t).strip()
                if t and len(t) > 1:
                    real_msg = t
                    break

        if real_msg:
            user_texts.append(real_msg[:400])

    if user_texts:
        if msg_type == 'FriendMessage':
            dm_users[target].append((created, user_texts))
        elif msg_type == 'GroupMessage':
            groups[target].append((created, user_texts))

# ── Report ──
print("=" * 70)
print("           小柠Bot · 用户心理画像与需求分析报告")
print(f"           数据: 2026-07-09 ~ 2026-07-18 | 数据库120会话")
print("=" * 70)

# 1. Volume
total_dm = sum(sum(len(s[1]) for s in msgs) for msgs in dm_users.values())
total_grp = sum(sum(len(s[1]) for s in msgs) for msgs in groups.values())
print(f"\n一、规模概览")
print(f"  私聊独立用户: {len(dm_users)} | 消息数: {total_dm}")
print(f"  活跃群聊: {len(groups)} | 消息数: {total_grp}")

# 2. Private chat users
dm_sorted = sorted(dm_users.items(), key=lambda x: -sum(len(s[1]) for s in x[1]))
print(f"\n二、私聊用户画像 ({len(dm_users)}人)\n")

for uid, sessions in dm_sorted:
    all_msgs = [t for s in sessions for t in s[1]]
    total = len(all_msgs)
    first = user_first_dates.get(uid, '?')
    print(f"{'─'*60}")
    print(f"QQ: {uid:20s} | 首次: {first} | 会话: {len(sessions):>2d} | 消息: {total:>3d}")

    # Show recent (skip duplicates)
    seen = set()
    unique = []
    for t in reversed(all_msgs):
        if t[:20] not in seen:
            seen.add(t[:20])
            unique.append(t)
        if len(unique) >= 8:
            break
    for t in reversed(unique):
        print(f"  > {t[:150]}")

    # Intent
    intents = Counter()
    for t in all_msgs:
        tl = t.lower()
        if re.search(r'bug|代码|报错|python|程序|服务器|部署|配置|安装|调试|api|接口|技术|编程|agent|模型|kimi|deepseek|AI', tl):
            intents['技术'] += 1
        if re.search(r'难过|失恋|喜欢|爱人?|孤独|寂寞|空虚|抑郁|焦虑|痛苦|分手|难受|累死了|烦死了|不开心', t):
            intents['情感'] += 1
        if re.search(r'你是谁|你叫什么|你是ai|你是机器人|你是啥|你好|hi|人工智能|小柠', t):
            intents['身份确认'] += 1
        if re.search(r'你能|你会|可以帮我|能不能|可不可以|有什么功能|会什么|恢复了|功能', t):
            intents['功能探索'] += 1
        if re.search(r'好|知道了|行|ok|嗯|没错|是的|对|在吗|睡了|早安|晚安|在干嘛|吃了吗|哈哈', t):
            intents['社交'] += 1
        if re.search(r'为什么|怎么看|你觉得|你怎么看|评价|分析|思考|哲学|意义|人生|是不是|对吗|真的', t):
            intents['思辨'] += 1
        if re.search(r'歌|音乐|周深|邓紫棋|明星|娱乐|游戏|唱|听|玩|电影|视频|画', t):
            intents['娱乐'] += 1
    if intents:
        top = dict(intents.most_common(4))
        print(f"  意图: {top}")

# 3. Core profiles
print(f"\n\n三、核心用户画像\n")
owner_id = '1211000567'
if owner_id in dm_users:
    msgs = [t for s in dm_users[owner_id] for t in s[1]]
    print(f"【A · 拥有者/技术决策者】QQ {owner_id}")
    print(f"  消息: {len(msgs)} | 涵盖技术调研、情绪表达、功能迭代")
    tech = [t for t in msgs if re.search(r'kimi|agent|模型|功能|bug|数据|技术|code|编程|开发', t)]
    emo = [t for t in msgs if re.search(r'难受|累|烦|困|睡不着|压力|焦虑|想', t)]
    print(f"  技术话题: {len(tech)}次, 情感表达: {len(emo)}次")
    print(f"  角色: 既是使用者也是决策者——需要 bot 在能力边界上不断被推着走")

others = [(uid, sum(len(s[1]) for s in sess)) for uid, sess in dm_sorted if uid != owner_id]
for uid, cnt in others[:6]:
    msgs = [t for s in dm_users[uid] for t in s[1]]
    # Classify
    has_tech = any(re.search(r'技术|代码|api|编程|bug|开发|服务器|部署', t) for t in msgs)
    has_emo = any(re.search(r'难过|失恋|累|烦|孤独|焦虑|分手|难受', t) for t in msgs)
    has_identity = any(re.search(r'你是谁|你是|叫什么|ai|机器人|人工智能', t) for t in msgs)
    typ = []
    if has_tech: typ.append('技术向')
    if has_emo: typ.append('情感向')
    if has_identity: typ.append('新人探索')
    if not typ: typ.append('轻度互动')
    print(f"\n【{'/'.join(typ)}】QQ {uid} | {cnt}条消息")
    for t in msgs[-3:]:
        print(f"  > {t[:130]}")

# 4. Groups
print(f"\n\n四、群聊洞察 ({len(groups)}个群)\n")
grp_sorted = sorted(groups.items(), key=lambda x: -sum(len(s[1]) for s in x[1]))
grp_names = {
    '945598390': '雪猪群(Pro群)',
    '1075963106': '活跃群2',
    '820762428': '活跃群3',
    '698271428': '活跃群4',
    '815620109': '活跃群5',
}
for gid, sessions in grp_sorted[:6]:
    all_msgs = [t for s in sessions for t in s[1]]
    total = len(all_msgs)
    name = grp_names.get(gid, '')
    print(f"{gid} {name}: {total}条消息 {len(sessions)}会话")
    # Unique users in this group (approximate)
    for t in all_msgs[-4:]:
        print(f"  > {t[:140]}")
    print()

# 5. Summary
print("五、需求总结与行动建议\n")
print("""
┌─────────────────────────────────────────────────────────────┐
│ 用户结构: 1个高频技术决策者 + 少数量中度用户 + 大量一次性接触  │
│ 核心路径: 身份确认 → 功能探索 → 技术/情感深度互动              │
│ 最大缺口: 一次性用户留存率极低 (117/120 = 97.5% 流失)          │
└─────────────────────────────────────────────────────────────┘

优先级排序:
  1. [留存活客] 新用户首次接触体验——当前"你是谁"类问题占比高但回复可能被老prompt污染
     → 确保首次接触清爽、有性格、不背诵功能列表
  2. [核心用户深耕] Owner是唯一高频用户——继续打磨他关心的能力边界
     → 模型路由、批判力、技术讨论质量
  3. [自动化运营] 粉丝群内容匮乏——需要定期推送偶像新消息
     → /daily 快报、knowledge_seed.json 定期更新
  4. [记忆系统] 当前只有1个用户有记忆数据——覆盖率≈0%
     → 扩大记忆提取覆盖面，让回头客感受到"被记住"
""")

db.close()
