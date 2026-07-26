---
name: link-card
description: "群内收到任何链接，自动抓取→内容质量判断→决定深度分析或轻量摘要→以卡片形式回复。卡片用 bot 身份发送。内容质量而非链接类型决定分析深度。"
description_zh: 链接自动抓取、内容质量判断、卡片回复
when_to_use: "群内收到任何 HTTP/HTTPS 链接时自动触发。所有链接类型统一走内容质量判断，不分来源。"
dispatch_intent: "link-card"
---

# link-card: 链接卡片回复

群内收到任何链接，先抓取内容、做内容质量判断，然后决定走深度分析还是轻量摘要，最后用卡片形式私聊发给触发者本人（群聊场景发 `bridge_context.senderId`；p2p 场景 `chatId` 即私聊会话，只发一次）。**所有卡片必须以 bot 身份发送（`--as bot`）**，不要以 user 身份发送。

## 第一性原理

**为什么用卡片？**

当前流程：链接 → 抓取 → 纯文本 Markdown → 群内回复。问题：
1. 手机上看大段 Markdown 体验差——结构扁平、字体单一、信息密度不均匀
2. 纯文本无法区分「这条消息是 bot 发的」和「群友发的」——身份模糊
3. 每次回复都是新的消息气泡，群聊容易刷屏

卡片的优势：
1. **视觉分层**：header（标题+主题色）、body（核心内容）、底部（元数据/金句），一眼定位
2. **身份清晰**：卡片自带 bot 发送者身份，与群友消息自然区分
3. **信息密度可控**：卡片天然约束了长度，倒逼提炼精华
4. **手机原生化**：飞书卡片在手机上渲染效果远好于 Markdown 块

**四个核心原则：**

1. **bot 身份发送** — 所有卡片用 `--as bot`，不用 user 身份。卡片是 bot 产出的内容，不是用户本人发的
2. **内容质量决定分析深度** — 不因为链接来源不同而区别对待。一篇即刻深度长文值得精读，一篇公众号水文不值得。链接类型只影响「怎么抓取」，不影响「分析多深」
3. **卡片是展示层，不是分析层** — 卡片只负责格式化输出，不替代 long-read 的深度分析
4. **抓取方式由链接类型决定** — 微信公众号用 `wechat-article-to-markdown`，即刻用 curl + `__NEXT_DATA__`，通用网页用 `read` skill。抓取方式不同，但抓取后的内容走同一套质量判断

---

## 入口约束：一次只处理一个内容

bridge 合并送达多条消息时（`user_input` 多段标注、`quoted_messages` 引用旧内容），**只处理最新一条用户指令**，其余只作上下文：

- 已响应过的旧消息不重复处理
- bot 进度卡、流式展示卡是过程产物，不是新指令
- 引用旧消息只作上下文，不重新执行其内容
- 需处理多个独立内容时，分次发送，一次一个

一次专注一件事，避免并行处理导致混乱、重复劳动和刷屏。

## 核心流程

```
群内收到链接
  │
  ├─ [0] 抓取：按链接类型选择抓取方式
  │    ├─ mp.weixin.qq.com → wechat-article-to-markdown
  │    ├─ m.okjike.com → curl + __NEXT_DATA__ 解析
  │    └─ 其他 URL → read skill
  │
  ├─ [1] 调 content-scoring 评分（统一标准，不分来源）
  │    ├─ score_status 非 scored -> 无数字状态卡
  │    ├─ route=long_read -> long-read 全流程（传 scoring_result）
  │    └─ route=card -> 按 quality_label 生成轻量或一句话卡片
  │
  └─ [2] 卡片输出：所有结果以卡片格式发送，`--as bot`；群聊私聊发 `senderId`，p2p 发 `chatId`
```

## [0] 抓取：按链接类型选择抓取方式

| 链接类型 | 抓取方式 |
|---------|---------|
| `mp.weixin.qq.com` | `wechat-article-to-markdown` skill（最快路径，不要用其他方式） |
| `m.okjike.com` | curl 模拟移动端 → 提取 `__NEXT_DATA__` JSON → 解析 `props.pageProps.post` |
| 其他 URL | `read` skill |

