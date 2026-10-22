# long-read 路由、隔离调度与交付

## 1. 来源适配

| 来源 | 方法 |
|------|------|
| GitHub 仓库 URL | 深度走 `learn`，明确快速总结走 `summarize`；不走文章解码 |
| `mp.weixin.qq.com` | 复用 link-card 前置抓取生成的 `source.md`，禁止再次抓取 |
| 飞书文档 URL | `lark-doc` |
| 其他网页 URL | `read` |
| 纯文本 | 直接使用 |

文章抓取后先识别文体，专项规则见 `genre-rules.md`。所有文章文体都不得绕过 Evidence。

## 2. Fast / Deep

### Fast

质量较低、文章结构简单或一次解码已经足够时：Evidence -> `article-decode` -> 完整成品。

Fast 不调用文字 ljg。内容短时可直接发卡片；需要承载完整 `article-decode` 时仍可生成飞书文档。

### Deep

符合评分数量或用户明确要求时：Evidence -> `article-decode` + 文字 ljg 隔离运行 -> 拼接成文档。

文字数量直接使用 content-scoring 的 `scoring_result.ljg_range`；`ljg_card` 与 `chatgpt_munger_doc` 只作评分留档——芒格分支与核心内容图不设分数门槛，route=long_read 每篇必跑/必生成。本 Skill 不复制分档，也不使用相关性改变深度。

## 3. 文字 Skill 选择

先从 Evidence 找出互不重叠的二阶问题，再为每个问题选一个 Skill：

| 独立问题 | Skill |
|----------|-------|
| 单一概念、底层机制 | `ljg-think` |
| 多层结构、多维系统 | `ljg-learn` |
| 真实争议、利益冲突 | `ljg-roundtable` |
| 长因果链、逐问推进 | `ljg-qa` |
| 值得形成独立批评文章 | `ljg-writes` |
| 罕见概念或单词 | `ljg-word` |

搭配限制：

- `ljg-think` 与 `ljg-learn` 不同时选；
- `ljg-roundtable` 与 `ljg-qa` 不同时选；
- `ljg-word` 独立使用；
- 不为凑数量选择同一问题的不同写法。

核心内容图不属于本表，也不是文字 ljg；它由 `run_chatgpt_core_image.py` 基于拼接完成的全文生成，每篇必产出。

## 4. 持久接管与隔离生成

唯一生产入口是 `.agents/skills/long-read/scripts/long_read_workflow_cli.py`。在同一任务快照中提交原文、Evidence、`scoring_result`、评分卡凭据、可信触发上下文和选中的问题文件：

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

`--question` 可按实际选取重复 0–3 次。只有返回 `status=accepted` 才表示输入快照和任务记录均已持久化。重复入站由应用身份与来源消息 ID 去重；用户再次主动提交相同 URL 仍创建新任务。

持久 Worker 为文字分析和芒格分支保存独立检查点、请求 ID、提示词与输入哈希、Bridge 收据、输出哈希、失败分类和耗时。`article-decode`、各文字 ljg 与芒格分析继续使用独立 ChatGPT Bridge 会话；全局并发 ≤3 路、请求错开 ≥10 秒。输出须包含有效提交确认、`verification=live-dom+snapshot`、会话 URL 和匹配哈希；非零进程退出码视为失败。提交状态不明时只观察原请求，不自动重发。

模型输入边界保持不变：

- `article-decode` 只获得完整原文与客观 Evidence；
- 每条文字 ljg 只获得完整原文、客观 Evidence 和分配给它的唯一问题；
- 不传用户画像、评分解释、其他分析结果、预设结论或前序对话。

Evidence Schema 与逐字引文在提交前校验。确定性输出校验失败不重试。必跑分支失败时保留其他有效成果，但任务整体不得报告成功。

进度使用只读 `status`。`run-worker` 推进一个待处理任务；`resume --job-id <id>` 接续指定任务。部署后的受管常驻 Worker 使用 `run-worker --serve`，在同一进程内检查报告周期并推进长文任务。该模式要求报告私聊目标预先持久保存在状态库；缺少目标不能启动，运行时目标断言必须与保存值一致。恢复先只读核验未决副作用，已确认的分支和产物按哈希、请求 ID 与收据复用。

