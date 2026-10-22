---
name: long-read
description: "长文精读编排器：接收已判定为 route=long_read 的任务，消费 content-scoring 结果，使用持久工作流生成、装配并核验主文档、必需图片和私聊交付；可从检查点恢复并生成周期可靠性报告。"
---

# long-read：长文精读编排器

只处理 `route=long_read`。**评分由 content-scoring 在上游完成，long-read 只消费评分结果，禁止重评。** 使用持久工作流 CLI 接管生成、装配、文档与图片交付、恢复和结果核验；不得以分析完成代替交付成功，也不得绕过工作流直接发布或发送。不要在同一次生成中模拟 `article-decode` 或 ljg Skill。

## 工作流

```text
原文 + Evidence + scoring_result + 评分凭据 + 可信触发上下文
     -> workflow CLI submit：校验并持久接管；返回 accepted 才表示已接管
     -> 独立 Worker：文字分支检查点 -> 程序装配 -> 主文档发布与读回
     -> 逐张图片：生成 -> 文档插入与读回 -> 单独私聊发送与读回
     -> 所有必需产物确认后，唯一完成门才允许最终成功通知
     -> 中断后 status / resume：先核验在途副作用，再从检查点继续
     -> Worker 同步维护周报/月报的固定快照与报告交付
```

## 1. 来源与 Evidence

来源适配、文体规则见 `references/routing.md` 与 `references/genre-rules.md`。Evidence 结构见 `references/output-schema.md`。

Evidence 只能来自原文。作者、日期未知写 `null`；抓取缺失或截断写入 `uncertainties`；金句必须通过 `scripts/validate_output.py` 的连续子串校验。

## 2. 消费评分结果与文字深度数量

评分在 `link-card` 阶段由 `content-scoring` 完成一次。`long-read` 只接受 `score_status=scored` 且 `route=long_read` 的 `scoring_result v3`，直接消费 `ljg_range`、`ljg_card` 与 `chatgpt_munger_doc`，**不得重新评分或复制阈值**。区间内只有存在互不重复的独立问题才取上限；`scoring_result.questions` 优先作为问题来源。`ljg_range` 仍决定文字 ljg 数量；`chatgpt_munger_doc` 与 `ljg_card` 只作评分留档——芒格分支 route=long_read 每篇必跑，核心内容图每篇必生成。

### 三档齐全门（硬性）

对 `quality_score ≥ quality_floor`（6.0）的文章，进入交付前必须三档齐全：`relevance_score` 与 `interest_score` 都必须是实数。任一为 `null`/「待计算」/「不可用」时，禁止进入精读交付（不发精读完成卡、不发文档交付卡），先按 content-scoring 相关性隔离阶段补算相关性、兴趣两轴，三档全部算完才一起发卡交付。质量分单独不算完，交付卡不得只带质量分发出。

## 3. 隔离生成与持久接管

文字任务由持久 Worker 调用 `run_isolated_analyses.py` 和 `run_chatgpt_munger.py`。各请求保持独立、互不可见；全局并发 ≤3 路，请求发起错开 ≥10 秒。保留 Bridge 的提交确认、`verification=live-dom+snapshot`、会话 URL、哈希与进程退出码门禁。一个分支失败不得覆盖其他分支的成功结果。

只有 `score_status=scored` 且 `route=long_read` 才可提交。提交前准备同一轮的原文、Evidence、评分结果、评分卡凭据、可信触发上下文和 0–3 个唯一问题文件。可信触发上下文必须包含 `bridge_context.sourceMessageId`；若字段缺失或本轮合并了多条消息，停止提交，不得猜测或替换消息 ID。将该 ID 原样传给 `--source-message-id`，用于按应用身份和来源事件去重。`long_read_workflow_cli.py submit` 校验后先保存不可变输入快照，再登记任务；只有返回 `status=accepted` 才表示持久接管。

重复入站以应用身份和来源消息 ID 去重。用户再次主动提交同一 URL 仍是新任务。提交失败时不得报告“已接管”。`status` 只读查看状态；`run-worker` 推进待办任务；`resume --job-id` 从检查点恢复。恢复时先只读核验在途副作用，不能把结果不明变成重新提交。已成功产物按内容哈希和收据复用。

### 单分支 Prompt 精简契约