即刻抓取命令：
```bash
curl -s -L -H "User-Agent: Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)" \
  "<url>" | python3 -c "
import sys, json, re
html = sys.stdin.read()
m = re.search(r'<script id=\"__NEXT_DATA__\"[^>]*>(.*?)</script>', html, re.DOTALL)
if m:
    d = json.loads(m.group(1))
    post = d['props']['pageProps']['post']
    print(json.dumps({'content':post.get('content',''),'author':post.get('author',''),'time':post.get('createdAt',''),'title':post.get('title',''),'likeCount':post.get('likeCount',0)}, ensure_ascii=False))
else:
    print('{"error":"NEXT_DATA not found"}')
"
```

## [1] 内容质量判断（统一标准）

抓取后，不论来源，统一调用 `content-scoring` v3。质量阶段只读正文和七篇锚点；相关性阶段在独立上下文中只读主张清单与经校验的 YWNext `core-context/full.md`。`scripts/content_scoring.py` 统一计算 `quality_score`、`relevance_score`、`decision_score`、`route` 和 `ljg_range`。质量结果传给 long-read，long-read 不得重评。

### 调用 content-scoring

1. 判断正文是否完整；片段或未知正文输出 `source_status=partial|unknown`，不得补造维度。
2. 在质量隔离上下文按 content-scoring Skill 生成 `quality_output v3`；需要重评时使用 fresh context，不能泄漏第一次结论。
3. 运行 YWNext 8 天校验；通过后在独立上下文生成 `relevance_output v1`，失败则不传相关性。
4. 跑 `python3 scripts/content_scoring.py <quality_output.json> <source.md> [相关性参数]` 拿 `scoring_result v3`。
5. 只据 `score_status` 和 `route` 分派；`route=long_read` 时把整个结果传给 long-read。

### 评分卡（所有路由必发）

评分完成后、进入任何深度处理前，**必须先发一张评分卡**（`--as bot`）。这是评分流程的固定产物：

- `score_status=needs_full_text|needs_review`：状态卡即最终卡；说明需要完整正文或人工复核，不显示任何数字。
- `route=card`：评分卡即最终卡。评分块作为卡片头部，下方接对应摘要/金句/链接，不再单独发结果卡。
- `route=long_read`：评分卡作为进度卡，告知"正在精读，稍后发文档"，long-read 完成后再发交付卡。

正式评分卡显示质量分、相关性分（不可用则写“不可用”）、决策分、质量档位、四维等级和一句结论。不得展示 YWNext 私有原文。示例：

```json
{
  "schema": "2.0",
  "header": {"title": {"tag": "plain_text", "content": "评分完成"}, "template": "indigo"},
  "body": {"elements": [{"tag": "markdown", "content": "《标题》\n\n**质量 {quality_score}/10 · {quality_label}**\n相关性 {relevance_score/10 或 不可用} · 决策 {decision_score}/10\n**四维**\n证据与论证 {quality_dimensions.evidence_quality.grade} · 洞察解释 {quality_dimensions.insight_explanatory.grade}\n长期迁移 {quality_dimensions.transfer_durability.grade} · 信息效率 {quality_dimensions.information_efficiency.grade}\n{scoring_result.conclusion}\n\n正在精读，稍后发文档。"}]}
}
```

### 分派

数值规则只存在于 content-scoring policy 与脚本。link-card 不重算、不手动覆盖：

- `route=long_read`：转 long-read，传 `scoring_result`。
- `route=card` 且 `quality_label=快速阅读`：轻量精读卡。
- 其他 scored card：一句话卡片。
- 非 scored：无数字状态卡。

### route=long_read -> 走 long-read 全流程

转交时把 `scoring_result` 一并传入。long-read 直接消费 `ljg_range` 与 `ljg_card`，不再重评或按相关性抬高深度。`ljg_card=true` 时在文档交付后独立运行，私聊发 PNG（群聊发 `senderId`，p2p 发 `chatId`）。

### quality_label=快速阅读 -> 走轻量精读

- 有 1-2 个值得记住的观点或金句
- 内容结构简单，不值得完整 `article-decode`
- 信息有用但无意外（行业常识、经验总结、工具推荐）
- 转述/编译类但质量不错

**执行**：抓取 -> 提取核心观点（1-3 条）-> 提取金句（必选，见下方金句规则）-> 卡片输出（私聊发，群聊发 `senderId`，p2p 发 `chatId`，不生成飞书文档）

