---
name: link-card
description: "群内收到任何链接，自动抓取→内容质量判断→决定深度分析或轻量摘要→以卡片形式回复。卡片用 bot 身份发送。内容质量而非链接类型决定分析深度。"
metadata:
  description_zh: 链接自动抓取、内容质量判断、卡片回复
  when_to_use: "群内收到任何 HTTP/HTTPS 链接时自动触发。所有链接类型统一走内容质量判断，不分来源。"
  dispatch_intent: "link-card"
---

# link-card: 链接卡片回复

群内收到任何链接，先抓取内容、做内容质量判断并发送评分卡。`route=card` 由本 Skill 生成结果卡；`route=long_read` 则转给持久工作流，后续进度、文档、图片和最终状态通知由 Worker 独占处理。**卡片必须以 bot 身份发送（`--as bot`）**，不要以 user 身份发送。

每次处理链接的第一步都必须重新读取本文件当前版本（本文件是入口，规则细节按下方索引从 references/ 按需加载）；长会话不得沿用启动时缓存的旧流程。若已读版本仍包含评分过程消息，必须放弃该版本并重读。

## 按需加载索引

本文件是入口，只保留每次任务都需要的规则；下列细节从 references/ 按需读取：

| 任务场景 | 读取 |
|---------|------|
| 想了解「为什么用卡片」的完整论证 | [references/first-principles.md](references/first-principles.md) |
| 非微信 URL（即刻 / 其他）的抓取命令与准备细节 | [references/fetch-non-wechat.md](references/fetch-non-wechat.md) |
| 专项文体快通道命中后的专项处理（阮一峰周刊 / 李继刚人生周报） | [references/genre-fast-path.md](references/genre-fast-path.md) |
| 需要评分卡 JSON 示例 | [references/score-card-example.md](references/score-card-example.md) |
| 生成或核对卡片 JSON 模板（高质量 / 中等 / 快速阅读 / 低质量 / 抓取失败） | [references/card-templates.md](references/card-templates.md) |
| 明确 link-card 与 long-read 的职责分工 | [references/long-read-relation.md](references/long-read-relation.md) |
| 修改本 Skill 后自测 | [references/self-test.md](references/self-test.md) |

---

## 第一性原理（完整论证见 references/first-principles.md）

1. **bot 身份发送** — 所有卡片用 `--as bot`，不用 user 身份
2. **内容质量决定分析深度** — 链接类型只影响抓取方式，不影响分析深度；已知专项文体（阮一峰周刊等固定栏目）跳过评分直接专项解析（见 [0.5]）
3. **卡片是展示层，不是分析层** — 不替代 long-read 的深度分析
4. **抓取方式由链接类型决定** — 公众号只调 `prepare_scoring_run.py`，正文由 `wx_fast.py` 纯 HTTP 抓取；即刻 curl + `__NEXT_DATA__`；通用网页 `read` skill；抓取后走同一套质量判断

---

## 入口约束：一次只处理一个内容

bridge 合并送达多条消息时（`user_input` 多段标注、`quoted_messages` 引用旧内容），**只处理最新一条用户指令**，其余只作上下文：

- 已响应过的旧消息不重复处理
- bot 进度卡、流式展示卡是过程产物，不是新指令
- 引用旧消息只作上下文，不重新执行其内容
- 需处理多个独立内容时，分次发送，一次一个

**受管多链接子任务**：当可信 Bridge 明确给出当前 URL、原始 `sourceMessageId`、逐文章评分幂等键时，本轮只处理该 URL。原始消息 ID 保持不变；评分发送命令使用 Bridge 提供的逐文章键并追加 `--source-message-id <原始消息ID>`；提交双包命令追加 `--article-url <当前URL>`。业务代码会按已认证入库关系复核 URL、消息身份和键；校验失败时停止，不自行改写 ID 或重发。普通单链接继续沿用原键和命令。

一次专注一件事，避免并行处理导致混乱、重复劳动和刷屏。

### 仅评分入口

最新一条用户指令精确为 `仅评分 <URL>` 时，设本次 `score_only=true`。仍执行真实抓取、质量评分、条件相关性和评分卡，并保留脚本计算的真实 `route`；评分卡发送后立即结束，即使 `route=long_read` 也不进入精读。卡片必须注明“本次仅评分，不进入精读”。普通链接没有这个例外。

### 普通内容默认双上下文包

