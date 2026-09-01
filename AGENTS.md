<!-- AGENTS.md — 告诉 Agent 在这个仓库里怎么工作，不复制 README -->

# read-x · Agent 工作规则

## 项目定位

阅读系统：链接自动抓取、内容质量判断、卡片回复、微信公众号长文精读、结构化拆解、飞书文档输出。同时管理微信读书。

README.md 保存项目事实；本文件保存 Agent 执行规则。

## Harness 接入

- 只在任务确实需要个性化相关性时读取通过校验的 YWNext `runtime/core-context/full.md`；完整核心上下文不得进入原文忠实度、质量评分、引用或交付状态判断。
- 每次阶段转换以当前 `run_dir`、`summary.json`、哈希和来源状态为准；不以对话记忆或旧产物推断完成。
- 文档创建、卡片发送和 DeepSeek 后处理分别保留尝试与读回证据；提交不确定时停止，禁止自动重发。

## 核心规则：群消息自动分派

**收到任何链接时，必须走 link-card 流程，以卡片 + bot 身份回复。禁止纯文本回复。**

link-card 流程：
1. **抓取**：按链接类型选择抓取方式；微信公众号只调用一次 `scripts/prepare_scoring_run.py <URL>`，内部使用纯 HTTP，不启动或回退浏览器
2. **内容质量判断**：统一调 `content-scoring` v3.18（质量输出仍为 v3.16）；质量模型只读去身份正文与通用三维质量语义，并独立判断 `problem_significance`；独立权威阶段只接收公开 identity_packet，由 Agent 搜索桥最多 3 查询/4 页面，失败不阻断大问题分，脚本固定按 70% 质量 + 30% 重要性计算决策分；只按脚本返回的 `score_status`、`route`、`quality_label` 和 `chatgpt_munger_doc` 分派
3. **卡片输出**：所有结果以卡片格式发送，`--as bot`

这是最高优先级规则。不要判断要不要处理、不要用纯文本回复。链接类型只影响抓取方式，不影响分析深度。显式例外有二：`仅评分 <URL>` 保留真实路由，但发完评分卡后不进入精读；已知专项文体（如阮一峰《科技爱好者周刊》）走快通道，跳过评分直接按专项规则生成卡片（见 link-card SKILL.md [0.5]）。

## link-card 流程（硬性要求）

按 `.agents/skills/link-card/SKILL.md` 执行，不跳过任何步骤。所有卡片 `--as bot`。

### 内容质量判断（核心，v3.18）

权威阶段只接收公开 identity_packet；Agent 搜索最多 3 个查询、4 个页面，失败时保留大问题分并显式标注 inferred/partial。

抓取后，统一调用 `content-scoring` v3.18。每次评分先读取一份运行级 Base 配置快照；快照可用时必须传给 `scripts/content_scoring.py --config-from-base`，不可用时使用本地策略并保留 `policy_source=local`。质量阶段一次判断证据、洞察、迁移三维等级和 `problem_significance`；权威阶段只传公开 identity_packet 给 Agent 搜索桥，最多 3 查询/4 页面，失败不阻断大问题分。由脚本校验并计算唯一决策分；锚点及目标分不得进入评分上下文。先运行脚本，只有返回 `needs_relevance` 时才隔离读取通过校验的 YWNext `runtime/core-context/full.md` 并计算相关性；完整上下文不可用时不读取 `full-full.md` 或其他个人材料，直接回到质量分。由 `scripts/content_scoring.py` 算出唯一 `scoring_result`：

- **`score_status=needs_relevance`** -> 内部补相关性，不发卡、不分派
- **`score_status=needs_full_text|needs_review`** -> 无数字状态卡
- **`route=long_read`** -> long-read 全流程 -> 卡片
- **`route=card`** -> 按 `quality_label` 生成轻量精读或一句话卡片

**三档齐全门（硬性）**：`quality_score ≥ quality_floor`（6.0）的文章，进交付前必须 `relevance_score` 与 `interest_score` 都是实数；任一为 `null`/「待计算」/「不可用」时，禁止发精读完成卡或文档交付卡，先按 content-scoring 相关性隔离阶段补算两轴，三档算完才一起发卡，禁止只带质量分单发。

`scoring_result` 原样传给 long-read。任何消费者不得复制阈值、重算路由、把相关性混称为质量或自行触发模型。完整规则见 `.agents/skills/content-scoring/SKILL.md`。

