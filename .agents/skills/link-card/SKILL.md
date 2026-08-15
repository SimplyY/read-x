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
2. **内容质量决定分析深度** — 不因为链接来源不同而区别对待。一篇即刻深度长文值得精读，一篇公众号水文不值得。链接类型只影响「怎么抓取」，不影响「分析多深」。**已知专项文体例外**：阮一峰《科技爱好者周刊》等固定栏目跳过评分，直接走专项解析（见 [0.5] 快通道）——评分是为未知内容设计的路由决策器，已知栏目的质量和处理方式都已确定
3. **卡片是展示层，不是分析层** — 卡片只负责格式化输出，不替代 long-read 的深度分析
4. **抓取方式由链接类型决定** — 微信公众号评分只调用 `prepare_scoring_run.py`（内部使用 `wx_fast.py` 纯 HTTP 抓取，不启动浏览器），即刻用 curl + `__NEXT_DATA__`，通用网页用 `read` skill。抓取方式不同，但抓取后的内容走同一套质量判断

---

## 入口约束：一次只处理一个内容

bridge 合并送达多条消息时（`user_input` 多段标注、`quoted_messages` 引用旧内容），**只处理最新一条用户指令**，其余只作上下文：

- 已响应过的旧消息不重复处理
- bot 进度卡、流式展示卡是过程产物，不是新指令
- 引用旧消息只作上下文，不重新执行其内容
- 需处理多个独立内容时，分次发送，一次一个

一次专注一件事，避免并行处理导致混乱、重复劳动和刷屏。

### 仅评分入口

最新一条用户指令精确为 `仅评分 <URL>` 时，设本次 `score_only=true`。仍执行真实抓取、质量评分、条件相关性和评分卡，并保留脚本计算的真实 `route`；评分卡发送后立即结束，即使 `route=long_read` 也不进入精读。卡片必须注明“本次仅评分，不进入精读”。普通链接没有这个例外。

## 核心流程

```
群内收到链接
  │
  ├─ [0] 抓取：按链接类型选择抓取方式
  │    ├─ mp.weixin.qq.com → prepare_scoring_run.py（内部只抓取一次）
  │    ├─ m.okjike.com → curl + __NEXT_DATA__ 解析
  │    └─ 其他 URL → read skill
  │
  ├─ [0.5] 专项文体快通道：抓取后基于标题/作者/结构识别已知专项文体
  │    ├─ 命中阮一峰《科技爱好者周刊》-> 跳过评分，直接按 genre-rules 周刊专项生成卡片
  │    └─ 未命中 -> 正常走 [1] 评分
  │
  ├─ [1] 调 content-scoring 评分（统一标准，不分来源）
  │    ├─ needs_relevance -> 内部补相关性，不发卡、不分派
  │    ├─ needs_full_text|needs_review -> 无数字状态卡
  │    ├─ route=long_read -> long-read 全流程（传 scoring_result）
  │    └─ route=card -> 按 quality_label 生成轻量或一句话卡片
  │
  └─ [2] 卡片输出：所有结果以卡片格式发送，`--as bot`；群聊私聊发 `senderId`，p2p 发 `chatId`
```

## [0] 抓取：按链接类型选择抓取方式

微信公众号只运行一次 `python3 /Users/yuwei/code/read-x/scripts/prepare_scoring_run.py <URL>`。它调用 `wx_fast.py` 进行纯 HTTP 抓取，不启动或回退任何浏览器；确定性创建独立 `run_dir`、解析唯一保存路径、复制并核对 `source.md`、生成匿名正文，最终只输出含绝对路径和文章元数据的 JSON。HTTP 失败即失败关闭。禁止在模型中自行 `mktemp`、解析 `fetch.log`、重建标题路径、扫描 output、调用 `grep -P` 或重复抓取。

非微信来源才先执行一次 `mktemp -d /tmp/readx-score.XXXXXX`，抓取后调用 `prepare_anchor_view.py --blind-only --article-source <source.md> --blind-output <blind-source.md>`。本次所有产物写入该目录；禁止固定共享路径或写入仓库。过程消息只能在 `source.md` 已存在并核对 URL 后发送。

| 链接类型 | 抓取方式 |
|---------|---------|
| `mp.weixin.qq.com` | `prepare_scoring_run.py`（内部使用 `wx_fast.py` 纯 HTTP，只抓取一次） |
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

## [0.5] 专项文体快通道

抓取完成后、调用 content-scoring 之前，先做一次专项文体识别。content-scoring 是为「质量未知、处理方式未知」的内容设计的路由决策器；阮一峰《科技爱好者周刊》这类固定栏目，质量稳定、处理方式已由 `long-read/references/genre-rules.md` 第1节写死（板块化解析、卡片输出、不走深度链路、不生成飞书文档），评分结果不参与任何决策。对已知栏目评分是空转，更会导致它被 `route=card` 挡在 long-read 门外、专项规则永远不触发。

