# long-read 路由、隔离调度与交付

## 1. 来源适配

| 来源 | 方法 |
|------|------|
| GitHub 仓库 URL | 深度走 `learn`，明确快速总结走 `summarize`；不走文章解码 |
| `mp.weixin.qq.com` | `wechat-article-to-markdown` |
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

文字数量由 content-scoring 的 `scoring_result.final_score` 决定（见 `content-scoring/SKILL.md`）：

- `<8.0`：0~1；
- `8.0~8.4`：1；
- `8.5~8.9`：1~2；
- `>=9.0`：2~3。

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

每个隔离任务必须通过 SubAgent、fresh thread 或运行时提供的等价 fresh-context 机制执行，只获得：完整原文、Evidence、唯一问题、自身 Skill。禁止传入：

- 用户画像；
- `article-decode` 或其他 ljg 的结果；
- 主 Agent 的预设结论；
- 期望答案或本轮问题诊断。

`article-decode` 同样独立，只获得原文、Evidence 和自身 Skill。隔离任务可以并行；容量不足时分批运行，但上下文边界不变。没有隔离机制时跳过对应深度产出并标记降级，禁止在主上下文中模拟多个 Skill。

## 5. 文档拼接

主 Agent 串行维护 `.wx_doc.xml`：

1. 先从 `article-decode` 选择主文；
2. 对照所有文字 ljg 删除重复结论；
3. 每条 ljg 完整原稿放入附录，不限字数；
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
# 原群（group 场景发群；p2p 场景即私聊会话，同一命令只发一次）
lark-cli im +messages-send --as bot --chat-id <chatId> \
  --msg-type interactive --content "$(cat /tmp/link_card.json)" \
  --jq '.data.message_id'
```

只有取得主文档 URL 且主交付完成后，才能进入 ljg-card。

### ljg-card 后置任务

质量 `>=8.0` 时：

1. 独立读取并运行 `ljg-card` Skill；
2. 生成 PNG 并验证文件存在且非空；
3. 从 PNG 所在目录执行，使用相对路径仅私聊发送：

```bash
lark-cli im +messages-send --as bot --user-id <senderId> --image ./<name>.png
```

禁止：

- 在主文档创建前运行 ljg-card；
- 把图片插入文档；
- 向原群发送图片；
- 图片失败后重发或修改已交付的文档。

## 7. 降级

- 抓取失败：说明失败，不编造正文。
- `article-decode` 失败：保留 Evidence 和一句话客观摘要交付。
- 单条文字 ljg 失败：跳过该附录，继续交付其他结果。
- 文档创建失败：回退为高密度卡片。
- 原群卡片发送失败：记录失败，不重复发送。
- ljg-card 生成或私聊发送失败：主文档保持成功，不补发到群。

## 8. 临时文件

按实际使用清理：

- `.wx_tmp.md`
- `.wx_evidence.json`
- `.wx_decode.md`
- `.wx_ljg_*.md`
- `.wx_doc.xml`
- `/tmp/link_card.json`
- `/tmp/ljg_cast_*.html`

不要删除与本轮无关的既有文件。