### 其他 scored card -> 一句话卡片

- 无独立观点（纯转述、通稿、PR 稿、AI 生成感强）
- 读完没有值得划线的地方
- 纯吐槽、情绪输出、短评、转发无附加内容
- 纯链接无正文（抓取失败但能拿到标题）

**执行**：卡片 header 显示标题，body 一句话摘要 + 原文链接。如原文确有值得提取的金句，保留 1 句；确无则不强行添加。

评分失败时回退为一句话卡片 + 原文链接，不阻塞交付。

---

## [2] 卡片模板

所有卡片使用 CardKit 2.0 schema（`schema: "2.0"`），header 用 `indigo` 模板。

### 高质量内容卡片（long-read 完成后）

**短摘要 ≤15 行无文字 ljg**：私聊发一份卡片（群聊发 `senderId`，p2p 发 `chatId`），body 放高密度摘要内容。

**长摘要 / 含 ljg**：生成飞书文档 → 私聊发一份卡片（群聊发 `senderId`，p2p 发 `chatId`）。

> ⚠️ `ljg-card` 不属于文档链路。`scoring_result.ljg_card=true` 时，先创建并发送主文档卡片，再独立生成 PNG，以 bot 身份私聊发给触发者（群聊发 `senderId`，p2p 发 `chatId`）；禁止插入文档。

> ⚠️ 精读完成卡是「交付」卡，不重复评分与四维--评分细节只在评分卡出现一次。这里只留档位一句话 + 核心结论 + 暗流 + ljg 链路 + 文档链接。

```json
{
  "schema": "2.0",
  "header": {
    "title": {"tag": "plain_text", "content": "精读完成"},
    "template": "indigo"
  },
  "body": {
    "elements": [
      {
        "tag": "markdown",
        "content": "《文章标题》\n\n**{quality_label}**（质量 {quality_score}/10，完整评分详见评分卡）\n\n核心结论：精读得到的最关键判断（取主文核心高亮或 scoring_result.conclusion）\n\n暗流/最值得深读之处：一句话点出\n\nX 条 ljg 链路：ljg-xxx + ljg-xxx\n\n[阅读全文](飞书文档链接)"
      }
    ]
  }
}
```

### 中等质量内容卡片

```json
{
  "schema": "2.0",
  "header": {
    "title": {"tag": "plain_text", "content": "标题（截断到 40 字）"},
    "template": "indigo"
  },
  "body": {
    "elements": [
      {
        "tag": "markdown",
        "content": "**作者/来源** · 平台 · 时间\n\n---\n\n**质量 {quality_score}/10 · {quality_label}**\n相关性 {relevance_score/10 或 不可用} · 决策 {decision_score}/10\n**四维**\n证据与论证 {quality_dimensions.evidence_quality.grade} · 洞察解释 {quality_dimensions.insight_explanatory.grade}\n长期迁移 {quality_dimensions.transfer_durability.grade} · 信息效率 {quality_dimensions.information_efficiency.grade}\n{scoring_result.conclusion}\n\n---\n\n核心要点（1-3 条）\n\n---\n\n💬 金句\n> 原文金句 1\n> 原文金句 2"
      }
    ]
  }
}
```

### 低质量内容卡片

```json
{
  "schema": "2.0",
  "header": {
    "title": {"tag": "plain_text", "content": "标题（截断到 40 字）"},
    "template": "indigo"
  },
  "body": {
    "elements": [
      {
        "tag": "markdown",
        "content": "**来源**\n\n**质量 {quality_score}/10 · {quality_label}**\n相关性 {relevance_score/10 或 不可用} · 决策 {decision_score}/10\n**四维**\n证据与论证 {quality_dimensions.evidence_quality.grade} · 洞察解释 {quality_dimensions.insight_explanatory.grade}\n长期迁移 {quality_dimensions.transfer_durability.grade} · 信息效率 {quality_dimensions.information_efficiency.grade}\n{scoring_result.conclusion}\n\n---\n\n一句话摘要\n\n[查看原文](链接)"
      }
    ]
  }
}
```

### 抓取失败卡片