### 识别标志

满足任一即命中阮一峰《科技爱好者周刊》（见 genre-rules.md 第1节）：

- 标题含「科技爱好者周刊」或「阮一峰」
- 作者为「阮一峰的网络日志」
- 正文结构含「科技动态」「文摘」「文章」「工具」「资源」「一句话新闻」等固定小标题

`prepare_scoring_run.py` 输出的 JSON 已含 `title`/`author`，标题和作者两项在评分前即可确定性判断；结构标志需要读 `source.md` 确认。

李继刚《人生周报》同样在快通道识别（见 genre-rules.md 第5节）：标题含「人生周报」且作者为「李继刚」，或标题含「人生周报」且正文含「## 读」「## 说」「## 书」固定小标题。

### 命中后的处理

1. **跳过 content-scoring**：不调 `generate_quality.py`、不调 `content_scoring.py`、不发「正文抓取完成，开始评分」过程消息、不发评分卡。
2. **直接按 genre-rules 周刊专项生成卡片**：读 `source.md`，按周刊板块解析。USER.md 仅用于筛选层决定保留/去掉哪些板块，正文不逐条贴用户画像标签（见 genre-rules.md 第1节「相关性约束」）。
3. **卡片输出**：`--as bot`，群聊私聊发 `senderId`，p2p 发 `chatId`，只发一次；不生成飞书文档。
4. **卡片不显示评分**：专项快通道不发评分卡，卡片 header 用「📖 《周刊标题》(第X期)」，body 直接是板块化解析内容 + 原文链接。

### 命中李继刚《人生周报》后的处理

1. **跳过 content-scoring**：同阮一峰周刊，固定栏目不评分。
2. **按 genre-rules.md 第5节筛选解析**：读 `source.md`，按用户画像从读/说/书中挑相关句子，按主题分组重组，入选 ≤ 正文 50%，不生成飞书文档。
3. **卡片输出**：`--as bot`，群聊私聊发 `senderId`，p2p 发 `chatId`，只发一次；header 用「📖 《人生周报vXXX：主题》」，body 分组句子 + 原文链接，不显示评分。

### 未命中

正常走 [1] content-scoring 评分，流程不变。

### 仅评分入口与快通道

`仅评分 <URL>` 对专项文体无效：若用户对阮一峰周刊发「仅评分」，仍走快通道（跳过评分、直接专项卡片），因为该文体没有评分环节可「仅」执行。如确需对周刊评分，用户需明确说明。

## [1] 内容质量判断（统一标准）

抓取后，不论来源，统一调用 `content-scoring` v3.15。质量阶段只读去身份正文和通用三维数值语义；七篇锚点及目标分只用于评分后的外部闭卷回归，禁止进入评分上下文。脚本应用硬门并计算总分。只有脚本返回 `needs_relevance` 后，相关性阶段才在独立上下文中读主张清单与经校验的 YWNext `core-context/full.md`。`scripts/content_scoring.py` 统一计算最终路由和深度。质量结果传给 long-read，long-read 不得重评。

### 调用 content-scoring

1. 判断正文是否完整；片段或未知正文输出 `source_status=partial|unknown`，不得补造维度。抓取完成后的**下一次模型响应只发并行工具调用，不写解释**：一边用 `lark-cli im +messages-send --as bot --user-id <senderId> --msg-type text --text "正文抓取完成，开始评分｜<文章标题>" --format json` 私聊发送过程消息（p2p 改用 `--chat-id <chatId>`），一边执行第 2 步的闭卷质量命令。过程消息成功是质量结果生效的硬门；发送失败则丢弃模型结果并 fail closed。不得只把它写入 COT/过程卡；飞书创建时间是评分主指标起点，标题用于并行任务配对。
2. 同时运行 `python3 /Users/yuwei/code/read-x/scripts/generate_quality.py <blind_source_parts...> --output <run_dir>/quality-output.json`。它通过既有本地 MoonBridge 直接调用相同 `glm-5.2`，不传推理覆盖；输入只有匿名正文与质量契约。主 Agent 禁止读取匿名正文和质量契约。命令失败、超时或未生成文件时失败关闭，禁止回退主上下文、启动子 Agent 或嵌套 `codex exec`。
3. 质量命令与过程消息均成功后，直接运行 `python3 scripts/content_scoring.py <quality_output.json> <source.md> --output <run_dir>/scoring-result.json`，拿第一个 `scoring_result v3.15`。
4. 若 `score_status=needs_relevance`，才读取 YWNext `full.md`：结构齐全则在独立上下文生成 `relevance_output v3` 并再跑脚本；缺失或结构损坏则使用 `--relevance-unavailable` 确定性结束。过期只降权。相关性无效或 low 时接受脚本的失败关闭结果，不重试阻塞。
5. `needs_relevance` 不得发卡、不得传 long-read。只对 `scored`、`needs_full_text`、`needs_review` 生成用户卡片；`scored` 只据 `route` 分派。