## 5. 程序装配

程序将通过校验的原文、Evidence、评分及各分支原稿装配成同一版本的 Markdown 与 Docx XML。`chatgpt-munger.md` 成功时作为一级主章节「芒格洞察」写入主文档，不创建第二篇文档。不得手工压缩、改写或删去成功分支的分析内容。

主文档结构与格式以 `SKILL.md` 和 `output-schema.md` 为准。生成收据同时绑定 Markdown/XML 哈希与输入版本；缺少收据、文件损坏或输入变化时不能复用旧文件。文档发布通过工作流内的 Document Compiler 适配器完成，复用稳定业务键、发布收据和只读恢复。发布状态不明且缺少文档 ID 时进入待核实，不换键重建。

## 6. 端到端交付与安全恢复

主文档通过发布和内容读回后可以先交付进度通知，但只能表述“主文档已交付，整体处理中”。卡片和任务状态由工作流总状态驱动，禁止从分析完成或单一消息成功推断最终成功。

每张必需图片按以下步骤推进并分别留存副作用意图、远端标识和读回结果：

1. Bridge 生成并核验图片字节及哈希；
2. 飞书创建图片块、上传素材、填充图片并精确读回块 ID、位置、图片 token 与图题；
3. 上传该图并单独发送给经验证的本人私聊，再按消息 ID 或完整近期历史读回确认。

同一图片的生成、插入与发送共享最多两轮额外恢复，正常间隔至少 240 秒。每轮从失败阶段恢复；不可恢复错误立即停止。丢响应或读回不完整时进入只读核验；无法唯一确认时保留待核实，不重复创建块或消息。缺少可信私聊目标、发送身份或频率预检数据时停止发送。

只有必跑文字分支、主文档发布与读回、主文档通知，以及每张必需图片的生成/插入/单独发送全部确认后，才能发送最终成功通知。任一必需结果失败或未知时明确报告缺失产物和阶段，保留持久状态供恢复；未知状态不能算成功。

不要直接调用 `lark-cli` 写文档、插图、发图片或写索引来绕过工作流。共享身份、消息频率、幂等、读回和持续发送时长门禁仍须满足。

## 7. 周期可靠性报告

报告使用同一持久数据库，以任务接管时间归属统计窗口。Asia/Shanghai 周日 09:00 检查此前连续 7 天；仅当明确最终失败占比 `F/N > 10%` 才发送，`N=0` 不触发。待核实和处理中计入 `N`，不计入明确失败分子。每月 1 日 09:00 必须发送上个自然月报告，无有效任务时也发送“无有效任务”。

报告固定截止快照、范围、版本和数据水位，包含成功/失败/未结数量、阶段故障、恢复结果、人工介入、耗时、重复故障指纹与修复前后证据。月报和周报同日先月报；月报未确认交付时，不发送周报。每份报告由完整 Markdown 附件和 Card 2.0 摘要组成，两者都须读回确认。

先用 `configure-report-target` 只读验证当前用户身份、本人的 open_id、bot app_id、私聊类型和精确会话成员，再将报告目标存入受保护状态库；已有配置只有显式 `--replace` 才能更改。`run-worker --serve` 在长文 Worker 的每轮调度中检查报告周期；`run-due-reports` 用于只检查或补跑上线后漏掉的周期。每次投递前重新验证目标；不匹配或不可读时不执行任何写操作。投递时只读取保存目标，并拒绝与其不一致的运行时参数。报告发件箱结果不确定时只读核验，不能重发。周报是否发送由固定快照中的 `F/N > 10%` 决定，不按待核实项触发。

## 8. 旧临时路径

`run_long_read_pipeline.py` 和直接的 `lark-cli docs +media-insert`、`lark-cli im +messages-send` 命令只保留作历史实现或局部测试，不是生产接管和交付入口。不得把旧流水线的完成状态当作持久工作流完成状态，也不得从旧临时目录推断本轮任务成功。
