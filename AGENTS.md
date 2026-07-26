<!-- AGENTS.md — 告诉 Agent 在这个仓库里怎么工作，不复制 README -->

# read-x · Agent 工作规则

## 项目定位

阅读系统：链接自动抓取、内容质量判断、卡片回复、微信公众号长文精读、结构化拆解、飞书文档输出。同时管理微信读书。

README.md 保存项目事实；本文件保存 Agent 执行规则。

## 核心规则：群消息自动分派

**收到任何链接时，必须走 link-card 流程，以卡片 + bot 身份回复。禁止纯文本回复。**

link-card 流程：
1. **抓取**：按链接类型选择抓取方式（微信公众号/即刻/通用网页）
2. **内容质量判断**：统一调 `content-scoring` v3，不分来源；只按脚本返回的 `score_status`、`route` 和 `quality_label` 分派
3. **卡片输出**：所有结果以卡片格式发送，`--as bot`

这是最高优先级规则。不要判断要不要处理、不要用纯文本回复。链接类型只影响抓取方式，不影响分析深度。

## link-card 流程（硬性要求）

按 `.agents/skills/link-card/SKILL.md` 执行，不跳过任何步骤。所有卡片 `--as bot`。

### 内容质量判断（核心）

抓取后，统一调用 `content-scoring` v3（不分来源）。质量阶段输出四维等级与原文主张证据；相关性阶段隔离读取经校验的 YWNext `core-context/full.md`。由 `scripts/content_scoring.py` 算出唯一 `scoring_result`：

- **`score_status != scored`** -> 无数字状态卡
- **`route=long_read`** -> long-read 全流程 -> 卡片
- **`route=card`** -> 按 `quality_label` 生成轻量精读或一句话卡片

`scoring_result` 原样传给 long-read。任何消费者不得复制阈值、重算路由或把相关性混称为质量。完整规则见 `.agents/skills/content-scoring/SKILL.md`。

### `route=long_read` → long-read 全流程

按 `.agents/skills/long-read/SKILL.md` 执行，不跳过任何步骤：

1. **抓取正文**：`wechat-article-to-markdown` skill 直接抓取（最快路径，不要用其他方式）
2. **文体识别**：判断是否专项文体（访谈 Q&A、周刊等），是则走专项规则
3. **独立解码**：Evidence 完成后，`article-decode` 在隔离上下文中完整运行；不输出骨架或单独 X 光四层
4. **文字深度链路**：各 ljg 在相互不可见的隔离上下文中运行；直接消费 content-scoring 的 `ljg_range` 与 `ljg_card`，不得用相关性抬高深度
5. **输出**：主 Agent 只摘取、去重和排版为 Docx XML；生成飞书文档后私聊发一份卡片（群聊发 `senderId`，p2p 发 `chatId`，只发一次）
   - `ljg_card=true` 时，主文档交付成功后再独立运行 `ljg-card`；PNG 不插入文档，以 bot 身份私聊发给触发者（群聊发 `senderId`，p2p 发 `chatId`）

## 关键目录

- `.agents/skills/long-read/` — long-read Skill 定义
- `.agents/skills/article-decode/` — 长文章 X 光解码 Skill（隔离运行）
- `.agents/skills/content-scoring/` - 内容评分引擎（link-card 与 long-read 共用，同一正文只评一次）
- `.agents/skills/link-card/` - link-card Skill 定义（卡片输出 + 链接分派 + 调用 content-scoring）
- `wechat-article-to-markdown` skill — 微信文章抓取（默认唯一路径）
- `scripts/wx_fast.py` — 微信文章抓取（备用，httpx 直连）
- `output/` — 已生成文档
- `outputs/` — 历史输出

## 常用命令

```bash
# 抓取微信文章
wechat-article-to-markdown "<mp.weixin.qq.com URL>"

# 创建飞书文档（long-read 输出用）
lark-cli docs +create --content @.wx_doc.xml --parent-position my_library

# 发卡片（群聊场景私聊发 senderId；p2p 场景发 chatId，只发一次）
# 群聊：
lark-cli im +messages-send --as bot --user-id <bridge_context.senderId> --msg-type interactive --content "$(cat /tmp/link_card.json)" --format json
# p2p：
lark-cli im +messages-send --as bot --chat-id <bridge_context.chatId> --msg-type interactive --content "$(cat /tmp/link_card.json)" --format json
```

## 输出路由（硬性）

以下均为卡片通知字数；主文档长度按 long-read 规则。卡片字数与原文长度 + 内容质量成正比，800 字是极高质量卡片的上限，不是默认目标。

- 高质量 → **必须生成飞书文档 → 私聊发一份卡片（600-800 字摘要通知，核心内容在文档里）**
- 中等质量 → 私聊发一份卡片（400-600 字，与原文长度成正比）
- 低质量 → 私聊发一份卡片（150-300 字，一句话 + 原文链接）
- 所有卡片 `--as bot`，不以 user 身份发送
- 群聊场景私聊发给 `bridge_context.senderId`（触发者本人），不污染群聊；p2p 场景 `chatId` 即私聊会话，只发一次；`senderType=bot`（bot-at-bot）时回退发原群

## 飞书文档段落顺序（硬性）

精读文档结论先行，使用 Docx XML 原生排版，评分不得埋在文末。

- 顶部：两列评分表（`quality_score + quality_label`、`relevance_score + priority_label`；不可用则如实标注）+ 全文唯一 `light-yellow` 核心结论高亮块
- 主文：真正的核心 → 基石/边缘/暗流 → 与作者对话 → 最值得深读之处 → 可选的对飞鱼意义（≤50 字）
- 原文金句 + 原文链接（附录上方）
- 附录：先 200~500 字导言（ljg 完整原稿的摘要），再各文字 ljg 完整原稿（不限字数）
- 文末：必要事实（若有）
- 单段可见文本不超过 100 字；并列信息用列表，真实对比或数据才用不超过 4 列的表格

## 安全边界

- 不读取 `.env`、密钥、token
- 飞书文档创建走当前 bridge profile 的 lark-cli
- 临时文件（`.wx_tmp.md`、`.wx_evidence.json`、`.wx_decode.md`、`.wx_ljg_*.md`、`.wx_doc.xml`、`/tmp/link_card.json`、`/tmp/ljg_cast_*.html`）按实际使用清理

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
- [ ] 中低质量：摘要已生成
- [ ] 卡片 JSON 格式正确（schema 2.0，markdown 标签）
- [ ] 卡片以 `--as bot` 发送
- [ ] 群聊场景：评分卡、精读完成卡、ljg-card PNG 均私聊发给 `senderId`，未发回原群
- [ ] p2p 场景：用 `chatId` 只发一次
- [ ] senderType=bot：回退发原群