第 3 步不是“先评分、下轮再发卡”。质量 JSON 之后必须在同一次 `exec_command` 内按下列固定尾部执行；只替换路径、卡片元数据和发送目标，不改分支：

```bash
python3 scripts/content_scoring.py <quality_output.json> <source.md> --output <scoring-result.json>
score_status_value=$(jq -r '.score_status' <scoring-result.json>)
if [ "$score_status_value" = needs_relevance ]; then
  cat <scoring-result.json>
else
  python3 scripts/render_score_card.py <scoring-result.json> --title <title> --author <author> --date <date> --url <url> [--score-only] --output <score-card.json>
  lark-cli im +messages-send --as bot <target> --msg-type interactive --content "$(cat <score-card.json>)" --jq '.data.message_id'
fi
```

非边界路径不允许在 `content_scoring.py` 与渲染发送之间返回模型；评分卡发送成功后，`score_only=true` 立即输出抑制标记并结束，不清理 `run_dir`、不复述结果，临时目录交给系统回收。

### 评分快路径（禁止探索性往返）

- 脚本固定为 `/Users/yuwei/code/read-x/scripts/content_scoring.py`；不搜索、不定位。
- 抓取后的第一个工具批次同时发送评分起点并运行一次性闭卷质量命令。评分起点后禁止再查脚本用法、创建 plan、生成任务文件或确认 `codex exec`。
- 主 Agent 不读取评分材料；`generate_quality.py` 只读 `blind_source_parts` 和 `quality-runtime.md`，用一次模型调用直接判断证据、洞察、迁移三维等级，裁决一次预算并从原文组装引用，一次写出 `quality-output.json`。
- 禁止把完整 content-scoring Skill、原始 `source.md`、`references/anchors.md`、任何锚点视图、schema、policy、URL、标题或用户对话传入质量 Agent；数值由脚本独占。
- 从“正文抓取完成，开始评分”到评分卡，非边界文章只允许：①并行发送起点与运行质量命令；②运行脚本并在同一命令渲染发送。只有 `needs_relevance` 才插入相关性响应。期间禁止用户可见解释、help、能力探测、搜索实现、方案设计、创建中间任务说明、重复读文件或手工核验引用。
- 第一次 `content_scoring.py`、非边界卡片渲染与发送必须在同一个工具调用中完成；先按 `score_status` 分支，不复制 6/7 数值阈值。脚本发现结构/枚举/引用错误时按既有失败关闭发送状态卡，不在当前上下文现场修 JSON 后重跑。只有契约规定的 fresh-context 隔离重评可产生第二份质量输出。
- 同一正文只读一次；引用失败直接按契约关闭，不在当前上下文返工。
- 不手算权重、证据封顶、档位或路由；不在运行脚本前做“预判”。
- 用户可见的评分过程消息只允许一条：`正文抓取完成，开始评分｜<文章标题>`；禁止再发“正在评分”、字数或步骤复述。
- 不为发卡再次阅读 Skill、schema 或 policy；卡片只消费已校验的 `scoring_result` 和文章元数据。
- 禁止手写卡片 JSON 或用 heredoc 组装。用 `/Users/yuwei/code/read-x/scripts/render_score_card.py <run_dir>/scoring-result.json --title ... --author ... --date ... --url ... [--score-only] --output <run_dir>/score-card.json` 生成经验证的 CardKit 2.0 JSON，再用 `lark-cli` 发送。
- 渲染器退出码为 0 即视为卡片结构验证通过；禁止再读取、筛选或人工核对生成的卡片 JSON。
- 主张、引用、枚举、三维输出和 JSON 自检只遵循 `quality-runtime.md`，编排层不复制认知规则。
- `score_only=true` 时评分卡发送成功即结束；不再生成文章复述、校准总结或第二张结果卡。最终回复必须只写 bridge 已支持的 `[[TIME_X_CARD_SENT]]`，复用自交付卡片抑制机制，避免 bridge 再把过程与总结包装成第二张卡。

### 评分卡（所有路由必发）

> 专项快通道（[0.5]）命中时不评分、不发评分卡；本段「所有路由」指 content-scoring 评分流程内的路由。

评分完成后、进入任何深度处理前，**必须先发一张评分卡**（`--as bot`）。这是评分流程的固定产物：

