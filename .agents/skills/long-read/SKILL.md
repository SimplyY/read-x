---
name: long-read
description: "长文精读编排器：收到微信公众号、飞书文档、网页链接、GitHub 仓库或粘贴长文后，建立客观 evidence，消费 content-scoring 的 route、ljg_range 与 ljg_card，再通过独立 HTTP 请求并行运行 article-decode 和选中的文字深度 Skill，最后去重拼接为高密度 Docx XML 飞书文档并私聊交付给触发者。"
---

# long-read：长文精读编排器

只做来源路由、Evidence、隔离调度、去重拼接和交付。**评分由 content-scoring 在上游完成，long-read 只消费评分结果，禁止重评。** 不要在同一次生成中模拟 `article-decode` 或 ljg Skill。

## 工作流

```text
原文 -> Evidence
     -> 消费 content-scoring 的 scoring_result（直接使用 ljg_range）
     -> article-decode + 0~3 个文字 ljg（独立 HTTP 请求，并行）
     -> 主 Agent 摘取、去重、排版为 Docx XML
     -> 创建文档并发送卡片（群聊私聊发 `senderId`，p2p 发 `chatId`，只发一次）
     -> ljg_card=true 时再运行 ljg-card，私聊发 PNG（群聊发 `senderId`，p2p 发 `chatId`）
```

## 1. 来源与 Evidence

来源适配、文体规则见 `references/routing.md` 与 `references/genre-rules.md`。Evidence 结构见 `references/output-schema.md`。

Evidence 只能来自原文。作者、日期未知写 `null`；抓取缺失或截断写入 `uncertainties`；金句必须通过 `scripts/validate_output.py` 的连续子串校验。

## 2. 消费评分结果与文字深度数量

评分在 `link-card` 阶段由 `content-scoring` 完成一次。`long-read` 只接受 `score_status=scored` 且 `route=long_read` 的 `scoring_result v3`，直接消费 `ljg_range` 与 `ljg_card`，**不得重新评分或复制阈值**。区间内只有存在互不重复的独立问题才取上限；`scoring_result.questions` 优先作为问题来源。`ljg_card=true` 时必须等主文档创建和通知成功后才开始。

## 3. 隔离运行

只通过 `scripts/run_isolated_analyses.py` 运行 `article-decode` 和文字 ljg。脚本为每个任务读取对应完整 `SKILL.md`；为 `article-decode` 追加推断必须标为「我的判断」的证据覆盖，对含工具/写文件步骤的外部 ljg 追加固定的无工具 HTTP 交付覆盖。覆盖只约束证据身份与交付动作，不改分析使命与方法。脚本分别向本地 MoonBridge `/v1/responses` 发送独立 `glm-5.2` 请求，固定 `store=false`，最多四请求并行。主 Agent 不读取这些 SKILL.md，不在自身上下文生成分析，也不得在脚本失败时回退角色扮演、SubAgent、fresh thread 或嵌套 `codex exec`。

为每条文字 ljg 创建只含唯一问题的独立文件，然后运行：

```bash
python3 .agents/skills/long-read/scripts/run_isolated_analyses.py \
  --source <run_dir>/source.md \
  --evidence <run_dir>/evidence.json \
  --output-dir <run_dir>/analyses \
  --summary-file <run_dir>/summary.json \
  --task ljg-think <run_dir>/question-01.md \
  --task ljg-qa <run_dir>/question-02.md
```

`--task` 按实际选择使用 0~3 次。每次消息先用 `mktemp -d /tmp/readx-longread.XXXXXX` 建立独立 `run_dir`，禁止复用固定 `.wx_decode.md` 或 `.wx_ljg_*.md`。脚本显式禁用环境 HTTP 代理，确保回环请求不外泄；单行 JSON 摘要及 `summary.json` 只包含任务状态、耗时、usage、Skill 哈希和输出路径。主 Agent 只读取摘要及成功生成的 Markdown。

### 输入边界

- `article-decode` 请求只含完整原文与客观 Evidence；
- 每条文字 ljg 请求只含完整原文、客观 Evidence 和分配给它的唯一问题；
- 所有请求都禁止用户画像、评分解释、其他分析结果、主 Agent 预设结论和前序对话。

脚本在发请求前严格校验 Evidence Schema、拒绝额外字段，并确认所有逐字引文确实存在于原文；任一校验失败时不发 HTTP。模型输出含 shell 命令、本地写入路径、语音通知残留，或缺少最低正文与关键形式时，该任务失败且不落盘。

脚本返回 `partial` 时跳过失败的文字 ljg，保留其他结果继续交付；`article-decode` 失败时按 `references/routing.md` 降级。不得把已有输出目录用于重跑，避免旧文件冒充本轮结果。

## 4. 拼接，不重写

主 Agent 维护全文，只做：

- 选择最锋利、最具文章特异性的原句或段落；
- 删除重复判断；
- 把超过 100 字的段落拆开；
- 补最少的连接语；
- 转义并排版为 Docx XML。

不要把独立 Skill 的表达统一改写成平直白话。相同证据可以复用，相同结论只能出现一次。能套在无关文章上的泛化句删除。