### `route=long_read` → long-read 全流程

按 `.agents/skills/long-read/SKILL.md` 执行，不跳过任何步骤：

1. **抓取正文**：进入 long-read 后复用 link-card 前置抓取生成的 `source.md`，禁止再次抓取
2. **文体识别**：判断是否专项文体（访谈 Q&A、周刊等），是则走专项规则
3. **独立解码**：Evidence 完成后，通过 `run_isolated_analyses.py` 向 MoonBridge 发出独立 `store=false` HTTP 请求运行 `article-decode`；脚本必须严格校验 Evidence、禁用环境代理并写本轮 summary；不输出骨架或单独 X 光四层
4. **文字深度链路**：各 ljg 由同一脚本并行发出互不可见的独立 HTTP 请求；命令、路径或交付残留必须失败关闭且不落盘；直接消费 content-scoring 的 `ljg_range` 与 `ljg_card`（已按 `decision_score` 含相关+兴趣计算深度档），不得自行用相关性二次抬高深度，不得回退主上下文角色扮演
5. **DeepSeek 芒格后处理**：仅当 `scoring_result.chatgpt_munger_doc=true` 时，在主文档 XML 创建前运行 `.agents/skills/long-read/scripts/run_chatgpt_munger.py`；本地 MoonBridge 固定使用 `deepseek-v4-flash`，返回规范 Markdown、`verification=local-http` 和匹配 hash，再由 `markdown_to_feishu_xml.py` 生成独立芒格洞察 XML。不伪造会话 URL；失败关闭，主精读文档仍照常交付
6. **输出**：主 Agent 只摘取、去重和排版为 Docx XML；成功时创建主文档与芒格洞察文档，并合并为一张私聊交付卡（群聊发 `senderId`，p2p 发 `chatId`，只发一次）；后处理失败时只交付主文档并注明待复核
   - `ljg_card=true` 时，主文档交付成功后再独立运行 `ljg-card`；PNG 不插入文档，以 bot 身份私聊发给触发者（群聊发 `senderId`，p2p 发 `chatId`）

## 关键目录

- `.agents/skills/long-read/` — long-read Skill 定义
- `.agents/skills/article-decode/` — 长文章 X 光解码 Skill（隔离运行）
- `.agents/skills/content-scoring/` - 内容评分引擎（link-card 与 long-read 共用，同一正文只评一次）
- `.agents/skills/link-card/` - link-card Skill 定义（卡片输出 + 链接分派 + 调用 content-scoring）
- `scripts/wx_fast.py` — 微信文章纯 HTTP 抓取（默认唯一路径，不启动浏览器）
- `output/` — 已生成文档
- `outputs/` — 历史输出

## 常用命令

```bash
# 抓取并准备微信文章评分输入
python3 scripts/prepare_scoring_run.py "<mp.weixin.qq.com URL>"

# 非微信来源：在当前 run_dir 生成 Base 配置快照
python3 scripts/fetch_base_config.py --output <run_dir>/base-config.json

# 创建飞书文档（long-read 输出用）
lark-cli docs +create --content @.wx_doc.xml --parent-position my_library

# 发卡片（群聊场景私聊发 senderId；p2p 场景发 chatId，只发一次）
# 群聊：
lark-cli im +messages-send --as bot --user-id <bridge_context.senderId> --msg-type interactive --content "$(cat /tmp/link_card.json)" --format json
# p2p：
lark-cli im +messages-send --as bot --chat-id <bridge_context.chatId> --msg-type interactive --content "$(cat /tmp/link_card.json)" --format json
```

## 输出路由（硬性）

以下均为卡片通知字数；主文档长度按 long-read 规则。卡片字数与原文长度 + 内容质量成正比，800 字是极高质量卡片的上限，不是默认目标。各档位字数区间以 `.agents/skills/link-card/SKILL.md`「各档位字数指导」为权威真值源，此处不再复制数值，避免漂移。

- 高质量 -> **必须生成飞书文档 -> 私聊发一份卡片（摘要通知，核心内容在文档里）**；显式 `仅评分` 除外
- 中等质量 -> 私聊发一份卡片（与原文长度成正比）
- 低质量 -> 私聊发一份卡片（一句话 + 原文链接）
- 所有卡片 `--as bot`，不以 user 身份发送
- 群聊场景私聊发给 `bridge_context.senderId`（触发者本人），不污染群聊；p2p 场景 `chatId` 即私聊会话，只发一次；`senderType=bot`（bot-at-bot）时回退发原群