`article-decode`、每条文字 ljg 与芒格分析的实际请求都必须附带 [`single-branch-concision.md`](references/single-branch-concision.md) 的完整基准正文。它是生成约束：保留当前分支的专业方法、必要证据与论证，删除低价值重复；不设统一硬性总字数门槛，不截断、不以后处理压缩，也不跨分支删重。调用层发现当前分支 Prompt 含冲突的硬性总篇幅要求时，必须在提交前停止并报告冲突，不得同时发送相互矛盾的指令。

### 输入边界

- `article-decode` 请求只含完整原文与客观 Evidence；
- 每条文字 ljg 请求只含完整原文、客观 Evidence 和分配给它的唯一问题；
- 所有请求都禁止用户画像、评分解释、其他分析结果、主 Agent 预设结论和前序对话。

Evidence Schema 与原文逐字引文在请求前严格校验。校验失败时不发出请求。模型输出含 shell 命令、本地写入路径、语音通知残留，或缺少最低正文与关键形式时，该分支失败且不落盘。

必跑文字分支明确失败时，保留其他可用成果，但整体不得标成功。每个分支的输入哈希、尝试、失败原因、耗时和结果由工作流持久记录。

## 4. 完整装配，不做后处理压缩

程序从本轮通过校验的分支产物、评分和来源完整装配主文档。不得手工重新生成、摘要或压缩成功分支原稿。装配只允许：

- 每个成功分支原样收录：`article-decode`、每条文字 ljg、`chatgpt-munger.md`；
- 不选择、不删改、不合并、不摘要、不压缩；
- 按输出结构拆分过长段落并转义、排版为 Docx XML；
- 保持 Markdown、XML 和校验收据绑定同一内容版本。

`chatgpt-munger.md` 成功时作为一级主章节「芒格洞察」拼进主文档；失败时保留失败状态，不创建第二篇文档。输入版本变化、收据缺失或文件哈希不符时不得复用旧产物。

文档根 `<title>` 只承载文章标题，正文不得再次输出同名标题。正文第一个主章节必须是 `<h1>评分</h1>`；评分、原文金句、文章真正的核心、基石 / 边缘 / 暗流、值得研究的相关问题、与作者对话、最值得深读之处、附录和必要事实均为一级主章节，附录内各文字 Skill 边界使用 `h2`，其内部标题使用 `h3` 或更低层级。

不要把独立 Skill 的表达统一改写成平直白话。完整原稿中的重复也保留；不得以“去重”“精华”或“可读性”为由删除用户尚未看到的分析内容。

文档与单分支分析均不设统一硬性总字数门槛；实际生成约束见 [`single-branch-concision.md`](references/single-branch-concision.md)。附录保留每条 ljg 完整原稿，不压缩、不改语义；标题后不放导言概要。排版细则见 `references/output-schema.md` 第 3、4 节。
准备每条文字 ljg 的唯一问题文件后，通过持久工作流入口提交同一轮输入：

```bash
python3 .agents/skills/long-read/scripts/long_read_workflow_cli.py submit \
  --source <run_dir>/source.md \
  --evidence <run_dir>/evidence.json \
  --scoring-result <run_dir>/scoring-result.json \
  --score-gate <run_dir>/score-gate.json \
  --trigger-context <run_dir>/trigger-context.json \
  --app-id <trusted-app-id> \
  --feishu-profile <trusted-profile> \
  --source-message-id <bridge_context.sourceMessageId> \
  --question ljg-think <run_dir>/question-01.md
```

按实际分支重复 `--question` 0–3 次。返回 `accepted` 后，Worker 持久推进分析、程序装配、Document Compiler 发布、图片交付和通知。`status` 只读查看；`run-worker` 推进一个任务；部署后的 `run-worker --serve` 在同一 Worker 中检查报告周期；`resume --job-id <id>` 先核验不确定的远端动作，再从可确认检查点接续。不要运行旧 `run_long_read_pipeline.py`、直接写飞书或手工重发。

仅当 `bridge_context.readxCanary` 为 `true` 时，在 `trigger-context.json` 原样保留该字段并在 `submit` 命令中附加 `--test-task`；bridge 只会在单条群内用户原始消息以 `【CANARY｜Read-X long_read 端到端验收】` 开头时设置该可信标记。标记与 CLI 参数不一致时提交必须失败关闭。该标记只将任务排除在正式成功率统计之外，不改变评分、交付或恢复规则。其他任务不得附加此参数。

## 5. 成品结构

顺序固定：