- 主文约 1000~2000 字。
- 附录放每条文字 ljg 原稿中的核心内容（每条核心内容长度600-1000字，压缩时不得去掉核心内容和逻辑连贯性），但每条原稿内部仍受可读性约束：单段≤100字、关键概念加粗、并列用列表、对比用表格、独立段落间换行。只做排版加工，不改语义、不磨平原 Skill 语气；附录标题后、各 ljg 前先放一段 200~300 字导言导读附录内容。完整规则见 `references/output-schema.md` 第 3、4 节。

## 5. 成品结构

顺序固定：

1. 三列评分表：`quality_score + quality_label`、`relevance_score + priority_label`、`interest_score + interest_label`（不可用则如实写不可用）及简短依据；路由分不冒充质量分；
2. 原文金句 + 原文链接（紧跟评分表，作为溯源入口的独立段落）；
3. 全文唯一 `light-yellow` 高亮块：一句最核心结论；
4. 文章真正的核心；
5. 基石 / 边缘 / 暗流；
6. 与作者对话；
7. 最值得深读之处；
8. 可选「对飞鱼的意义」，有独立增量才写，最多 50 字；
9. 附录：独立深度分析；
10. 必要事实（若有，文末）。

不输出「骨架」章节，不再输出独立的「X 光四层」。普通文章最多 5 条原文金句；确有足够密度时最多 8 条；允许更少，禁止凑数。

Docx XML、段落、颜色、引用和表格规范见 `references/output-schema.md`。创建文档前按 `lark-doc` Skill 读取当前 CLI 内置 XML、style 与 create workflow。

## 6. 交付顺序

1. 用 `.wx_doc.xml` 创建飞书文档；
2. 发送文档卡片：群聊场景 `--user-id <bridge_context.senderId>` 私聊发给触发者，p2p 场景 `--chat-id <bridge_context.chatId>`（即私聊会话，只发一次），全部 `--as bot`；`senderType=bot` 时回退 `--chat-id` 发原群；
3. 确认文档卡片发送成功后，回写一行到「精读记录」索引表，登记本次精读：

   ```bash
   lark-cli base +record-upsert --base-token ASdsbB3Gka9OKNsD7YhcJ9rZnjd --table-id tbltqJwdmOmcbFlI --as user --json '{"日期":"<当天 00:00:00>","标题":"<原文标题>","来源链接":"[<原文 URL>](<原文 URL>)","云文档链接":"[<飞书文档 URL>](<飞书文档 URL>)","评分":"<quality_score>/10","是否已读":true}'
   ```

   要点：`日期` 取当天 `00:00:00`；`标题` 用原文标题；`来源链接`/`云文档链接` 用 markdown 链接格式 `[url](url)`（与历史记录一致）；`评分` 取 content-scoring 的 `quality_score` 去尾零（如 `9/10`、`7.5/10`）；`是否已读` 固定 `true`。标题或 URL 含 `"`、`\` 等字符时，用 `python3 -c "import json,sys;print(json.dumps(sys.stdin.read()))"` 或等价方式构造 `--json` 值，禁手工拼接破坏 JSON。回写是登记步骤，失败不阻塞主流程，仅告警不回滚、不重试阻塞文档交付；
4. 独立运行 `ljg-card`；按 ljg-card「截图后校验」确认 PNG 生成（capture.js exit 0 + 文件存在 + size>0，禁止 `view_image`）；
5. PNG 私聊发给触发者，按 `chatType` 只执行一条、只发一次（禁止同时执行 `--chat-id` 与 `--user-id`）：p2p 场景 `lark-cli im +messages-send --as bot --chat-id <bridge_context.chatId> --image ./图片.png`，群聊场景 `--user-id <bridge_context.senderId>`；`senderType=bot` 时回退发原群；
6. 不把 PNG 插入文档；失败不修改、不延迟、不重复发送主文档。

具体命令、降级和临时文件清理见 `references/routing.md`。

## 7. 个性化边界

用户画像只能在 content-scoring 的独立相关性阶段及可选的「对飞鱼的意义」中使用。不得写回文章质量、基石、边缘、暗流、作者动机、事实或原文金句。

## 自检

- [ ] 是否消费 content-scoring 的 `scoring_result`，而非自行评分？
- [ ] `article-decode` 与每条文字 ljg 是否由脚本发出独立 `store=false` HTTP 请求？
- [ ] 主 Agent 是否未读取分析 Skill、未角色扮演生成、未在失败时回退？
- [ ] Evidence 是否通过严格 Schema 与逐字引文校验，summary 是否与本轮成功文件一一对应？
- [ ] 是否无「骨架」和独立「X 光四层」？
- [ ] 主文与附录是否没有重复结论？
- [ ] 原文金句是否逐字、总数不超过 8？
- [ ] 是否只有一个金色高亮块，段落均不超过 100 字？
- [ ] `ljg-card` 是否在文档通知后运行，且群聊私聊发 `senderId`、p2p 发 `chatId`？
- [ ] 附录每条 ljg 是否各用一个注明 Skill 名的独立 h2 包裹，原稿内部小标题是否降为 h3 未占用 h2？
- [ ] 附录每条 ljg 原稿是否做过排版加工（拆段≤100字、加粗、列表/表格、换行），而非原样照搬？
- [ ] ljg-card PNG 是否用文件校验（capture.js exit 0 + 文件存在 + size>0）确认，未调用 `view_image`？
- [ ] ljg-card PNG 是否只发送一次（按 chatType 二选一，未同时执行 `--chat-id` 与 `--user-id`）？
