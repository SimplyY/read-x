<!-- AGENTS.md — 告诉 Agent 在这个仓库里怎么工作，不复制 README -->

# read-x · Agent 工作规则

## 项目定位

阅读系统：链接自动抓取、内容质量判断、卡片回复、微信公众号长文精读、结构化拆解、飞书文档输出。同时管理微信读书。

README.md 保存项目事实；本文件保存 Agent 执行规则。

## 核心规则：群消息自动分派

**收到任何链接时，必须走 link-card 流程，以卡片 + bot 身份回复。禁止纯文本回复。**

link-card 流程：
1. **抓取**：按链接类型选择抓取方式（微信公众号/即刻/通用网页）
2. **内容质量判断**：统一标准，不分来源。高质量→long-read 深度分析，中等→轻量精读，低质量→一句话卡片
3. **卡片输出**：所有结果以卡片格式发送，`--as bot`

这是最高优先级规则。不要判断要不要处理、不要用纯文本回复。链接类型只影响抓取方式，不影响分析深度。

## link-card 流程（硬性要求）

按 `.agents/skills/link-card/SKILL.md` 执行，不跳过任何步骤。所有卡片 `--as bot`。

### 内容质量判断（核心）

抓取后，统一按「信号密度」判断（不分来源）。**信号密度 = 洞察量 ÷ 字数**，不设字数硬门槛：

- **高质量**（信号密度高：值得划线 3+ 处 + 有独立判断）→ long-read 全流程 → 卡片
- **中等质量**（信号密度中等：值得划线 1-2 处，或读后判断有微调）→ 轻量精读 → 卡片
- **低质量**（信号密度低：值得划线 0 处 + 无独立判断）→ 一句话卡片

边界裁决：两档之间往上取，但不要为了「安全」把所有中等推给 long-read。

### 高质量 → long-read 全流程

按 `.agents/skills/long-read/SKILL.md` 执行，不跳过任何步骤：

1. **抓取正文**：`wechat-article-to-markdown` skill 直接抓取（最快路径，不要用其他方式）
2. **文体识别**：判断是否专项文体（访谈 Q&A、周刊等），是则走专项规则
3. **三段式精读摘要**：评分 → 一句话 → 骨架 → 值得记住
4. **ljg 深度链路**：根据内容自动选择 1-3 条
5. **输出**：含 ljg 产出时必须生成飞书文档 → 群里 + 私聊各发一份卡片（link-card 格式）
   - ⚠️ **如果 ljg 链路含 ljg-card，必须在 long-read [2] 节完成 PNG 图片生成并上传到飞书云空间，以 Markdown 图片语法嵌入文档**，然后才创建飞书文档。

## 关键目录

- `.agents/skills/long-read/` — long-read Skill 定义
- `.agents/skills/link-card/` — link-card Skill 定义（卡片输出 + 链接分派 + 内容质量判断）
- `wechat-article-to-markdown` skill — 微信文章抓取（默认唯一路径）
- `scripts/wx_fast.py` — 微信文章抓取（备用，httpx 直连）
- `output/` — 已生成文档
- `outputs/` — 历史输出

## 常用命令

```bash
# 抓取微信文章
wechat-article-to-markdown "<mp.weixin.qq.com URL>"

# 创建飞书文档（long-read 输出用）
lark-cli docs +create --title "<标题>" --content @.wx_doc.md --doc-format markdown --parent-position my_library

# 发卡片（群里 + 私聊各一份；p2p 只发一次）
# 群里发
lark-cli im +messages-send --as bot --chat-id <bridge_context.chatId> --msg-type interactive --content "$(cat /tmp/link_card.json)" --format json
# 私聊发
lark-cli im +messages-send --as bot --user-id <bridge_context.senderId> --msg-type interactive --content "$(cat /tmp/link_card.json)" --format json
```

## 输出路由（硬性）

字数与原文长度 + 内容质量成正比。800 字是极高质量长文的上限，不是默认目标。

- 高质量 → **必须生成飞书文档 → 群里 + 私聊各发一份卡片（200-400 字摘要通知，核心内容在文档里）**
- 中等质量 → 群里 + 私聊各发一份卡片（300-500 字，与原文长度成正比）
- 低质量 → 群里 + 私聊各发一份卡片（50-150 字，一句话 + 原文链接）
- 所有卡片 `--as bot`，不以 user 身份发送
- p2p 场景只发一次（chatId 即私聊会话，避免重复）

## 安全边界

- 不读取 `.env`、密钥、token
- 飞书文档创建走当前 bridge profile 的 lark-cli
- 临时文件（`.wx_tmp.md`、`.wx_doc.md`、`/tmp/link_card.json`）用后清理

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
- [ ] 高质量：文体识别/三段式精读/ljg 链路/飞书文档已完成
- [ ] 中低质量：摘要已生成
- [ ] 卡片 JSON 格式正确（schema 2.0，markdown 标签）
- [ ] 卡片以 `--as bot` 发送
- [ ] 群里 + 私聊卡片已发送