除精确的“仅评分”请求与已命中的专项快通道外，凡原文完整且可校验的普通内容任务，都默认交付两份可直接用于 ChatGPT 的 TXT：文章深层解码与独立芒格洞察。这个默认要求不受 `route=card` / `route=long_read` 或评分高低影响；评分仍用于卡片和分析路由，不负责决定是否生成双包。

完成原文、Evidence、可信触发身份与简洁标题准备后，调用 `.agents/skills/long-read/scripts/long_read_workflow_cli.py submit-context-packs`，传入 `--source`、`--evidence`、`--trigger-context`、`--app-id`、`--feishu-profile`、`--source-message-id`、`--title`、`--title-source agent`、`--source-status complete` 与真实评分路由（`--route card|long_read`）。该命令只冻结输入、生成双包并尝试附件交付，不提交 ChatGPT 生成请求。只有包收据、文件校验和两份附件送达读回均确认后，才能报告“上下文包：已完成 2/2”；`accepted` 或本地文件存在都不代表送达完成。

未明确选择自动产物时，不运行正文、图片、额外文章或其他自动生成阶段；评分路由本身不构成自动执行授权。身份或原文不完整时保留待处理原因，不猜标题、不换发送身份。明确选择自动产物时，只按用户选择执行，并按其独立完成状态汇报；不得让自动链失败撤销已确认的包，也不得让包附件暂时受阻无故阻塞已授权的自动链。

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
  │    ├─ needs_relevance ->（补算默认暂停）--relevance-unavailable 回质量分路由
  │    ├─ needs_full_text|needs_review -> 无数字状态卡
  │    ├─ 完整原文 -> 持久双上下文包工作流（与评分路由解耦，不提交 ChatGPT）
  │    ├─ 用户明确选择自动产物 -> 持久自动工作流（仅选择的交付目标）
  │    └─ route=card|long_read -> 按评分路由生成对应卡片
  │
  └─ [2] 卡片输出：评分卡和 route=card 结果以卡片回复；route=long_read 后续交付由持久 Worker 独占
