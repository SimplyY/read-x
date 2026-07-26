---
name: long-read
description: "长文精读编排器：收到微信公众号、飞书文档、网页链接、GitHub 仓库或粘贴长文后，建立客观 evidence，消费 content-scoring 的 route、ljg_range 与 ljg_card，再让 article-decode 和选中的文字深度 Skill 在隔离上下文中独立运行，最后去重拼接为高密度 Docx XML 飞书文档并私聊交付给触发者。"
---

# long-read：长文精读编排器

只做来源路由、Evidence、隔离调度、去重拼接和交付。**评分由 content-scoring 在上游完成，long-read 只消费评分结果，禁止重评。** 不要在同一次生成中模拟 `article-decode` 或 ljg Skill。

## 工作流

```text
原文 -> Evidence
     -> 消费 content-scoring 的 scoring_result（直接使用 ljg_range）
     -> article-decode（隔离上下文）
     -> 0~3 个文字 ljg（各自隔离上下文）
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

### article-decode

使用正式 `article-decode` Skill。必须通过 SubAgent、fresh thread 或运行时提供的等价隔离机制执行，不能在主 Agent 当前上下文中角色扮演。给它完整原文与客观 Evidence，不给用户画像、既有摘要、评分解释或任何 ljg 输出。它独立产出文章真正的核心、基石/边缘/暗流、思想结构、与作者对话和最值得深读的论证。

### 文字 ljg

按 `references/routing.md` 选择。每条 Skill 在相互不可见的独立上下文中运行，只接收：

1. 完整原文；
2. 客观 Evidence；
3. 分配给它的唯一分析问题；
4. 自身 `SKILL.md`。

不得给它 `article-decode`、其他 ljg 或用户画像输出。每条同样必须通过 SubAgent、fresh thread 或等价隔离机制执行。隔离机制不可用时，明确降级并跳过对应深度产出，不得假装已独立运行。一个 Skill 失败时保留其他结果继续交付。

## 4. 拼接，不重写

主 Agent 维护全文，只做：

- 选择最锋利、最具文章特异性的原句或段落；
- 删除重复判断；
- 把超过 100 字的段落拆开；
- 补最少的连接语；
- 转义并排版为 Docx XML。

不要把独立 Skill 的表达统一改写成平直白话。相同证据可以复用，相同结论只能出现一次。能套在无关文章上的泛化句删除。

- 主文约 1000~2000 字。
- 附录放每条文字 ljg 原稿中的核心内容（每条核心内容长度700-1200字，压缩时不得去掉核心内容和逻辑连贯性），但每条原稿内部仍受可读性约束：单段≤100字、关键概念加粗、并列用列表、对比用表格、独立段落间换行。只做排版加工，不改语义、不磨平原 Skill 语气；附录标题后、各 ljg 前先放一段 200~300 字导言导读附录内容。完整规则见 `references/output-schema.md` 第 3、4 节。

## 5. 成品结构

顺序固定：

1. 两列表格：`quality_score + quality_label`、`relevance_score + priority_label`（不可用则如实写不可用）及简短依据；路由分不冒充质量分；
2. 全文唯一 `light-yellow` 高亮块：一句最核心结论；
3. 文章真正的核心；
4. 基石 / 边缘 / 暗流；
5. 与作者对话；
6. 最值得深读之处；
7. 可选「对飞鱼的意义」，有独立增量才写，最多 80 字；
8. 原文金句 + 原文链接（附录上方，主文末尾与附录导言之间）；
9. 附录：独立深度分析；
10. 必要事实（若有，文末）。

不输出「骨架」章节，不再输出独立的「X 光四层」。普通文章最多 5 条原文金句；确有足够密度时最多 8 条；允许更少，禁止凑数。

Docx XML、段落、颜色、引用和表格规范见 `references/output-schema.md`。创建文档前按 `lark-doc` Skill 读取当前 CLI 内置 XML、style 与 create workflow。

## 6. 交付顺序

1. 用 `.wx_doc.xml` 创建飞书文档；
2. 发送文档卡片：群聊场景 `--user-id <bridge_context.senderId>` 私聊发给触发者，p2p 场景 `--chat-id <bridge_context.chatId>`（即私聊会话，只发一次），全部 `--as bot`；`senderType=bot` 时回退 `--chat-id` 发原群；
3. 确认文档卡片发送成功后，独立运行 `ljg-card`；按 ljg-card「截图后校验」确认 PNG 生成（capture.js exit 0 + 文件存在 + size>0，禁止 `view_image`）；
4. PNG 私聊发给触发者：群聊场景 `lark-cli im +messages-send --as bot --user-id <bridge_context.senderId> --image ./图片.png`，p2p 场景 `--chat-id <bridge_context.chatId>`；`senderType=bot` 时回退发原群；
5. 不把 PNG 插入文档；失败不修改、不延迟、不重复发送主文档。

具体命令、降级和临时文件清理见 `references/routing.md`。

## 7. 个性化边界

用户画像只能在 content-scoring 的独立相关性阶段及可选的「对飞鱼的意义」中使用。不得写回文章质量、基石、边缘、暗流、作者动机、事实或原文金句。

## 自检

- [ ] 是否消费 content-scoring 的 `scoring_result`，而非自行评分？
- [ ] `article-decode` 与每条文字 ljg 是否真正使用隔离上下文？
- [ ] 是否无「骨架」和独立「X 光四层」？
- [ ] 主文与附录是否没有重复结论？
- [ ] 原文金句是否逐字、总数不超过 8？
- [ ] 是否只有一个金色高亮块，段落均不超过 100 字？
- [ ] `ljg-card` 是否在文档通知后运行，且群聊私聊发 `senderId`、p2p 发 `chatId`？
- [ ] 附录每条 ljg 是否各用一个注明 Skill 名的独立 h2 包裹，原稿内部小标题是否降为 h3 未占用 h2？
- [ ] 附录每条 ljg 原稿是否做过排版加工（拆段≤100字、加粗、列表/表格、换行），而非原样照搬？
- [ ] ljg-card PNG 是否用文件校验（capture.js exit 0 + 文件存在 + size>0）确认，未调用 `view_image`？
