# long-read 输出协议

本文件是 Evidence、隔离产物、拼接与 Docx XML 成品的唯一数据边界。

## 1. Evidence

Evidence 只从原文提取，不读取用户画像、既有摘要或外部评价。

```json
{
  "metadata": {
    "title": "",
    "author": null,
    "source_url": "",
    "published_at": null,
    "genre": "",
    "word_count": 0
  },
  "claims": [
    {
      "id": "C1",
      "claim": "",
      "evidence": "",
      "evidence_type": "quote|data|example|reasoning",
      "confidence": "high|medium|low"
    }
  ],
  "facts": [],
  "data_points": [],
  "quotes": [],
  "uncertainties": [],
  "article_structure": []
}
```

硬约束：

- 作者或发布时间未知写 `null`，禁止猜测。
- 无法确认的数据进入 `uncertainties`。
- 原文没有的观点不得进入 `claims`。
- `quotes` 与 `evidence_type=quote` 的证据必须是正文连续子串。
- 原文金句最多 8 条；普通文章最多 5 条，只有确有足够密度时使用 6~8 条。

## 2. 隔离输入与输出

### article-decode

输入仅包括完整原文与 Evidence。输出为自由 Markdown，遵循 `article-decode/SKILL.md`，不得强塞 JSON schema，以免压平语言。

### 文字 ljg

每条输入仅包括完整原文、Evidence、唯一分析问题和自身 Skill。每条输出独立 Markdown；不同 Skill 彼此不可见。

### 临时产物

隔离产物可暂存为 `.wx_decode.md`、`.wx_ljg_*.md`。只用于主 Agent 拼接，交付后清理，不进入长期状态。

## 3. 拼接协议

拼接器执行“摘取后排序”，不执行统一改写：

1. 从 `article-decode` 摘取真正的核心、三重世界、与作者对话和最值得深读之处；
2. 每条文字 ljg 的完整原稿用一个独立 h2 标题包裹后放入附录（总量不限字数）；h2 标题必须注明该条使用的 Skill（格式「Skill 名 · 简短定位」，如「ljg-think · 追本之箭」），是该 ljg 在附录的唯一边界标记；ljg 原稿内部的小标题一律降为 h3 及以下，不得占用 h2；每条原稿内部仍受第4节全部可读性约束：单段≤100字、关键概念加粗、并列用列表、对比用表格、独立段落间换行；
3. 相同结论只保留最锋利、证据最强的一处；
4. 只补必要连接语，不磨平原 Skill 的语气。排版加工（拆超长段、加粗关键概念、并列项列表化、对比项表格化、独立段落换行）不属于改写，允许且应当对附录原稿执行；禁止的是改写语义与统一语气；
5. 删除能套在无关文章上的泛化句；
6. 用户画像只允许生成可选的 50 字个性化段。

主文约 1000~2000 字。附录放每条文字 ljg 完整原稿，不限字数；附录标题后、各 ljg 前先放 200~500 字导言导读附录内容。

## 4. Docx XML 成品

创建前读取当前 `lark-doc` Skill 的 XML、style、create workflow。默认写 `.wx_doc.xml`，不要退回 Markdown。

### 固定顺序

```text
标题
评分表
唯一金色核心结论
文章真正的核心
基石 / 边缘 / 暗流
与作者对话
最值得深读之处
可选：对飞鱼的意义（<=50 字）
原文金句 + 原文链接
附录：独立深度分析
必要事实（若有，文末）
```

附录 h1 标题后先放 200~500 字导言（普通段落，可分多段），再按 Skill 顺序放各文字 ljg 完整原稿。每条 ljg 用一个独立 h2 标题包裹，标题必须注明使用的 Skill（格式「Skill 名 · 简短定位」，如「ljg-think · 追本之箭」）；该 h2 是这条 ljg 在附录的唯一边界标记，不得与导言或其他 ljg 混排。ljg 原稿内部的小标题一律降为 h3 及以下，禁止占用 h2，避免与 Skill 边界标题混淆。导言与每条 ljg 原稿均受下文可读性约束（单段≤100字等），原稿只做排版加工不改语义。

禁止出现：

- 「骨架」章节；
- 独立「X 光四层」章节；
- 第二个金色高亮块；
- ljg-card 图片或占位章节。

### 顶部评分

用两列表格，展示 content-scoring 的评分结果，不自行评分：

```xml
<table>
  <colgroup><col width="90"/><col width="360"/></colgroup>
  <thead><tr><th background-color="light-gray">维度</th><th background-color="light-gray">判断</th></tr></thead>
  <tbody>
    <tr><td><b>质量</b></td><td>8.5/10 · 完整深读：简短证据依据</td></tr>
    <tr><td><b>依据</b></td><td>来自 content-scoring 的维度证据与风险扣分</td></tr>
  </tbody>
</table>
```

分数与档位来自 link-card 传入的 `scoring_result`（`final_score` + `decision_label`），long-read 不得重新评分。

### 唯一核心结论

全文只允许一个 `light-yellow` callout：

```xml
<callout emoji="⭐" background-color="light-yellow" border-color="yellow">
  <p><b>文章最重要的一刀：</b>一句不可替换的核心判断。</p>
</callout>
```

黄色只表达“全文最高优先级”，不作装饰。风险可在普通段落内少量使用 `<span text-color="red">`，不得再建彩色 callout。

### 可读性

以下约束全文生效，含主文、导言与附录每条 ljg 原稿；附录原稿同样要拆段、加粗、列表化、表格化与换行，不得原样照搬。

- 单个 `<p>` 的可见文本不超过 100 个中文字符；超出拆成多个相邻段落。
- 真正并列的短项用 `<ul>` 或 `<ol>`；叙述与论证保留连贯短段落。
- 只有真实行列数据或明确对比才用表格，最多 4 列。
- 原文金句逐条用 `<blockquote>`，不得改写。
- 核心概念加粗；颜色只表达稳定语义。
- 标题只给真实章节，不把每个句子升成标题。
- 要多使用换行、空行，针对完全独立的段落或章节，提高可读性
- ljg-thinking 每个要点间都要换行
### 个性化

「对飞鱼的意义」不是必选章节。只有文章解码之外仍有明确新增价值时才输出，正文可见文本最多 50 字。不得把该段内容回填到三重世界或作者动机。

## 5. 评分与深度映射

评分由 content-scoring 在 link-card 阶段完成，long-read 消费 `scoring_result.final_score`：

| final_score | 文字 ljg | ljg-card |
|-------------|----------|----------|
| `<8.0` | 0~1 | 不强制 |
| `8.0~8.4` | 1 | 文档交付后运行 |
| `8.5~8.9` | 1~2 | 文档交付后运行 |
| `>=9.0` | 2~3 | 文档交付后运行 |

文字 Skill 只在有独立问题时取上限。`ljg-card` 不计入文字数量。完整决策阈值与路由见 `content-scoring/SKILL.md`。