1. ChatGPT 溯源链接（紧跟 `<title>`，独立段落；来自芒格分支 `conversationUrl`）；
2. 原文链接（独立段落，作为来源溯源入口）；
3. 评分表：`quality_score + quality_label`、`importance_score`、`relevance_score + priority_label`、`interest_score + interest_label`（不可用则如实写不可用）及简短依据；路由分不冒充质量分；
4. 原文金句（紧跟评分表，作为溯源入口）；
5. 全文唯一 `light-yellow` 高亮块：一句最核心结论；
6. 文章真正的核心；
7. 基石 / 边缘 / 暗流；
8. 值得研究的相关问题（独立的问题列表 + 多个问题共同的上下文列表，总计 ≤300 字）；
9. 与作者对话；
10. 最值得深读之处；
11. 芒格洞察（仅 ChatGPT 分支成功时，一级主章节；失败或未达门槛时整章省略）；
12. 附录：独立深度分析；
13. 必要事实（若有，文末）。

不输出「骨架」章节，不再输出独立的「X 光四层」。普通文章最多 5 条原文金句；确有足够密度时最多 8 条；允许更少，禁止凑数。
原文为英文时，用户可见的金句以中文译文为主展示并遵守信达雅（忠实原意、通顺自然、文采得体），英文原句紧随其后作溯源附注，格式「译文（原文：English original）」；Evidence 层仍保留逐字英文原文供 `validate_output.py` 校验，翻译只作用于成品展示，不改变证据层。

核心内容图与芒格洞察图不属于 Docx XML。工作流逐张生成、插入并读回核验，再单独发送并读回核验；每张图的生成、插入、发送共享最多两轮额外恢复额度。未知结果只做只读核验，不能盲目重建图片块或重发消息。

Docx XML、段落、颜色、引用和表格规范见 `references/output-schema.md`。主文档发布由持久工作流的 Document Compiler 适配器负责。

## 6. 持久交付与唯一完成门

工作流按持久检查点推进。主文档允许先交付，但必须通知“主文档已交付，整体处理中”。文字分支进入待核实时，发送独立持久通知，明确 Bridge 结果尚未确认、主文档未交付且系统只读核验原请求；通知本身也必须读回确认。分析分支失败、图片缺失、消息未确认或任一必需副作用待核实时，都不得报告最终成功。

每张必需图片分别核验生成、文档插入和单独私聊发送。首次失败后最多额外两轮，生成、插入、发送共用额度；正常间隔至少 240 秒。只恢复失败环节。不可恢复错误立即终止图片恢复；结果不明时先核验，不做盲目重发。

只有所有必跑文字分支、主文档发布和读回、主文档通知、每张必需图片的生成/插入/私聊发送及最终成功通知都确认后，任务才能进入成功终态。未触发的条件分支记录为 `not_required`。明确失败或无法核验时，通知缺失产物、阶段、原因或未知状态，并持久保留处理记录。

通知也通过持久发件箱交付。限流或已确认拒绝时，间隔至少 60 秒，最多额外尝试两次；重试沿用同一请求 UUID，并在完整读取远端消息后确认原卡是否已到达。Feishu 的一小时 UUID 去重窗口外不自动重发；历史读取不完整或匹配歧义时转待核实。

飞书副作用统一由工作流适配器执行并登记请求意图、远端标识和读回收据。不得手工绕过 `long_read_workflow_cli.py` 直接创建文档、插图、发消息或回写索引。Worker 身份、收件目标、近期消息频率与读回范围不能核验时停止发送。

## 7. 周期可靠性报告

部署前先用 `configure-report-target --profile <profile> --user-id <open_id> --chat-id <p2p_chat_id> --sender-id <app_id>` 验证并将报告目标写入私有状态库。配置时及每次报告投递前，只读验证当前用户身份、本人的 open_id、bot app_id、私聊类型和精确会话成员；任一信息不匹配或不可读时停止且不发送。该配置首次写入后不可静默更改，替换必须显式使用 `--replace`。常驻 `run-worker --serve`、`report --deliver` 和 `run-due-reports --deliver` 只使用已保存目标，任何传入断言不匹配时均拒绝投递。缺少持久目标时不得声称已具备自动投递。周报每周日 09:00 检查此前连续 7 天；只有明确最终失败占比 `F/N > 10%` 才发送。`N=0` 不发送；待核实与处理中计入分母，但不计入明确失败分子。月报每月 1 日 09:00 必须发送上个自然月报告，即使没有任务也写明无有效任务。

每份报告使用固定周期快照。完整 Markdown 附件和 Card 2.0 摘要分别发送、分别读回核验。用 `status --report-key <key>` 只读查看报告与附件/卡片投递收据；收件目标或投递结果无法确认时，保留待处理状态，不谎报已发送、不重复投递。只有真实任务不少于 50、覆盖完整且没有未结项时，才判断长期失败率目标。

