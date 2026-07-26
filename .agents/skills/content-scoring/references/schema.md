# Content Scoring v3 Schema

数值、权重、等级映射和阈值以 `scoring-policy.json` 为唯一真值。本文件只定义模型与脚本的契约。

## quality_output v3

正文不完整时只要求 `schema_version`、`source_status` 和可用的结论；其余字段可省略。正文完整时使用完整结构：

```json
{
  "schema_version": "3.0",
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
      "grade": "strong",
      "claim_ids": ["C1"],
      "rationale": "达到该等级的依据",
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
- `claim_ledger`：条数由 policy 约束；ID 唯一；`source_quote` 必须是 `source.md` 连续子串。
- `type`：`empirical|causal|experiential|normative|method`。
- `importance`：`core|supporting`；`support`：`direct|partial|asserted`。
- 四个质量维度必须且只能完整出现；等级必须来自 policy。
- 每维至少引用一条存在的 claim，并填写理由和上限原因。
- `closest_anchor` 只能为 A1～A7。
- `domain_confidence`：`high|medium|low`。

## relevance_output v1

```json
{
  "schema_version": "1.0",
  "dimensions": {
    "current_mainline": {
      "grade": "strong",
      "context_sections": ["当前主线"],
      "rationale": "仅供内部计算的对应依据"
    },
    "current_tension": {},
    "long_term_alignment": {},
    "current_actionability": {}
  },
  "confidence": "high",
  "conclusion": "相关性结论"
}
```

约束：

- 四个相关性维度必须且只能完整出现；等级必须来自 policy。
- `context_sections` 非空，只能引用 `当前主线|当前张力|长期校准|暂不做什么`。
- `confidence=low` 时脚本不采用相关性数字。
- 该输出不得接收或复述质量分。

## scoring_result v3

```json
{
  "score_version": "3.0",
  "quality_version": "3.0",
  "relevance_version": "1.0",
  "content_fingerprint": "sha256",
  "context_fingerprint": "sha256 或 null",
  "score_status": "scored|needs_full_text|needs_review",
  "quality_score": 8.6,
  "quality_confidence": "high|medium|low",
  "relevance_score": 8.2,
  "relevance_confidence": "high|medium|unavailable",
  "decision_score": 8.6,
  "quality_label": "完整深读",
  "priority_label": "高度相关",
  "route": "card|long_read",
  "ljg_range": [1, 2],
  "ljg_card": true,
  "claims": [],
  "quality_dimensions": {},
  "relevance_dimensions": {},
  "calibration": {},
  "conclusion": "",
  "questions": [],
  "issues": []
}
```

`needs_full_text` 与 `needs_review` 时，三个分数为空、`route=card`、深度字段为空或 false。消费者不得自行补分。

指纹由脚本生成：正文先做 Unicode NFC、统一换行、折叠连续空白，并消除中英文相邻处的纯排版空格，再与 `quality_version` 计算 SHA-256；相关性指纹再加入规范化后的 YWNext `full.md` 与 `relevance_version`。只有同时持有相同版本和对应评分产物时才允许复用。

## CLI

```bash
python3 scripts/content_scoring.py quality_output.json source.md
python3 scripts/content_scoring.py quality_output.json source.md \
  --retry-quality-output retry.json \
  --relevance-output relevance_output.json \
  --context /Users/yuwei/code/skills/ywnext/runtime/core-context/full.md
```

`--relevance-output` 与 `--context` 必须同时提供。脚本不负责调用模型、刷新 YWNext 或持久化缓存。
