# Content Scoring v3.2 Schema

数值、权重、维度分集合和阈值以 `scoring-policy.json` 为唯一真值。本文件只定义模型与脚本的契约。

## quality_output v3.2

正文不完整时只要求 `schema_version`、`source_status` 和可用的结论；其余字段可省略。正文完整时使用完整结构：

```json
{
  "schema_version": "3.2",
  "source_status": "complete",
  "detected_domain": {"primary": "AI/Agent 工程", "secondary": "软件工程"},
  "claim_ledger": [
    {
      "id": "C1",
      "type": "causal",
      "importance": "core",
      "claim": "作者提出的主张",
      "source_quote": "正文中的连续原文",
      "support": "direct",
      "uncertainty": null
    }
  ],
  "calibration": {
    "closest_anchor": "A7",
    "at_least_seven": true,
    "comparison": "相对 A7 的具体强弱与证据"
  },
  "dimensions": {
    "evidence_quality": {
      "score": 8.0,
      "claim_ids": ["C1"],
      "rationale": "达到该分数的依据",
      "ceiling_reason": "不能更高的原因"
    },
    "insight_explanatory": {},
    "transfer_durability": {},
    "information_efficiency": {}
  },
  "domain_confidence": "high",
  "conclusion": "一句话结论",
  "questions": ["供深度分析使用的独立问题"]
}
```

约束：

- `source_status`：`complete|partial|unknown`。
- `claim_ledger`：标准正文按论证结构只取 5、8、12 条；不足 1000 字时取 2～5 条。ID 唯一；`source_quote` 必须是 `source.md` 连续子串，优先取正文内部 12～40 个字符并避开易转写标点。
- `type`：`empirical|causal|experiential|normative|method`。
- `importance`：`core|supporting`；`support`：`direct|partial|asserted`。
- 四个质量维度必须且只能完整出现；维度分必须来自 policy，6 分以上只用 0.5 步长。
- 每维至少引用一条存在的 claim，并填写理由和上限原因。
- `closest_anchor` 只能为本次匿名锚点视图中的 A1～A6；编号不承诺跨文章稳定。
- `domain_confidence`：`high|medium|low`。

## relevance_output v2

```json
{
  "schema_version": "2.0",
  "score": 0.8,
  "matched_mainlines": ["AI 产业认知"],
  "rationale": "命中程度与依据",
  "confidence": "high",
  "conclusion": "相关性结论"
}
```

约束：

- `score` 是 0 到 `relevance_bonus.max` 的数值，按内容对飞鱼元主线的命中程度给分；脚本 clamp 到 `[0, max]`。
- `score>0` 时 `matched_mainlines` 非空，列出命中的元主线名。
- 判定看内容吻合度与相关性，不以作者身份/名气单独判断。
- `confidence=low` 时脚本不采用相关性，决策分回退质量分。
- 该输出不得接收或复述质量分；`schema_version != 2.0` 的旧输出一律拒绝。

## scoring_result v3.2

```json
{
  "score_version": "3.2",
  "quality_version": "3.2",
  "relevance_version": "2.0",
  "content_fingerprint": "sha256",
  "context_fingerprint": "sha256 或 null",
  "score_status": "scored|needs_relevance|needs_full_text|needs_review",
  "quality_score": 8.6,
  "quality_confidence": "high|medium|low",
  "relevance_score": 0.8,
  "relevance_confidence": "high|medium|unavailable",
  "decision_score": 8.6,
  "quality_label": "完整深读",
  "priority_label": "相关",
  "route": "card|long_read|null",
  "ljg_range": [1, 2],
  "ljg_card": true,
  "claims": [],
  "quality_dimensions": {},
  "relevance_dimensions": {"score": 0.8, "matched_mainlines": [], "rationale": ""},
  "calibration": {},
  "conclusion": "",
  "questions": [],
  "issues": []
}
```

`needs_full_text` 与 `needs_review` 时，三个分数为空、`route=card`、深度字段为空或 false。消费者不得自行补分。

`needs_relevance` 是质量已确定、只有相关性可改变路由时的内部暂停态。它可携带 `quality_score`，但 `decision_score`、`route`、`ljg_range` 必须为 null，不得发卡或分派。完成相关性，或用 `--relevance-unavailable` 明确不可用后，才会返回 `scored`。

非边界文章不运行相关性：`relevance_score=null`、`context_fingerprint=null`、`decision_score=quality_score`、`priority_label=未计算（不影响本次路由）`。

`relevance_score` 语义为边界文章的生效 bonus（0 到 `relevance_bonus.max`），等于 clamp 后的 `score`；未计算或不可用时为 null。

指纹由脚本生成：正文先做 Unicode NFC、统一换行、折叠连续空白，并消除中英文相邻处的纯排版空格，再与 `quality_version` 计算 SHA-256；相关性指纹再加入规范化后的 YWNext `full.md` 与 `relevance_version`。只有同时持有相同版本和对应评分产物时才允许复用。

## CLI

```bash
python3 scripts/content_scoring.py quality_output.json source.md
python3 scripts/content_scoring.py quality_output.json source.md --relevance-unavailable
python3 scripts/content_scoring.py quality_output.json source.md \
  --retry-quality-output retry.json \
  --relevance-output relevance_output.json \
  --context /Users/yuwei/code/skills/ywnext/runtime/core-context/full.md
```

`--relevance-output` 与 `--context` 必须同时提供，且不得与 `--relevance-unavailable` 同时使用。脚本不负责调用模型、刷新 YWNext 或持久化缓存。
