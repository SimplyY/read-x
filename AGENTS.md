<!-- AGENTS.md — 告诉 Agent 在这个仓库里怎么工作，不复制 README -->

# read-x · Agent 工作规则

## 项目定位

阅读系统：链接自动抓取、内容质量判断、卡片回复、微信公众号长文精读、结构化拆解、飞书文档输出。同时管理微信读书。

README.md 保存项目事实；本文件保存 Agent 执行规则。

## 核心规则：群消息自动分派

**收到任何链接时，必须走 link-card 流程，以卡片 + bot 身份回复。禁止纯文本回复。**

link-card 流程：
1. **抓取**：按链接类型选择抓取方式（微信公众号/即刻/通用网页）
2. **内容质量判断**：统一调 `content-scoring` 评分，不分来源。`>=7.0`->long-read 深度分析，`6.0~6.9`->轻量精读，`<6.0`->一句话卡片
3. **卡片输出**：所有结果以卡片格式发送，`--as bot`

这是最高优先级规则。不要判断要不要处理、不要用纯文本回复。链接类型只影响抓取方式，不影响分析深度。

## link-card 流程（硬性要求）

按 `.agents/skills/link-card/SKILL.md` 执行，不跳过任何步骤。所有卡片 `--as bot`。

### 内容质量判断（核心）

抓取后，统一调用 `content-scoring` 评分（不分来源）。模型输出六维度等级+证据，由 `scripts/content_scoring.py` 算出 `scoring_result`，同一正文只评一次：

- **`final_score >=7.0`** -> long-read 全流程 -> 卡片
- **`final_score 6.0~6.9`** -> 轻量精读 -> 卡片
- **`final_score <6.0`** -> 一句话卡片

`scoring_result` 传给 long-read，long-read 不得重评。完整规则见 `.agents/skills/content-scoring/SKILL.md`。

### 高质量 → long-read 全流程

按 `.agents/skills/long-read/SKILL.md` 执行，不跳过任何步骤：

1. **抓取正文**：`wechat-article-to-markdown` skill 直接抓取（最快路径，不要用其他方式）
2. **文体识别**：判断是否专项文体（访谈 Q&A、周刊等），是则走专项规则
3. **独立解码**：Evidence 完成后，`article-decode` 在隔离上下文中完整运行；不输出骨架或单独 X 光四层
4. **文字深度链路**：各 ljg 在相互不可见的隔离上下文中运行；由 content-scoring 的 `final_score` 决定数量（`<8.0` 0~1、`8.0~8.4` 1、`8.5~8.9` 1~2、`≥9.0` 2~3）
5. **输出**：主 Agent 只摘取、去重和排版为 Docx XML；生成飞书文档后群里 + 私聊各发一份卡片
   - 质量 `≥8.0` 时，主文档交付成功后再独立运行 `ljg-card`；PNG 不插入文档、不发群，仅以 bot 身份私聊发送

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

# 发卡片（群里 + 私聊各一份；p2p 只发一次）
# 群里发
lark-cli im +messages-send --as bot --chat-id <bridge_context.chatId> --msg-type interactive --content "$(cat /tmp/link_card.json)" --format json
# 私聊发
lark-cli im +messages-send --as bot --user-id <bridge_context.senderId> --msg-type interactive --content "$(cat /tmp/link_card.json)" --format json
```

## 输出路由（硬性）

以下均为卡片通知字数；主文档长度按 long-read 规则。卡片字数与原文长度 + 内容质量成正比，1600 字是极高质量卡片的上限，不是默认目标。

- 高质量 → **必须生成飞书文档 → 群里 + 私聊各发一份卡片（400-800 字摘要通知，核心内容在文档里）**
- 中等质量 → 群里 + 私聊各发一份卡片（600-1000 字，与原文长度成正比）
- 低质量 → 群里 + 私聊各发一份卡片（100-300 字，一句话 + 原文链接）
- 所有卡片 `--as bot`，不以 user 身份发送
- p2p 场景只发一次（chatId 即私聊会话，避免重复）

## 飞书文档段落顺序（硬性）

精读文档结论先行，使用 Docx XML 原生排版，评分不得埋在文末。

- 顶部：两列评分表（content-scoring 的 `final_score` + `decision_label` + 依据）+ 全文唯一 `light-yellow` 核心结论高亮块
- 主文：真正的核心 → 基石/边缘/暗流 → 与作者对话 → 最值得深读之处 → 可选的对飞鱼意义（≤50 字）
- 附录：先 200~500 字导言，再各文字 ljg 完整原稿（不限字数）
- 文末：Evidence 段（原文金句最多 8 条）+ 原文链接
- 单段可见文本不超过 100 字；并列信息用列表，真实对比或数据才用不超过 4 列的表格

## 安全边界

- 不读取 `.env`、密钥、token
- 飞书文档创建走当前 bridge profile 的 lark-cli
- 临时文件（`.wx_tmp.md`、`.wx_evidence.json`、`.wx_decode.md`、`.wx_ljg_*.md`、`.wx_doc.xml`、`/tmp/link_card.json`、`/tmp/ljg_cast_*.html`）按实际使用清理

## 禁止事项

- 禁止对任何链接用纯文本回复（必须用卡片）
- 禁止因链接来源（即刻/公众号/网页）而区别对待分析深度
- 禁止卡片以 user 身份发送（必须 `--as bot`）
- 禁止跳过 long-read 流程中的任何步骤
- 禁止生成飞书文档后不在群里和私聊通知用户
- 禁止对高质量内容走轻量摘要

## 验证方式

收到任何链接后，确认：
- [ ] 正文已成功抓取
- [ ] 内容质量判断已完成（字数、论点、金句、结构、亲历者）
- [ ] 高质量：Evidence / article-decode / 隔离文字 ljg / XML 飞书文档已完成，主文与附录无重复结论
- [ ] 质量 ≥8.0：文档已先交付，ljg-card PNG 仅私聊发送
- [ ] 中低质量：摘要已生成
- [ ] 卡片 JSON 格式正确（schema 2.0，markdown 标签）
- [ ] 卡片以 `--as bot` 发送
- [ ] 群里 + 私聊卡片已发送
