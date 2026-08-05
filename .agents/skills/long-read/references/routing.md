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

文字数量直接使用 content-scoring 的 `scoring_result.ljg_range`；是否生成图片直接使用 `scoring_result.ljg_card`。本 Skill 不复制分档，也不使用相关性改变深度。

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

`ljg-card` 不参与本表，也不参与文档生成。

## 4. 隔离协议

主 Agent 选定互不重复的问题和文字 Skill 后，只调用 `scripts/run_isolated_analyses.py`。脚本严格校验 Evidence Schema 与原文逐字引文、预检所有输入和 Skill、显式禁用环境代理，再用 `ThreadPoolExecutor` 为 `article-decode` 和 0~3 个文字 ljg 分别发送独立 MoonBridge 请求；每个请求固定 `model=glm-5.2`、`store=false`，不传会话 ID 或前序响应 ID。外部 ljg 的完整 Skill 若要求 shell、引用文件、交互或本地写入，脚本追加固定运行覆盖，跳过这些不可用动作并直接返回最终 Markdown；命令、路径或交付残留会被生产门禁拒绝且不落盘。禁止传入：

- 用户画像；
- `article-decode` 或其他 ljg 的结果；
- 主 Agent 的预设结论；
- 期望答案或本轮问题诊断。

`article-decode` 只获得原文、Evidence 和自身 Skill；每条 ljg 额外获得自己的唯一问题。主 Agent 不读取这些 Skill，不生成任务原稿。脚本输出按声明顺序编号，使用临时文件加原子替换，并把完整结果写入本轮 `summary.json`；单条 ljg 失败不影响其他结果。脚本或 MoonBridge 不可用时按第 7 节降级，禁止退回主上下文、SubAgent、fresh thread 或嵌套 `codex exec`。

## 5. 文档拼接

主 Agent 串行维护 `.wx_doc.xml`：

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
  --parent-position my_library
```

创建前必须读取当前 CLI 内置的 `lark-doc` XML、style、create workflow。开发验证使用 `--dry-run`，不得创建测试文档。

文档成功后按 link-card 流程构建并校验 CardKit JSON，再执行：

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

只有取得主文档 URL 且主交付完成后，才能进入 ljg-card。

### ljg-card 后置任务

`scoring_result.ljg_card=true` 时：

1. 独立读取并运行 `ljg-card` Skill；
2. 生成 PNG 并验证文件存在且非空；
3. 从 PNG 所在目录执行，使用相对路径私聊发给触发者，按 `chatType` 只执行一条、只发一次（禁止同时执行 `--chat-id` 与 `--user-id`）：

- p2p：`lark-cli im +messages-send --as bot --chat-id <bridge_context.chatId> --image ./<name>.png`
- 群聊：`lark-cli im +messages-send --as bot --user-id <bridge_context.senderId> --image ./<name>.png`
- `senderType=bot`：回退 `--chat-id` 发原群

禁止：

- 在主文档创建前运行 ljg-card；
- 把图片插入文档；
- 图片失败后重发或修改已交付的文档。

## 7. 降级

- 抓取失败：说明失败，不编造正文。
- `article-decode` 或执行脚本失败：保留 Evidence 和一句话客观摘要交付，禁止角色扮演回退。
- 单条文字 ljg 失败：跳过该附录，继续交付其他结果。
- 文档创建失败：回退为高密度卡片。
- 卡片发送失败：记录失败，不重复发送。
- ljg-card 生成或发送失败：主文档保持成功，不重复发送。

## 8. 临时文件

每次消息先创建独立的 `/tmp/readx-longread.XXXXXX`，在其中保存 `source.md`、`evidence.json`、问题文件和 `analyses/*.md`；交付后只清理本轮目录。其他产物按实际使用清理：

- `.wx_tmp.md`
- `.wx_evidence.json`
- `.wx_doc.xml`
- `/tmp/link_card.json`
- `/tmp/ljg_cast_*.html`

不要删除与本轮无关的既有文件。