## 飞书文档段落顺序（硬性）

精读文档结论先行，使用 Docx XML 原生排版，评分不得埋在文末。

- 顶部：评分表（`quality_score + quality_label`、`importance_score`、权威性状态/分数、大问题思考分、`relevance_score + priority_label`、`interest_score + interest_label`、`decision_score`；不可用则如实标注）
- 原文金句 + 原文链接（紧跟评分表，作为溯源入口）
- 全文唯一 `light-yellow` 核心结论高亮块
- 主文：真正的核心 → 基石/边缘/暗流 → 值得研究的相关问题（2–3 个问题及各自一句上下文，总计 ≤300 字）→ 与作者对话 → 最值得深读之处
- 附录：先 ≤100 字导言（只做最精华的一句话概要，从第一性原理平实描述，不堆复杂概念），再各文字 ljg 完整原稿（不限字数）
- 文末：必要事实（若有）
- 单段可见文本不超过 100 字；并列信息用列表，真实对比或数据才用不超过 4 列的表格

## 写作准则（透明玻璃）

所有文字输出（卡片、精读文档、摘要）遵守「透明玻璃」原则：文字是让读者看到内容的窗户，不是装饰。写完回读，若读者注意「这句话写得好」而非「这件事讲清了」，即失败。

- 短句短段：一句一个意思，一段一个观点；长句长段是阅读负担
- 华丽是障碍物：删多余修辞、冗长从句、不必要的强调；朴素、清晰、洗练
- 易读难写：随手写出的文字天然带杂质，写完主动删一遍，删到不能再删
- 传递优先于漂亮：让人记住内容，不让人记住文笔；冷静自持，不炫技

适用本仓库所有产出文字的 skill：link-card 卡片、long-read 精读文档、content-scoring 摘要。完整准则见深 bot CODEX_HOME/AGENTS.md「写作准则（透明玻璃）」。

## 安全边界

- 不读取 `.env`、密钥、token
- 飞书文档创建走当前 bridge profile 的 lark-cli
- 评分临时文件必须放在每次消息独立的 `mktemp -d /tmp/readx-score.XXXXXX` 目录，禁止跨任务共享固定 `/tmp/readx-*` 文件；其他临时文件（`.wx_tmp.md`、`.wx_evidence.json`、`.wx_decode.md`、`.wx_ljg_*.md`、`.wx_doc.xml`、`/tmp/link_card.json`、`/tmp/ljg_cast_*.html`）按实际使用清理

## 禁止事项

- 禁止对任何链接用纯文本回复（必须用卡片）
- 禁止因链接来源（即刻/公众号/网页）而区别对待分析深度
- 禁止卡片以 user 身份发送（必须 `--as bot`）
- 禁止群聊场景把长文阅读卡片（评分卡、精读完成卡、ljg-card PNG）发回原群（必须私聊发给 `senderId`）
- 禁止跳过 long-read 流程中的任何步骤
- 禁止生成飞书文档后不通知触发者
- 禁止对高质量内容走轻量摘要

## 验证方式

收到任何链接后，确认：
- [ ] 正文已成功抓取
- [ ] 内容质量判断已完成（字数、论点、金句、结构、亲历者）
- [ ] 高质量：Evidence / article-decode / 隔离文字 ljg / XML 飞书文档已完成，主文与附录无重复结论
- [ ] `ljg_card=true`：文档已先交付，ljg-card PNG 私聊发给触发者（群聊发 `senderId`，p2p 发 `chatId`）
- [ ] `chatgpt_munger_doc=true`：DeepSeek 后处理成功后创建第二篇芒格洞察文档，与主文档共用一张交付卡；失败关闭且主文档仍交付
- [ ] DeepSeek 输出经 `local-http` 和 hash 验证为规范 Markdown，再创建第二篇文档
- [ ] 交付卡由渲染脚本生成，读回内容没有字面量 `\\n`
- [ ] 中低质量：摘要已生成
- [ ] 卡片 JSON 格式正确（schema 2.0，markdown 标签）
- [ ] 卡片以 `--as bot` 发送
- [ ] 群聊场景：评分卡、精读完成卡、ljg-card PNG 均私聊发给 `senderId`，未发回原群
- [ ] p2p 场景：用 `chatId` 只发一次
- [ ] senderType=bot：回退发原群
