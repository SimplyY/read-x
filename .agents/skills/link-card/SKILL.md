---
name: link-card
description: "群内收到任何链接，自动抓取→内容质量判断→决定深度分析或轻量摘要→以卡片形式回复。卡片用 bot 身份发送。内容质量而非链接类型决定分析深度。"
description_zh: 链接自动抓取、内容质量判断、卡片回复
when_to_use: "群内收到任何 HTTP/HTTPS 链接时自动触发。所有链接类型统一走内容质量判断，不分来源。"
dispatch_intent: "link-card"
---

# link-card: 链接卡片回复

群内收到任何链接，先抓取内容、做内容质量判断，然后决定走深度分析还是轻量摘要，最后用卡片形式发到原群 + 私聊发给用户本人（两边都发）。**所有卡片必须以 bot 身份发送（`--as bot`）**，不要以 user 身份发送。

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
  │    ├─ final_score >=7.0 -> long-read 全流程（传 scoring_result）
  │    ├─ final_score 6.0~6.9 -> 轻量精读
  │    └─ final_score <6.0 -> 一句话卡片
  │
  └─ [2] 卡片输出：所有结果以卡片格式发送，`--as bot`
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

抓取后，不论来源，统一调用 `content-scoring` 评分。模型对六维度输出 `0~10` 等级及证据、上下文加分、风险扣分，由 `scripts/content_scoring.py` 计算出 `scoring_result`（含 `final_score`、`decision`、`route`、`ljg_range`）。**同一正文只评一次**：评分结果传给 long-read，long-read 不得重评。

### 调用 content-scoring

1. 用抓取后的正文 + 元数据，按 `.agents/skills/content-scoring/SKILL.md` 让模型输出 `model_output`（六维度等级+证据、加分、扣分、置信度、结论、1~3 个深挖问题）；
2. 跑 `python3 scripts/content_scoring.py <model_output.json> <source.md>` 拿 `scoring_result`；
3. 据 `route` 分派：`route=card` 自处理，`route=long_read` 把 `scoring_result` 传给 long-read；
4. 只有摘要/片段时 `model_output.provisional=true`，不得冒充完整评分。

### 评分通知（仅 long-read）

`route=long_read`（`final_score >=7.0`）时，long-read 是长任务，评分完成、启动精读前先发一条轻量进度卡片（`--as bot`），让用户立刻知道分数与档位，再进入 long-read 全流程。`route=card` 直接出结果卡片，不单独通知。

进度卡片内容：标题 + `final_score/10 · decision_label` + 一句结论（取 `scoring_result.conclusion`），不展开分析。示例：

```json
{
  "schema": "2.0",
  "header": {"title": {"tag": "plain_text", "content": "评分完成"}, "template": "indigo"},
  "body": {"elements": [{"tag": "markdown", "content": "《标题》\n\n**评分：{final_score}/10 · {decision_label}**\n{scoring_result.conclusion}\n\n正在精读，稍后发文档。"}]}
}
```

### 三档分派（由 final_score 决定）

| final_score | 决策 | 处理 |
|-------------|------|------|
| `<6.0` | 跳过 | 一句话卡片 |
| `6.0~6.9` | 快速阅读 | 中等卡片（轻量精读） |
| `>=7.0` | 选择性/完整/稀缺精读 | 转 long-read，传 scoring_result |

边界裁决：两档之间按 `final_score` 落点，不手动覆盖。不为「安全」把所有中等推给 long-read，那会稀释 long-read 价值。

### 高质量（>=7.0）-> 走 long-read 全流程

转交时把 `scoring_result` 一并传入。long-read 用 `final_score` 决定文字 ljg 数量（`<8.0` 0~1、`8.0~8.4` 1、`8.5~8.9` 1~2、`>=9.0` 2~3），不再重评。`final_score >=8.0` 的 `ljg-card` 在文档交付后独立运行，仅私聊发送 PNG。

### 中等质量（6.0~6.9）-> 走轻量精读

- 有 1-2 个值得记住的观点或金句
- 内容结构简单，不值得完整 `article-decode`
- 信息有用但无意外（行业常识、经验总结、工具推荐）
- 转述/编译类但质量不错

**执行**：抓取 -> 提取核心观点（1-3 条）-> 提取金句（必选，见下方金句规则）-> 卡片输出（群里 + 私聊各发一份，不生成飞书文档）

### 低质量（<6.0）-> 一句话卡片

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

**短摘要 ≤15 行无文字 ljg**：群里 + 私聊各发一份卡片，body 放高密度摘要内容。

**长摘要 / 含 ljg**：生成飞书文档 → 群里 + 私聊各发一份卡片。

