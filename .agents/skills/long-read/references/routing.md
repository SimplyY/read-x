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

质量较低、文章结构简单或一次解码已经足够时：Evidence -> `article-decode` -> 精简成品。

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

## 4. 隔离协议

主 Agent 选定互不重复的问题和文字 Skill 后，只调用 `scripts/run_long_read_pipeline.py`；编排入口并行独立拉起分析分支（内部运行 `scripts/run_isolated_analyses.py`）与 ChatGPT 芒格分支（`scripts/run_chatgpt_munger.py`，每篇必跑）。脚本严格校验 Evidence Schema 与原文逐字引文、预检所有输入和 Skill，再用 `ThreadPoolExecutor` 为 `article-decode` 和 0~3 个文字 ljg 分别经现有 Ego Lite ChatGPT web-bridge 发起独立会话（每个任务一个 `chatgpt.com/c/new` 新会话，互不可见）；全局并发 ≤3 路、会话发起错开 ≥10 秒由 Bridge 槽位与错峰器保证，Bridge 忙时按给定等待时间有界排队。外部 ljg 的完整 Skill 若要求 shell、引用文件、交互或本地写入，脚本追加固定运行覆盖，跳过这些不可用动作并直接返回最终 Markdown；命令、路径或交付残留会被生产门禁拒绝且不落盘。禁止传入：

- 用户画像；
- `article-decode` 或其他 ljg 的结果；
- 主 Agent 的预设结论；
- 期望答案或本轮问题诊断。

`article-decode` 只获得原文、Evidence 和自身 Skill；每条 ljg 额外获得自己的唯一问题。主 Agent 不读取这些 Skill，不生成任务原稿。脚本输出按声明顺序编号，使用临时文件加原子替换，并把完整结果写入本轮 `summary.json`；单条 ljg 失败不影响其他结果。脚本或 ChatGPT web-bridge 不可用时按第 7 节降级，禁止退回主上下文、SubAgent、fresh thread 或嵌套 `codex exec`。

## 5. 文档拼接

主 Agent 串行维护 `.wx_doc.xml`；ChatGPT Bridge 芒格结果必须先落为本轮临时 Markdown，再作为一级主章节「芒格洞察」拼进主文档，**禁止创建第二篇文档**，也禁止把模型纯文本直接拼进文档。编排层只传递原文、实时读取并冻结的 `read-x.munger-analysis` 资产（正文内嵌芒格之魂全文，独立成篇）和最小边界，不把固定八标题或其他外层模板塞进模型请求：

1. 先从 `article-decode` 选择主文；
2. 对照所有文字 ljg 删除重复结论；
3. 每条 ljg 完整原稿放入附录，每条 600-1000 字；
4. 拆分超过 100 字的段落；
5. 按 `output-schema.md` 转成 XML。

不得让第二个 Agent 接管全文润色；全文只由主 Agent拼接，避免再次平均化语言。

## 6. 文档与卡片交付

### 主文档

```bash
lark-cli docs +create \
  --content @.wx_doc.xml \
  --parent-position my_library \
  --as bot
```

创建前必须读取当前 CLI 内置的 `lark-doc` XML、style、create workflow。开发验证使用 `--dry-run`，不得创建测试文档。

文档成功后必须用 link-card 生成的 `<run_dir>/score-gate.json` 和同一轮 `<run_dir>/scoring-result.json` 生成并校验交付卡 JSON，再执行：

```bash
# 群聊场景私聊触发者
lark-cli im +messages-send --as bot --user-id <senderId> \
  --msg-type interactive --content "$(cat /tmp/link_card.json)" \
  --jq '.data.message_id'

# p2p 场景使用当前私聊 chatId，只发一次
lark-cli im +messages-send --as bot --chat-id <chatId> \
  --msg-type interactive --content "$(cat /tmp/link_card.json)" \
  --jq '.data.message_id'
```

主文档创建与核心内容图生成并行进行。ChatGPT Bridge 芒格结果属于主文档交付前的后处理，成功时作为一级主章节「芒格洞察」拼入主文档，不创建第二篇文档；失败时保留主文档交付。交付卡必须由 `scripts/render_long_read_delivery_card.py` 生成，经 JSON 解析后发送；发送后读回消息确认没有字面量 `\\n`。

### 核心内容图

拼接完成的全文落盘 `<run_dir>/main-doc.md` 后，与文档创建并行运行：

```bash
python3 .agents/skills/long-read/scripts/run_chatgpt_core_image.py \
  --source <run_dir>/main-doc.md \
  --output <run_dir>/core-image.png \
  --summary <run_dir>/core-image-summary.json
```

脚本经 Ego Lite ChatGPT web-bridge image 模式生成一张 16:9 核心内容信息图；输出必须通过 `verification=live-dom+snapshot`、有效会话 URL 与 `outputSha256` 校验。图片与文档都就绪后插入文档顶部（标题后、原文链接前）：

```bash
lark-cli docs +media-insert --as bot --doc <文档URL> \
  --file <run_dir>/core-image.png \
  --selection-with-ellipsis <原文链接段落文本> \
  --before --caption 核心内容图 --align center
```

禁止：

- 在主文档 XML 创建校验前生成或插入图片；
- 插入失败后自动重试插入（防止重复插入）；
- 对同一轮文章生成第二张核心内容图。

### PNG 私聊发送

核心图 PNG 以 bot 身份私聊发给触发者，按 `chatType` 只执行一条、只发一次（禁止同时执行 `--chat-id` 与 `--user-id`）：

- p2p：`lark-cli im +messages-send --as bot --chat-id <bridge_context.chatId> --image <run_dir>/core-image.png`
- 群聊：`lark-cli im +messages-send --as bot --user-id <bridge_context.senderId> --image <run_dir>/core-image.png`
- `senderType=bot`：回退 `--chat-id` 发原群

## 7. 降级

- 抓取失败：说明失败，不编造正文。
- `article-decode` 或执行脚本失败：保留已成功的文字 ljg 输出和 Evidence，主文档降级交付并在交付内容中明确列出缺失的分析分支；ChatGPT 分支不受影响照常运行；禁止角色扮演回退。
- 单条文字 ljg 失败：跳过该附录，继续交付其他结果；该分支的每次尝试错误记录保留在 `pipeline-summary.json`。
- ChatGPT 分支失败：主文档按「主精读完成，ChatGPT 待复核」交付，整轮不得标成「精读完成」。
- 核心内容图生成失败：主文档照常交付（无图），交付卡注明「核心图待复核」，不发送 PNG，禁止自动重发。
- 图片插入失败：主文档照常交付，交付卡注明「核心图待插入」，PNG 仍私聊发送一次，禁止自动重试插入。
- 文档创建失败：回退为高密度卡片。
- 卡片发送失败：记录失败，不重复发送。

## 8. 临时文件

每次消息先创建独立的 `/tmp/readx-longread.XXXXXX`，在其中保存 `source.md`、`evidence.json`、问题文件和 `analyses/*.md`；交付后只清理本轮目录。其他产物按实际使用清理：

- `.wx_tmp.md`
- `.wx_evidence.json`
- `.wx_doc.xml`
- `main-doc.md`
- `/tmp/link_card.json`
- `core-image.png`、`core-image-summary.json`
- `chatgpt-munger.md`、`chatgpt-munger-summary.json`

不要删除与本轮无关的既有文件。
