# 谷歌生态深化清单（2026-08-04 审查）

审查范围：gemini-proxy.py、chat_router、draw_command、Firestore 记忆。本轮只出清单，点单后实施。

## 1. 图像模型名不一致 + 未验证有效性
- **现状**：`draw_command/main.py:77` 定义 `DRAW_MODEL = "gemini-3-pro-image"`（生成路径），但 `:217` 编辑路径硬编码 `"gemini-3.1-flash-image"`；代理端 `IMAGE_MODEL_PRIMARY` 另有来源。三处模型名互不一致。
- **影响**：编辑路径与生成路径模型不同档；若 `gemini-3-pro-image` 不在 Vertex 支持清单，生成直接失败（历史上有过同类事故：ac3fe37 提交"route QQ drawing through supported Vertex model"）。
- **工作量**：S。**优先级**：P0（先验证有效性再点单）。
- **风险**：模型下线/改名导致画图全挂。

## 2. 聊天模型统一到 3.6-flash，路由命名陈旧
- **现状**：代理 `CHAT_MODEL = "gemini-3.6-flash"`，`MODEL_ALIASES` 把 2.5/3.1/3.5 全映射到 3.6。但 `chat_router/main.py:163` 等仍写 `"gemini-2.5-flash"`（靠别名生效）。
- **影响**：行为正确但代码可读性差；新模型升级只改代理不改路由，路由语义失真。
- **工作量**：S。**优先级**：P2。
- **风险**：无。

## 3. 自动深度思考被禁（Vertex 403）
- **现状**：`gemini-proxy.py:476` `use_thinking = explicit_thinking`——自动 deep thinking 关闭，因 Vertex AI 对 high 模式 403；只有显式 `thinking`/`thinking_budget` 参数才走深度思考。3.6 内置 thinking 用默认 medium。
- **影响**：难题（解题/观点题）若路由未显式开 thinking，质量受限于默认档。deep_think 插件走 `/think` 显式参数，普通群聊解题无显式 thinking。
- **工作量**：M（需验证 Vertex 3.6 的 thinking_level 支持矩阵）。**优先级**：P1。
- **风险**：开错档位会 403/额外计费。

## 4. Google Search grounding 已接但覆盖窄
- **现状**：`gemini-proxy.py:451-457` 支持 `google_search` flag → `GoogleSearch` 工具；`:729` 回传 grounding_metadata。消费方：emotional_chat 群关怀 `_gen_group_care`（带真实新消息）、search_command 部分路径。
- **影响**：落地能力已有，但 grounding 结果未结构化利用（如附来源链接回 QQ）。
- **工作量**：M。**优先级**：P1（群关怀回复附来源）。
- **风险**：搜索配额（每日 grounding 调用有 Vertex 配额）。

## 5. Firestore 记忆：单点读写、无配额管理
- **现状**：`astrbot_plugin_xiaoning_memory` + `emotional_chat`（关怀读取 top6）直连 Firestore。无写入频率节流、无失败降级缓存（记忆 memory 里记录过 embedding 配额坑）。
- **影响**：高频聊天时 Firestore 配额/账单风险；单次故障拖垮关怀生成。
- **工作量**：M。**优先级**：P1。
- **风险**：账单与稳定性。

## 6. 安全拦截（finish_reason 感知）已是良好实践
- **现状**：代理对 SAFETY/RECITATION/BLOCKLIST/PROHIBITED_CONTENT 确定性拦截，返回人格化兜底而非裸 502（07-19 已实现）。空响应 3 次退避重试。
- **影响**：已达标，无动作。**优先级**：无。

## 7. Vertex 计费与额度
- **现状**：全部走 Vertex 赠金（用户 07-19 决定群聊私聊统一 Gemini）。无用量仪表盘/预算告警。
- **影响**：赠金耗尽前无预警，服务静默 402。
- **工作量**：S（写个用量脚本查 Vertex 配额 API）。**优先级**：P1。
- **风险**：服务中断。

## 点单建议顺序
P0 #1（先验证图像模型）→ P1 #3/#4/#5/#7 → P2 #2。