> ⚠️ `ljg-card` 不属于文档链路。质量 `>=8.0` 时，先创建并发送主文档卡片，再独立生成 PNG，仅以 bot 身份私聊发送；禁止插入文档或发群。

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
        "content": "《文章标题》\n\n**评分：{final_score}/10 · {decision_label}**\n一句话说明（来自 scoring_result.conclusion）\n\nX 条 ljg 链路：ljg-xxx + ljg-xxx\n\n[阅读全文](飞书文档链接)"
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
        "content": "**作者/来源** · 平台 · 时间\n\n---\n\n核心要点（1-3 条）\n\n---\n\n💬 金句\n> 原文金句 1\n> 原文金句 2"
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
        "content": "**来源**\n\n一句话摘要\n\n[查看原文](链接)"
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

所有卡片两边都发：群里发一份（`--chat-id <bridge_context.chatId>`）+ 私聊发一份（`--user-id <bridge_context.senderId>`）。p2p 场景只发一次（chatId 即私聊会话，避免重复）：

```bash
# 群里发（仅 group 场景）
lark-cli im +messages-send \
  --as bot \
  --chat-id <bridge_context.chatId> \
  --msg-type interactive \
  --content "$(cat /tmp/link_card.json)" \
  --jq '.data.message_id'

# 私聊发
lark-cli im +messages-send \
  --as bot \
  --user-id <bridge_context.senderId> \
  --msg-type interactive \
  --content "$(cat /tmp/link_card.json)" \
  --jq '.data.message_id'
```

**关键约束：**
- `--as bot`：必须，卡片以 bot 身份发送
- `--msg-type interactive`：必须，表示交互卡片
- `--content`：JSON 字符串，直接传入或用文件
- 卡片 JSON 写到临时文件 `/tmp/link_card.json`，发送后清理
- group 两边发，p2p 只发一次
- **JSON 结构化生成（硬约束）**：卡片 JSON 必须用 `python3` 的 `json.dump` 结构化构建后写入 `/tmp/link_card.json`，禁止手拼字符串拼 JSON--手拼括号配对易错（如末尾多/少 `]`），坏 JSON 传给 `--content` 会让整张卡发不出
- **发送前校验（硬约束）**：发送前必须跑 `python3 -c "import json;json.load(open('/tmp/link_card.json'))"` 校验合法性；校验失败则停止、修好 JSON 再发，绝不带病发送
- **发送后确认（硬约束）**：每条 `messages-send` 必须用 `--jq '.data.message_id'` 取回 message_id 确认成功；禁止用 `tail`/截断输出判断是否发出--截断会丢 message_id，误判后重发会产生重复卡片

---

## 卡片内容约束

1. **卡片总字数**：字数与原文长度 + 内容质量成正比，不为凑字数而展开。800 字是极高质量长文的上限，不是默认目标。
   - 高质量（走 long-read + 飞书文档）：卡片作为摘要通知，200-400 字。核心内容在飞书文档里
   - 中等质量：300-500 字，与原文长度成正比。即刻短帖（原文 <500 字）→ 300-400 字；公众号中篇（原文 1000-3000 字）→ 400-500 字
   - 低质量：50-150 字，一句话 + 原文链接
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
| 高质量（long-read + 飞书文档） | 200-400 字 | 卡片是摘要通知，核心内容在飞书文档里。含评分、一句话、ljg 链路引导 |
| 中等质量 | 300-500 字 | 与原文长度成正比。短帖 300-400 字，中篇 400-500 字 |
| 低质量 | 50-150 字 | 一句话摘要 + 原文链接，不需要展开

9. **金句规则（无文档卡片必选）**：不生成飞书文档的卡片（中等质量 + 低质量有可提取内容时），必须从原文提取金句。
   - **密度**：每 2000 字原文提取 1-2 个金句。例如 6000 字原文 → 3-6 个金句
   - **金句定义**：原文中独立成句、有记忆点、脱离上下文仍有力度的表达。不一定是「金句格式」，但必须值得划线
   - **格式**：卡片中以 `> 原文金句` 引用块呈现，每条金句不超过 60 字
   - **低质量例外**：若原文确无值得划线的内容，不强行添加；但不能因为「懒」而跳过

---

## 边界情况处理

| 情况 | 处理 |
|------|------|
| 抓取失败（超时/反爬/404） | 卡片 header 显示「抓取失败」，body 说明原因 + 附原始链接 |
| 内容为空/极短 | 卡片 header 显示标题，body 显示「内容过短，无法提取摘要」+ 原始链接 |
| 超长文章（>10000 字） | 自动归为高质量，走 long-read 全流程 + 飞书文档 |
| 特殊字符/emoji | 正常保留，JSON 正确转义 |
| 群内多发链接 | 每条链接独立处理，各自发一张卡片 |

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