```

## [0] 抓取：按链接类型选择抓取方式

微信公众号只运行一次 `python3 /Users/yuwei/code/read-x/scripts/prepare_scoring_run.py <URL>`。它调用 `wx_fast.py` 进行纯 HTTP 正文抓取；确定性创建独立 `run_dir`、解析唯一保存路径、复制并核对 `source.md`、生成匿名正文，并在同一 run 目录生成 Base 配置快照。输出 JSON 含 `title_status` 和 `title_language`。若标题待补采或只有非中文原题，使用 EGO Lite 查看页面标题、H1、Open Graph 标题及中文 alternate；没有 alternate 时，可检索中文译文页，但必须确认译文页明确链接回同一原文 URL。不得根据英文标题自行翻译。中文标题核实时，调用 `python3 /Users/yuwei/code/read-x/scripts/apply_article_title.py --run-dir <run_dir> --url <URL> --title <EGO核实的标题>`，以其读回结果作为本轮标题。没有中文版本时保留真实原题；没有任何标题时保留 `标题待补采`。EGO 仅用于标题元数据，不用于正文和评分。HTTP 正文失败仍按真实失败状态处理。禁止在模型中自行 `mktemp`、解析 `fetch.log`、重建标题路径、扫描 output、调用 `grep -P` 或重复抓取。

非微信来源才先执行一次 `mktemp -d /tmp/readx-score.XXXXXX`，抓取后调用 `prepare_anchor_view.py --blind-only --article-source <source.md> --blind-output <blind-source.md>`，再运行 `python3 /Users/yuwei/code/read-x/scripts/fetch_base_config.py --output <run_dir>/base-config.json`。本次所有产物写入该目录；禁止固定共享路径或写入仓库。Base 读取失败时继续使用本地策略，并保留失败原因。

非微信来源（即刻 / 其他 URL）的准备细节与抓取命令见 [references/fetch-non-wechat.md](references/fetch-non-wechat.md)；其他 URL → `read` skill。

| 链接类型 | 抓取方式 |
|---------|---------|
| `mp.weixin.qq.com` | `prepare_scoring_run.py`（正文由 `wx_fast.py` 纯 HTTP 单次抓取；必要时 EGO Lite 仅补标题元数据） |
| `m.okjike.com` | curl 模拟移动端 → 提取 `__NEXT_DATA__` JSON → 解析 `props.pageProps.post` |
| 其他 URL | `read` skill |

## [0.5] 专项文体快通道

抓取完成后、调用 content-scoring 之前，先做一次专项文体识别。已知固定栏目跳过评分，直接按专项规则生成卡片——评分是为未知内容设计的路由决策器，对已知栏目评分是空转。完整识别标志、结构标志与命中后的处理见 [references/genre-fast-path.md](references/genre-fast-path.md)。

识别标志（标题/作者判定条件，满足任一即命中）：

- 阮一峰《科技爱好者周刊》：标题含「科技爱好者周刊」或「阮一峰」，或作者为「阮一峰的网络日志」
- 李继刚《人生周报》：标题含「人生周报」且作者为「李继刚」

命中即跳评分直接专项卡片：不调 content-scoring、不发评分卡，按 `long-read/references/genre-rules.md` 对应小节专项生成卡片（`--as bot` 私聊发送）。未命中正常走 [1] 评分。`仅评分 <URL>` 对专项文体无效：仍走快通道，因为该文体没有评分环节可「仅」执行。

## [1] 内容质量判断（统一标准）

抓取后，不论来源，统一调用 `content-scoring` v3.19（质量输出仍为 v3.16）。质量阶段只读去身份正文和通用数值语义，并独立输出 `problem_significance`。不做权威解析、身份包生成或外部搜索；七篇锚点及目标分只用于评分后的外部闭卷回归，禁止进入质量评分上下文。脚本应用硬门并按 70% 质量 + 30% 重要性计算决策分。只有脚本返回 `needs_relevance` 后，相关性阶段才在独立上下文中读主张清单和逐条正文/manifest 哈希验证通过的 YWContext P1。`coverage.complete=false` 时继续使用有效本仓正文，并按固定提交的实时规则表和 `external_followups[].lookup_hints` 检查与文章相关的外围来源；记录读回版本/哈希和缺口，不把未查范围当成“无兴趣”。仅当路由无效、无任何可用本仓 P1 或远端提交不可验证时，才用 `--relevance-unavailable` 回到质量分。不读取旧 YWNext 文件或其他未授权个人材料。`scripts/content_scoring.py` 统一计算最终路由和深度。质量结果传给 long-read，long-read 不得重评。（2026-11-23 起：相关性补算默认暂停，统一 `--relevance-unavailable`，见第 4 步。）

### 调用 content-scoring

1. 判断正文是否完整；片段或未知正文输出 `source_status=partial|unknown`，不得补造维度。抓取完成后的下一次模型响应直接执行第 2 步的闭卷质量命令，不发送评分过程消息。
2. 同时运行 `python3 /Users/yuwei/code/read-x/scripts/generate_quality.py <blind_source_parts...> --output <run_dir>/quality-output.json`。它通过当前 Codex 模型完成一次闭卷评分，传输失败时在同一个总超时内对同一模型重试。不切换模型、不传推理覆盖；输入只有匿名正文与质量契约。主 Agent 禁止读取匿名正文和质量契约。每次尝试都必须通过同一 Schema 和脚本校验；全部尝试失败、超时或未生成文件时才失败关闭，禁止回退主上下文或启动子 Agent。
3. 质量命令成功后，直接运行 `python3 scripts/content_scoring.py <quality_output.json> <source.md> --output <run_dir>/scoring-result.json`，拿 `scoring_result v3.19`。`base_config.json` 存在时必须在 `source.md` 后追加 `--config-from-base <base_config.json>`；不存在时省略并接受 `policy_source=local`。
4. **相关性补算默认暂停（2026-11-23 用户裁决）**：不读取 P1、不运行 `generate_relevance.py`。首次 `content_scoring.py` 调用统一追加 `--relevance-unavailable`：路由与深度照常由质量分+重要性决定，相关性/兴趣显示「未计算（不影响本次路由）」，`score_status` 直接为 `scored`（正常流程不再出现 `needs_relevance` 分支）。用户明确要求补算相关性时，才按 [references/relevance-stage-paused.md](references/relevance-stage-paused.md) 的原协议执行并去掉该旗标重跑；恢复默认须更新本裁决。
5. `needs_relevance` 不得发卡、不得传 long-read。只对 `scored`、`needs_full_text`、`needs_review` 生成用户卡片；`scored` 只据 `route` 分派。

第 3 步不是“先评分、下轮再发卡”。质量 JSON 之后必须在同一次 `exec_command` 内按下列固定尾部执行；只替换路径、卡片元数据和发送目标，不改分支：

```bash
score_config_args=()
if [ -f "<base_config.json>" ]; then
  score_config_args=(--config-from-base "<base_config.json>")