```json
{
  "schema": "2.0",
  "header": {
    "title": {"tag": "plain_text", "content": "抓取失败"},
    "template": "indigo"
  },
  "body": {
    "elements": [
      {
        "tag": "markdown",
        "content": "无法抓取内容\n\n原因：超时/反爬/404\n\n[查看原文](链接)"
      }
    ]
  }
}
```

---

## 发送卡片命令

群聊场景：所有卡片（评分卡、中低质量结果卡、精读完成卡、ljg-card PNG）私聊发给触发者本人（`--user-id <bridge_context.senderId>`），不污染群聊。p2p 场景：`chatId` 即私聊会话，用 `--chat-id` 只发一次，不要再用 `--user-id` 重复发送。

发送目标裁决：
- `chatType=group` 且 `senderType=user`：`--user-id <bridge_context.senderId>` 私聊发给触发者
- `chatType=p2p`：`--chat-id <bridge_context.chatId>`（即私聊会话，只发一次）
- `senderType=bot`（bot-at-bot 触发）：私聊给 bot 无意义，回退 `--chat-id <bridge_context.chatId>` 发原群

```bash
# 群聊场景：私聊发给触发者本人
lark-cli im +messages-send \
  --as bot \
  --user-id <bridge_context.senderId> \
  --msg-type interactive \
  --content "$(cat /tmp/link_card.json)" \
  --jq '.data.message_id'

# p2p 场景：chatId 即私聊会话，只发一次
lark-cli im +messages-send \
  --as bot \
  --chat-id <bridge_context.chatId> \
  --msg-type interactive \
  --content "$(cat /tmp/link_card.json)" \
  --jq '.data.message_id'
```

> ⚠️ 群聊场景禁止把长文阅读卡片（评分卡、精读完成卡、ljg-card PNG）发回原群；统一私聊发给 `bridge_context.senderId`。p2p 场景用 `chatId`，不要再用 `--user-id` 重复发送。

**关键约束：**
- `--as bot`：必须，卡片以 bot 身份发送
- `--msg-type interactive`：必须，表示交互卡片
- `--content`：JSON 字符串，直接传入或用文件
- 卡片 JSON 写到临时文件 `/tmp/link_card.json`，发送后清理
- 所有卡片（含 ljg-card PNG）群聊私聊发 `senderId`，p2p 发 `chatId`，只发一次
- **JSON 结构化生成（硬约束）**：卡片 JSON 必须用 `python3` 的 `json.dump` 结构化构建后写入 `/tmp/link_card.json`，禁止手拼字符串拼 JSON--手拼括号配对易错（如末尾多/少 `]`），坏 JSON 传给 `--content` 会让整张卡发不出
- **发送前校验（硬约束）**：发送前必须跑 `python3 -c "import json;json.load(open('/tmp/link_card.json'))"` 校验合法性；校验失败则停止、修好 JSON 再发，绝不带病发送
- **发送后确认（硬约束）**：每条 `messages-send` 必须用 `--jq '.data.message_id'` 取回 message_id 确认成功；禁止用 `tail`/截断输出判断是否发出--截断会丢 message_id，误判后重发会产生重复卡片

---

## 卡片内容约束

1. **卡片总字数**：字数与原文长度 + 内容质量成正比，不为凑字数而展开。800 字是极高质量长文的上限，不是默认目标。
   - 高质量（走 long-read + 飞书文档）：卡片作为摘要通知，200-400 字。核心内容在飞书文档里
   - 中等质量：300-500 字，与原文长度成正比。即刻短帖（原文 <500 字）→ 300-400 字；公众号中篇（原文 1000-3000 字）→ 400-500 字
   - 低质量：50-150 字（评分块不计入），一句话摘要 + 原文链接
2. **标题截断**：header title 最多 40 字（中文字符），超出用 `...` 截断
3. **逻辑自洽优先**：卡片内容必须能独立构成完整论述。读者只看卡片不看原文，也能理解核心论点和推理链条。不能因为压缩而丢失因果逻辑
4. **分段控制**：body 的 markdown 中，单段 100-200 字为宜；超过 200 字自然拆段。不要用「一句话一段」的碎片化写法
5. **禁止的标签**：CardKit 2.0 不支持 `note` 标签，只能用 `markdown` 标签
6. **特殊字符转义**：JSON 中的 markdown 内容必须正确转义（双引号、反斜杠、换行符）
7. **链接保留**：markdown 中的链接保持可点击格式