人工处置、已实施修复和修复对比通过 `long_read_workflow_cli.py record-event` 进入同一持久事件账本，并纳入后续报告。`manual_intervention`、`manual_reconciled`、`manual_repair` 使用 `category`、单行 `summary` 和可选 `reference`；category 为 `recovery`、`reconciliation`、`repair` 或 `other`。`repair_applied`、`root_cause_fixed` 使用 `repair_id`、`failure_fingerprint`、单行 `summary`、`change_ref`。`repair_comparison` 使用 `repair_id`、`metric`、`before_value`、`after_value`、`before_sample_size`、`after_sample_size`、`before_window`、`after_window`；metric 为 `final_failure_rate`、`stage_failure_rate`、`recovery_rate` 或 `p90_duration_seconds`，窗口为 `YYYY-MM` 或 `YYYY-MM-DD..YYYY-MM-DD`。例如：

```bash
long_read_workflow_cli.py record-event --job-id <job-id> --event-type repair_applied --details-json '{"repair_id":"fix-42","failure_fingerprint":"bridge:response-unstable","summary":"保留原 Bridge 会话以便只读恢复","change_ref":"abc1234"}'
```

不要写入文章正文、凭据或敏感个人信息；同一任务、类型和规范字段的重复命令会复用原事件。

终态任务的输入快照与分析/图片产物保留 30 天，之后清除并脱敏任务记录；仍有未处理副作用的任务继续保留。过期扫描同时清理无任务引用的输入快照和中断提交遗留的临时快照。逐任务统计与已完成报告快照保留 180 天。原因、阶段、验证和错误码只保留已知机器值或数字；未知文本统一记为 `unknown`，避免把文章内容留在长期统计中。未解决任务及其产物、未完成的报告投递保留至处理。180 天后仅保留无任务标识的生命周期成功/失败计数，供长期指标使用；超出统计留存窗口的漏跑周期不重造报告。

## 8. 值得研究的相关问题

这是标准 long-read 主文的固定章节，位于「基石 / 边缘 / 暗流」之后、
「与作者对话」之前，直接替换旧的个性化章节。默认输出 2 个问题，
只有第三个问题具有独立且决定性的研究价值时才输出第 3 个。

问题优先从完整原文、Evidence、article-decode 和本轮成功的文字 ljg 中提炼，
聚焦因果机制、隐含前提、适用边界、证伪条件或关键取舍。先输出 `问题` 小节：2–3 个有序列表项。每个问题必须单独包含研究对象、关系和待判断点，脱离上下文列表也能读懂，不得使用悬空的“该判断”“上述内容”“它”等指代。再输出 `上下文` 小节：一个供全部问题共同使用的无序列表，只补充文章已经建立的核心事实、证据和未证实边界，不在问题项内写“上下文：”。章节正文可见文字（不含标题）≤300 字。

content-scoring 已校验的个人上下文只作为弱辅助排序信号，不能压过文章证据，
也不得在可见文字中贴个人标签或把个人背景写成文章事实。不要新增个人文件读取。

## 自检

- [ ] 是否只处理 `score_status=scored` 且 `route=long_read`，并原样消费上游评分和凭据？
- [ ] 是否由 `long_read_workflow_cli.py submit` 持久接管，并仅在返回 `accepted` 后报告已接管？
- [ ] 分支输入是否隔离，Bridge 结果是否核对提交确认、`live-dom+snapshot`、URL、哈希和进程退出码？
- [ ] 成功产物是否经输入版本、哈希和收据核验后复用？
- [ ] 是否由程序完整装配成功分支，并保留原文内容，不做截断或生成后压缩？
- [ ] 主文档是否发布并读回确认，主文档先交付时是否明确标记整体处理中？
- [ ] 必需图片是否逐张完成生成、插入与单独发送读回核验？
- [ ] 图片额外恢复是否最多两轮、阶段共用预算且正常间隔至少 240 秒？
- [ ] 远端结果不明时是否只读核验、保留待核实，未盲目重复副作用？
- [ ] 最终成功门是否要求所有必需分支和交付动作均确认？
- [ ] 周报/月报是否按固定窗口和 `F/N > 10%` 规则执行，并分别确认附件和卡片投递？
- [ ] 是否未通过旧临时流水线或直接飞书命令绕过持久工作流？
- [ ] 成品是否遵循固定章节、Evidence 引文校验和单分支 Prompt 精简规范？