fi
python3 scripts/content_scoring.py <quality_output.json> <source.md> "${score_config_args[@]}" --relevance-unavailable --output <scoring-result.json>
score_status_value=$(jq -r '.score_status' <scoring-result.json>)
if [ "$score_status_value" = needs_relevance ]; then
  cat <scoring-result.json>
else
  python3 /Users/yuwei/code/read-x/scripts/send_score_card.py <scoring-result.json> --title <title> --author <author> --date <date> --url <url> [--score-only] --chat-id <chatId 或 --user-id <senderId>> --idempotency-key "readx-score-<当前消息ID>" --evidence-output <run_dir>/score-gate.json
fi
```

非边界路径不允许在 `content_scoring.py` 与渲染发送之间返回模型；评分卡发送成功后，`score_only=true` 立即输出抑制标记并结束，不清理 `run_dir`、不复述结果，临时目录交给系统回收。`score-gate.json` 是评分卡已发送的唯一凭据；命令失败或未写出凭据时禁止进入 long-read，也不得手工造凭据。

### 评分快路径（禁止探索性往返）

- 脚本固定为 `/Users/yuwei/code/read-x/scripts/content_scoring.py`；不搜索、不定位。
- 抓取后的第一个工具批次只运行一次性闭卷质量命令。此后禁止再查脚本用法、创建 plan、生成任务文件或确认 `codex exec`。
- 主 Agent 不读取评分材料；`generate_quality.py` 只读 `blind_source_parts` 和 `quality-runtime.md`，用一次模型调用直接判断证据、洞察、迁移三维等级，裁决一次预算并从原文组装引用，一次写出 `quality-output.json`。
- 禁止把完整 content-scoring Skill、原始 `source.md`、`references/anchors.md`、任何锚点视图、schema、policy、URL、标题或用户对话传入质量 Agent；数值由脚本独占。
- 从抓取完成到评分卡，非边界文章只允许：①运行质量命令；②运行脚本并在同一命令渲染发送。只有 `needs_relevance` 才插入相关性响应。期间禁止用户可见解释、help、能力探测、搜索实现、方案设计、创建中间任务说明、重复读文件或手工核验引用。
- 第一次 `content_scoring.py`、非边界卡片渲染与发送必须在同一个工具调用中完成；先按 `score_status` 分支，不复制 6/7 数值阈值。脚本发现结构/枚举/引用错误时按既有失败关闭发送状态卡，不在当前上下文现场修 JSON 后重跑。只有契约规定的 fresh-context 隔离重评可产生第二份质量输出。
- 同一正文只读一次；引用失败直接按契约关闭，不在当前上下文返工。
- 不手算权重、证据封顶、档位或路由；不在运行脚本前做“预判”。
- 评分期间不发送任何用户可见过程消息，最终只发评分卡。
- 不为发卡再次阅读 Skill、schema 或 policy；卡片只消费已校验的 `scoring_result` 和文章元数据。
- 禁止手写卡片 JSON 或用 heredoc 组装。用 `/Users/yuwei/code/read-x/scripts/send_score_card.py <run_dir>/scoring-result.json --title ... --author ... --date ... --url ... [--score-only] --chat-id <chatId 或 --user-id <senderId>> --idempotency-key "readx-score-<当前消息ID>" --evidence-output <run_dir>/score-gate.json` 发送。
- wrapper 退出码为 0 且写出 `score-gate.json` 才视为发送成功；凭据包含 lark-cli 返回的 `message_id`，后续交付卡用它做硬门。禁止再读取、筛选或人工核对生成的卡片 JSON。
- 主张、引用、枚举、三维输出和 JSON 自检只遵循 `quality-runtime.md`，编排层不复制认知规则。
- `score_only=true` 时评分卡发送成功即结束；不再生成文章复述、校准总结或第二张结果卡。最终回复必须只写 bridge 已支持的 `[[TIME_X_CARD_SENT]]`，复用自交付卡片抑制机制，避免 bridge 再把过程与总结包装成第二张卡。

### 评分卡（所有路由必发）

> 专项快通道（[0.5]）命中时不评分、不发评分卡；本段「所有路由」指 content-scoring 评分流程内的路由。

评分完成后、进入任何深度处理前，**必须先发一张评分卡**（`--as bot`）。这是评分流程的固定产物：

- `score_status=needs_full_text|needs_review`：状态卡即最终卡；说明需要完整正文或人工复核，不显示任何数字。
- `score_status=needs_relevance`：内部状态，禁止发卡。
- `route=card`：评分卡即最终卡。评分块作为卡片头部，下方接对应摘要/金句/链接，不再单独发结果卡。
- `route=card` 且 `quality_label=快速阅读`：发评分卡前先按「速读卡结构」提炼 200-400 字速读内容写入 `<run_dir>/quick-read.md`，发送命令加 `--quick-read <run_dir>/quick-read.md`；缺失或为空时脚本失败关闭。
- `route=long_read`：评分卡作为进度卡，告知"正在精读，稍后发文档"，long-read 完成后再发交付卡。
- `score_only=true`：不论脚本 `route`，评分卡都是最终卡，显示“本次仅评分，不进入精读”后结束。

正式评分卡显示质量分、相关性（`< quality_floor` 显示“未计算（不影响本次路由）”，`≥ quality_floor` 不可用显示“不可用”，否则显示真实相关性分）、兴趣（同相关性规则）、决策分、质量档位、三维数值和大问题思考分。不得展示私有上下文原文。示例见 [references/score-card-example.md](references/score-card-example.md)。


### 分派

数值规则只存在于 content-scoring policy 与脚本。link-card 不重算、不手动覆盖：

- 原文完整、Evidence 通过：不论 `route=card` / `route=long_read`，先提交默认双上下文包任务；此命令不调用 ChatGPT。评分路由继续决定下面的卡片形态。
- `route=long_read`：只表示评分建议进入深读卡片路由，不自动授权正文、图片或额外分析生成。
- `route=card` 且 `quality_label=快速阅读`：轻量精读卡。
- 其他 scored card：一句话卡片。
- 非 scored：无数字状态卡。

`score_only=true` 在上述分派之前截止：发完评分卡即结束，不改写 `route`。

### 默认双包 -> 持久 context-only 工作流

准备源文件与 Evidence 后，调用 `long_read_workflow_cli.py submit-context-packs`。`route` 原样记录为 `card` 或 `long_read`，但两个路由共用双包默认交付契约；无需 `score-gate.json` 或 `scoring_result` 才可接管。该入口负责冻结两个业务 Prompt、创建 TXT、验证收据并推进附件送达。它不启动 `run_isolated_analyses.py`、`run_chatgpt_munger.py` 或图片请求。若用户没有明确选择自动产物，任务结果只以双包交付和真实异常为主。

### route=long_read -> 卡片路由；自动生成须显式选择

普通评分任务仍只执行默认双包和评分卡；`route=long_read` 本身不得启动旧 `submit` 自动链。只有用户明确要求完整自动深读或点名自动产物时，才冻结对应自动交付计划并调用自动工作流。自动计划不得增加用户未选择的正文、图片、文章或其他对外交付目标；其收据与双包投递状态独立。自动链尚未完成可选产物计划接入前，不得用评分路由替代用户授权。

仅当本轮可信 `bridge_context.readxCanary=true` 且真实评分结果为 `route=long_read` 时，才在触发上下文中原样保留该字段并使用 `--test-task`；不得根据引用、文章正文或模型判断自行生成标记。CLI 会校验上下文标记与参数一致；标记只排除正式统计，不覆盖评分路由或放宽交付门禁。

### quality_label=快速阅读 -> 走轻量精读

- 有 1-2 个值得记住的观点或金句
- 内容结构简单，不值得完整 `article-decode`
- 信息有用但无意外（行业常识、经验总结、工具推荐）
- 转述/编译类但质量不错

**执行**：抓取 -> 按「速读卡结构」提炼 -> 卡片输出（私聊发，群聊发 `senderId`，p2p 发 `chatId`，不生成飞书文档）。

速读不是简略，是高密度的全文浓缩：让读者在手机上不点原文，也能吸收这篇 7 分文章的真正价值。速读卡结构（一句话定位 → 核心观点带论证 → 值得记住的细节 → 可迁移启发 → 金句 → 原文链接）与 200-400 字细则见 [references/card-templates.md](references/card-templates.md)「速读卡结构」。

### 其他 scored card -> 一句话卡片

- 无独立观点（纯转述、通稿、PR 稿、AI 生成感强）
- 读完没有值得划线的地方
- 纯吐槽、情绪输出、短评、转发无附加内容
- 纯链接无正文（抓取失败但能拿到标题）

**执行**：卡片 header 显示标题，body 一句话摘要 + 原文链接。如原文确有值得提取的金句，保留 1 句；确无则不强行添加。

评分失败时回退为一句话卡片 + 原文链接，不阻塞交付。

---

## [2] 卡片模板

所有卡片使用 CardKit 2.0 schema（`schema: "2.0"`），header 用 `indigo` 模板。完整 JSON 模板与高质量 long-read 状态卡规则见 [references/card-templates.md](references/card-templates.md)。

路由 → 模板对照：`route=long_read` 的状态/交付通知由持久工作流独占渲染，入口不手工拼 JSON；`route=card` 中等质量 → 中等质量内容卡片；`quality_label=快速阅读` → 快速阅读卡；其他 scored → 低质量内容卡片（一句话）；抓取失败 → 抓取失败卡片。

---

## 发送卡片命令

本节直发命令仅适用于评分卡和 link-card 自己处理的中低质量结果卡。`route=long_read` 被工作流接管后，进度、文档、图片和终态通知都由 Worker 独占发送；入口不得直发精读完成卡或图片。

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

> ⚠️ 群聊场景评分卡私聊发给触发者。long-read 接管后的所有通知与图片由 Worker 按已验证私聊目标发送，不得发回原群或由入口重复发送。p2p 场景用 `chatId`。

**关键约束：**
- `--as bot`：必须，卡片以 bot 身份发送
- `--msg-type interactive`：必须，表示交互卡片
- `--content`：JSON 字符串，直接传入或用文件
- 卡片 JSON 写到临时文件 `/tmp/link_card.json`，发送后清理
- 本 Skill 发送的评分卡和 route=card 卡片按触发上下文选择目标；long-read 的图片和后续通知由 Worker 按自身已验证目标处理
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
| 高质量（long-read） | 由工作流状态决定 | Worker 生成处理中、最终成功、最终失败或待核实通知；入口不另发完成卡 |
| 中等质量 | 300-500 字 | 与原文长度成正比。短帖 300-400 字，中篇 400-500 字 |
| 快速阅读 | 200-400 字 | 速读卡结构：一句话定位 + 核心观点（带论证）+ 值得记住 + 可迁移 + 金句。高密度浓缩，不是三五行简略 |
| 低质量 | 50-150 字 | 评分块（不计入字数）+ 一句话摘要 + 原文链接

9. **金句规则（无文档卡片必选）**：不生成飞书文档的卡片（中等质量 + 低质量有可提取内容时），必须从原文提取金句。
   - **密度**：每 2000 字原文提取 1-2 个金句。例如 6000 字原文 → 3-6 个金句
   - **金句定义**：原文中独立成句、有记忆点、脱离上下文仍有力度的表达。不一定是「金句格式」，但必须值得划线
   - **格式**：卡片中以 `> 原文金句` 引用块呈现，每条金句不超过 60 字
   - **英文翻译（硬）**：原文为英文时，金句以中文译文呈现并遵守信达雅（忠实原意、通顺自然、文采得体），译文后附英文原句作溯源，格式 `> 中文译文（原文：English original）`；英文附注不计入 60 字限制。禁止只贴英文不译。
   - **低质量例外**：若原文确无值得划线的内容，不强行添加；但不能因为「懒」而跳过
10. **评分与重要性展示**：正式评分只在评分卡出现一次，展示质量、大问题思考、相关性、兴趣状态或分数、决策分和三维质量数值；不展示权威性。`needs_relevance` 不得展示；`needs_full_text`、`needs_review` 不显示数字。long-read 后续通知只由 Worker 依据持久状态生成，不重复评分。**三档齐全门**：`quality_score ≥ quality_floor`（6.0）的文章，相关性、兴趣两轴未算完不得进入 long-read 工作流，先补算两轴再接管。（2026-11-23 用户裁决：相关性补算默认暂停期间，`--relevance-unavailable` 产出的「未计算（不影响本次路由）」状态视作两轴暂缺的合规态，long-read 工作流照常接管与交付；恢复补算后本门恢复原语义。）


---

## 边界情况处理

| 情况 | 处理 |
|------|------|
| 抓取失败（超时/反爬/404） | 卡片 header 显示「抓取失败」，body 说明原因 + 附原始链接 |
| 内容为空/极短 | 卡片 header 显示标题，body 显示「内容过短，无法提取摘要」+ 原始链接 |
| 超长文章（>10000 字） | 仍走同一评分；长度不替代质量 |
| 特殊字符/emoji | 正常保留，JSON 正确转义 |
| 群内多发链接 | 每条链接独立处理，各自私聊发一张卡片 |