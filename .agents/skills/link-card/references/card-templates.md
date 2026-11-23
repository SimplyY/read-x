# 卡片模板（完整 JSON）

> 本文件自 link-card/SKILL.md 移入，原文未改动；入口保留触发条件与索引。

## [2] 卡片模板

所有卡片使用 CardKit 2.0 schema（`schema: "2.0"`），header 用 `indigo` 模板。

### 高质量内容卡片（long-read 状态通知）

long-read 的状态卡和全部文档/图片交付通知由持久工作流独占。入口只传递或展示工作流的已确认状态，不自行重新生成文档卡、图片或索引记录。

主文档可先交付，但通知必须说明“主文档已交付，整体处理中”。只有工作流确认所有必跑分支、主文档、每张必需图片的插入与私聊发送后，才可显示最终成功。失败、缺失或待核实状态必须保留具体阶段，不得使用“精读完成”标题。

芒格洞察成功时由工作流拼入主文档并在最终卡注明；失败时主文档仍可交付，但任务不得提前判成功。核心内容图每篇必需；芒格分支成功时芒格图也必需。图片失败不会取消剩余可用交付，但整体不能标记成功。

卡片使用 CardKit 2.0 和真实换行，由工作流渲染、发送并读回；入口不得手工拼 JSON、重复发送或从分析完成推断交付成功。

> ⚠️ `ljg_card` 仅是评分留档字段，不是图片或额外通知的触发条件。

> ⚠️ **三档齐全门（硬性）**：`quality_score ≥ quality_floor`（6.0）的文章，相关性、兴趣两轴未算完（`relevance_score`/`interest_score` 为 `null`/「待计算」/「不可用」）时，不得提交 long-read 工作流或发送文档交付卡。先补算两轴，三档齐全后才继续。

long-read 的 Card 2.0 内容由持久 Worker 根据进度、最终状态和已确认产物生成。此处不提供可复用的成功卡模板，避免入口误发“精读完成”。

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

### 快速阅读卡（quality_label=快速阅读）

```json
{
  "schema": "2.0",
  "header": {
    "title": {"tag": "plain_text", "content": "文章标题"},
    "template": "indigo"
  },
  "body": {
    "elements": [
      {
        "tag": "markdown",
        "content": "**作者/来源** · 平台 · 时间\n\n---\n\n**一句话定位**：这篇文章在回答什么问题 / 核心张力\n\n**核心观点**\n1. 观点 + 一句论证\n2. 观点 + 一句论证\n3. 观点 + 一句论证\n\n**值得记住**\n- 具体例子/案例/数据 1\n- 具体例子/案例/数据 2\n\n**可迁移**：这对我/我们能借鉴什么（有才写）\n\n---\n\n💬 金句\n> 原文金句 1\n> 原文金句 2\n\n[阅读原文](链接)"
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


---

## 速读卡结构（quality_label=快速阅读，入口分派命中后按此提炼）

速读不是简略，是高密度的全文浓缩：让读者在手机上不点原文，也能吸收这篇 7 分文章的真正价值。速读卡结构（自上而下）：

1. **一句话定位**：这篇文章在回答什么问题 / 核心张力。让读者先抓住「它到底在讲什么」。
2. **核心观点 3-5 条**：每条观点不是标签式罗列，而是带一句论证或原文支撑，保留文章自身的推理逻辑。宁可少而准，不要空泛堆砌。
3. **值得记住的细节/例子 1-2 个**：具体的案例、数据、画面，让观点落地、有记忆点。
4. **可迁移启发 1 句**（有才写，不强凑）：这对我/我们能借鉴什么。
5. **金句**（必选，见下方金句规则）：保留原文值得划线的表达。
6. **阅读原文链接**。

速读卡字数控制在 200-400 字（评分块不计入），信息密度优先；不要因为它「不需要精读」就只给三五行。若文章确实只有 1 个值得记的观点，就只写 1 个，不为了凑数拉长。