8. **字数反比例原则**：原文越长 → 卡片可相对长（但不超过 800）；原文越短 → 卡片必须短。不要对 300 字原文写 600 字摘要
### 各档位字数指导

| 质量档位 | 推荐字数 | 说明 |
|---------|---------|------|
| 高质量（long-read + 飞书文档） | 200-400 字 | 卡片是摘要通知，核心内容在飞书文档里。不含详细评分（已在评分卡），聚焦核心结论、暗流、ljg 链路引导 |
| 中等质量 | 300-500 字 | 与原文长度成正比。短帖 300-400 字，中篇 400-500 字 |
| 低质量 | 50-150 字 | 评分块（不计入字数）+ 一句话摘要 + 原文链接

9. **金句规则（无文档卡片必选）**：不生成飞书文档的卡片（中等质量 + 低质量有可提取内容时），必须从原文提取金句。
   - **密度**：每 2000 字原文提取 1-2 个金句。例如 6000 字原文 → 3-6 个金句
   - **金句定义**：原文中独立成句、有记忆点、脱离上下文仍有力度的表达。不一定是「金句格式」，但必须值得划线
   - **格式**：卡片中以 `> 原文金句` 引用块呈现，每条金句不超过 60 字
   - **低质量例外**：若原文确无值得划线的内容，不强行添加；但不能因为「懒」而跳过
10. **评分与四维展示**：正式评分只在评分卡出现一次，展示质量、相关性、决策分和四维语义等级。`needs_full_text`、`needs_review` 不显示数字。精读完成卡只留质量档位一句话并注明“完整评分详见评分卡”，不重复四维。

---

## 边界情况处理

| 情况 | 处理 |
|------|------|
| 抓取失败（超时/反爬/404） | 卡片 header 显示「抓取失败」，body 说明原因 + 附原始链接 |
| 内容为空/极短 | 卡片 header 显示标题，body 显示「内容过短，无法提取摘要」+ 原始链接 |
| 超长文章（>10000 字） | 仍走同一评分；长度不替代质量 |
| 特殊字符/emoji | 正常保留，JSON 正确转义 |
| 群内多发链接 | 每条链接独立处理，各自私聊发一张卡片 |

---

## 与 long-read 的关系

| 维度 | long-read | link-card |
|------|-----------|-----------|
| 职责 | Evidence、隔离解码、独立深度 Skill 与 XML 文档拼接 | 展示输出（卡片格式化 + 发送） |
| 触发条件 | 由 link-card 的内容质量判断决定 | 所有链接的入口和出口 |
| 关系 | 被 link-card 调度 | 调度 long-read（高质量内容）+ 自处理（中低质量） |

**link-card 是入口，long-read 是深度引擎**：link-card 负责抓取、质量判断、卡片输出。高质量内容交给 long-read 做深度分析，分析结果再回到 link-card 做卡片输出。中低质量内容 link-card 自己处理。

---

## 自测清单

每次修改 SKILL.md 后，用以下用例自测：

- [ ] 即刻深度长文（≥800字，有论点）→ 正确识别为高质量 → 走 long-read
- [ ] 即刻短文（<300字，无观点）→ 正确识别为低质量 → 一句话卡片
- [ ] 微信公众号长文 → 正确识别为高质量 → 走 long-read
- [ ] 微信公众号水文 → 正确识别为低质量 → 一句话卡片
- [ ] 通用网页（有深度）→ 正确识别为高质量 → 走 long-read
- [ ] 抓取失败 → 卡片显示失败信息
- [ ] 特殊字符（双引号、换行、emoji）→ JSON 正确转义
- [ ] 标题超 40 字 → 正确截断
- [ ] 所有卡片 `--as bot`，不出现 user 身份
- [ ] 评分卡已发（所有路由必发，非 scored 状态没有伪数字）
- [ ] 群聊场景：评分卡、精读完成卡、ljg-card PNG 均私聊发给 `senderId`，未发回原群
- [ ] p2p 场景：用 `chatId` 只发一次，未用 `--user-id` 重复发送
- [ ] senderType=bot（bot-at-bot）：回退发原群