- `score_status=needs_full_text|needs_review`：状态卡即最终卡；说明需要完整正文或人工复核，不显示任何数字。
- `score_status=needs_relevance`：内部状态，禁止发卡。
- `route=card`：评分卡即最终卡。评分块作为卡片头部，下方接对应摘要/金句/链接，不再单独发结果卡。
- `route=long_read`：评分卡作为进度卡，告知"正在精读，稍后发文档"，long-read 完成后再发交付卡。
- `score_only=true`：不论脚本 `route`，评分卡都是最终卡，显示“本次仅评分，不进入精读”后结束。

正式评分卡显示质量分、相关性（`< quality_floor` 显示“未计算（不影响本次路由）”，`≥ quality_floor` 不可用显示“不可用”，否则显示真实相关性分）、兴趣（同相关性规则）、决策分、质量档位、三维数值和一句结论。不得展示 YWNext 私有原文。示例：

```json
{
  "schema": "2.0",
  "header": {"title": {"tag": "plain_text", "content": "评分完成"}, "template": "indigo"},
  "body": {"elements": [{"tag": "markdown", "content": "《标题》\n\n**质量 {quality_score}/10 · {quality_label}**\n相关性 {relevance_score 或 不可用} · 兴趣 {interest_score 或 不可用} · 决策 {decision_score}/10\n**三维**\n证据与论证 {quality_dimensions.evidence_quality.score} · 洞察解释 {quality_dimensions.insight_explanatory.score} · 长期迁移 {quality_dimensions.transfer_durability.score}\n{scoring_result.conclusion}\n\n正在精读，稍后发文档。"}]}
}
```

### 分派

数值规则只存在于 content-scoring policy 与脚本。link-card 不重算、不手动覆盖：

- `route=long_read`：转 long-read，传 `scoring_result`。
- `route=card` 且 `quality_label=快速阅读`：轻量精读卡。
- 其他 scored card：一句话卡片。
- 非 scored：无数字状态卡。

`score_only=true` 在上述分派之前截止：发完评分卡即结束，不改写 `route`。

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

> ⚠️ 精读完成卡是「交付」卡，不重复评分与三维--评分细节只在评分卡出现一次。这里只留档位一句话 + 核心结论 + 暗流 + ljg 链路 + 文档链接。
>
> ⚠️ **三档齐全门（硬性）**：`quality_score ≥ quality_floor`（6.0）的文章，相关性、兴趣两轴未算完（`relevance_score`/`interest_score` 为 `null`/「待计算」/「不可用」）时，禁止发精读完成卡或文档交付卡。先补算两轴，三档全部算完才一起发卡；禁止只带质量分单发交付卡。

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
        "content": "**作者/来源** · 平台 · 时间\n\n---\n\n**质量 {quality_score}/10 · {quality_label}**\n相关性 {relevance_score 或 不可用} · 兴趣 {interest_score 或 不可用} · 决策 {decision_score}/10\n**三维**\n证据与论证 {quality_dimensions.evidence_quality.score} · 洞察解释 {quality_dimensions.insight_explanatory.score} · 长期迁移 {quality_dimensions.transfer_durability.score}\n{scoring_result.conclusion}\n\n---\n\n核心要点（1-3 条）\n\n---\n\n💬 金句\n> 原文金句 1\n> 原文金句 2"
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
        "content": "**来源**\n\n**质量 {quality_score}/10 · {quality_label}**\n相关性 {relevance_score 或 不可用} · 兴趣 {interest_score 或 不可用} · 决策 {decision_score}/10\n**三维**\n证据与论证 {quality_dimensions.evidence_quality.score} · 洞察解释 {quality_dimensions.insight_explanatory.score} · 长期迁移 {quality_dimensions.transfer_durability.score}\n{scoring_result.conclusion}\n\n---\n\n一句话摘要\n\n[查看原文](链接)"
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
10. **评分与三维展示**：正式评分只在评分卡出现一次，展示质量、相关性、兴趣状态或分数、决策分和三维数值。`needs_relevance` 不得展示；`needs_full_text`、`needs_review` 不显示数字。精读完成卡只留质量档位一句话并注明“完整评分详见评分卡”，不重复三维。**三档齐全门**：`quality_score ≥ quality_floor`（6.0）的文章，相关性、兴趣两轴未算完不得发精读完成卡，先补算两轴再一起发。

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

- [ ] 阮一峰《科技爱好者周刊》-> [0.5] 快通道命中，跳过评分，直接周刊专项卡片（不评分、不发评分卡、不生成飞书文档）
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
- [ ] `quality_score ≥ quality_floor` 时，精读完成卡在相关性、兴趣两轴算完后才发出，未只带质量分单发
- [ ] 群聊场景：评分卡、精读完成卡、ljg-card PNG 均私聊发给 `senderId`，未发回原群
- [ ] p2p 场景：用 `chatId` 只发一次，未用 `--user-id` 重复发送
- [ ] senderType=bot（bot-at-bot）：回退发原群
