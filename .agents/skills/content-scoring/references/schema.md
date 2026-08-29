# Content Scoring v3.16 Schema

结构性数值、权重、维度分集合和阈值的本地基线来自 `scoring-policy.json`；运行时可调路由与档位字段可由已校验的 Base 快照覆盖。本文件只定义模型与脚本的契约。

## quality_output v3.16

正文不完整时只要求 `schema_version`、`source_status` 和可用的结论；其余字段可省略。正文完整时使用完整结构：

```json
{
  "schema_version": "3.16",
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
  "dimensions": {
    "evidence_quality": {
      "level": 7.5,
      "disqualifiers": [],
      "claim_ids": ["C1"],
      "rationale": "达到该分数的依据",
      "ceiling_reason": "不能更高的原因"
    },
    "insight_explanatory": {},
    "transfer_durability": {}
  },
  "problem_significance": {
    "level": 9.0,
    "claim_ids": ["C1"],
    "rationale": "影响范围广且具有长期决策杠杆",
    "ceiling_reason": "未覆盖全部系统边界"
  },
  "domain_confidence": "high",
  "conclusion": "一句话结论",
  "questions": ["供深度分析使用的独立问题"]
}
```

三个维度的 `claim_ids` 都必须至少包含一个当前 `claim_ledger` 的有效 ID。

约束：

- `source_status`：`complete|partial|unknown`。
- `claim_ledger`：标准正文按论证结构只取 5、8、12 条；不足 1000 字时取 2～5 条。ID 唯一；`source_quote` 必须是 `source.md` 连续子串，优先取正文内部 12～24 个字符并避开易转写标点。
- 直引号/弯引号差异仅在正文中唯一命中时回填原字符；其他不精确引用、零命中或多命中一律失败关闭。
- `type`：`empirical|causal|experiential|normative|method`。
- `importance`：`core|supporting`；`support`：`direct|partial|asserted`。
- 三个质量维度必须且只能完整出现；内部模型一次直接返回合法 `level`、原文 `unit_ids` 和硬反证。生成脚本组装逐字引用；评分脚本应用硬反证封顶后复制为 `score`。
- 每维至少引用一条存在的 claim，并填写理由和上限原因。
- 质量输出禁止包含 `calibration`；锚点和目标区间只用于评分后的外部闭卷回归。
- `domain_confidence`：`high|medium|low`。

## relevance_output v3

```json
{
  "schema_version": "3.0",
  "relevance_score": 0.5,
  "interest_score": 0.4,
  "matched_mainlines": ["AI 产业认知"],
  "matched_interests": ["价值投资"],
  "rationale": "命中程度与依据",
  "confidence": "high",
  "conclusion": "相关性结论"
}
```

约束：

- `relevance_score` 是 0 到 `relevance_bonus.relevance_max`（0.5）的数值，按内容对飞鱼元主线的命中程度给分；脚本 clamp 到 `[0, 0.5]`。
- `interest_score` 是 0 到 `relevance_bonus.interest_max`（0.5）的数值，按内容对飞鱼领域兴趣区块的命中程度给分；脚本 clamp 到 `[0, 0.5]`。两轴相加为总 bonus，自然 ≤ `relevance_bonus.max`（1.0）。
- `relevance_score>0` 时 `matched_mainlines` 非空；`interest_score>0` 时 `matched_interests` 非空。
- 判定看内容吻合度与相关性，不以作者身份/名气单独判断。
- `confidence=low` 时脚本不采用相关性，决策分回退质量分。
- 该输出不得接收或复述质量分；`schema_version != 3.0` 的旧输出一律拒绝。

## scoring_result v3.16

```json
{
  "score_version": "3.16",
  "policy_source": "base|local",
  "quality_version": "3.16",
  "relevance_version": "3.0",
  "content_fingerprint": "sha256",
  "context_fingerprint": "sha256 或 null",
  "score_status": "scored|needs_relevance|needs_full_text|needs_review",
  "quality_score": 8.0,
  "quality_confidence": "high|medium|low",
  "importance_score": 9.0,
  "importance_confidence": "high|medium|unavailable",
  "importance_dimensions": {"authority_score": 9.0, "problem_significance_score": 9.0, "evidence": []},
  "relevance_score": 0.5,
  "interest_score": 0.5,
  "relevance_confidence": "high|medium|unavailable",
  "base_priority": 8.3,
  "decision_score": 9.3,
  "quality_label": "稀缺精读",
  "priority_label": "相关",
  "interest_label": "高兴趣",
  "route": "card|long_read|null",
  "ljg_range": [2, 3],
  "ljg_card": true,
  "chatgpt_munger_doc": true,
  "claims": [],
  "quality_dimensions": {},
  "relevance_dimensions": {"relevance_score": 0.5, "interest_score": 0.5, "matched_mainlines": [], "matched_interests": [], "rationale": ""},
  "conclusion": "",
  "questions": [],
  "issues": []
}
```

`needs_full_text` 与 `needs_review` 时，三个分数为空、`route=card`、深度字段为空或 false。消费者不得自行补分。

`importance_score = round1((authority_score + problem_significance_score) / 2)`；`base_priority = round1(0.70 * quality_score + 0.30 * importance_score)`；最终 `decision_score = round1(base_priority + relevance_score + interest_score)`。质量分仍只由三维内容质量计算，`quality_floor=6.0` 是路由硬门槛；`quality_label` 只按 `quality_score`，而路由和精读深度按 `decision_score`。

来源核验产物使用 `schema_version:"3.16"`、`authority_score`、`evidence[]`、`confidence` 和 `rationale`。权威分 9 必须有至少两条 `verified:true` 证据且包含 `interview` 或 `primary_source`；核验缺失或失败时 `importance_score=null`、`importance_confidence="unavailable"`，记录 `importance_unavailable`，不造默认分。

```json
{
  "schema_version": "3.16",
  "authority_score": 9.0,
  "evidence": [
    {"kind": "publisher", "label": "专业出版物", "url": "https://example.com", "verified": true},
    {"kind": "interview", "label": "一手采访", "url": "https://example.com", "verified": true}
  ],
  "confidence": "high",
  "rationale": "出处链完整且存在可核验的一手材料"
}
```

`chatgpt_munger_doc` 仅在 `score_status=scored`、`route=long_read` 且运行时 ChatGPT 芒格门槛满足时为 true；其他状态固定为 false。消费者只消费该字段，不复制门槛。

`needs_relevance` 是质量已确定、只有相关性可改变路由时的内部暂停态。它可携带 `quality_score`，但 `decision_score`、`route`、`ljg_range` 必须为 null，不得发卡或分派。完成相关性，或用 `--relevance-unavailable` 明确不可用后，才会返回 `scored`。

`quality_score < quality_floor` 的低质量文章不运行相关性：`relevance_score=null`、`interest_score=null`、`context_fingerprint=null`，并直接以质量基线结束评分（不计算重要性/相关性加成）；`quality_label` 按质量分档生成、`priority_label=未计算（不影响本次路由）`、`interest_label=未计算（不影响本次路由）`。`quality_score ≥ quality_floor` 的文章一律计算相关性；`quality_label` 按 `quality_score`，`ljg_range`、`ljg_card` 与 `long_read_threshold` 按 `decision_score` 判断，`route` 仍要求原始 `quality_score ≥ quality_floor`，防止低质量文章靠加分晋级。`< quality_floor` 时双满档也拉不进精读。

`relevance_score` 语义为 `quality_score ≥ quality_floor` 文章的 relevance 轴生效分（0 到 `relevance_bonus.relevance_max`，0.5）；`interest_score` 为兴趣轴生效分（0 到 `relevance_bonus.interest_max`，0.5）。重要性不可用时取消 30% 加成并记录 issue；相关性不可用时只回退到 `base_priority`。

旧版“决策分等同质量分”的描述不适用于 v3.16：重要性可用时按上述双轴公式计算；重要性缺失时只回退质量基线。

指纹由脚本生成：正文先做 Unicode NFC、统一换行、折叠连续空白，并消除中英文相邻处的纯排版空格，再与 `quality_version` 计算 SHA-256；相关性指纹再加入规范化后的 YWNext `runtime/repo-context/read-x.md` 与 `relevance_version`。只有同时持有相同版本和对应评分产物时才允许复用。

## CLI

```bash
python3 scripts/content_scoring.py quality_output.json source.md
python3 scripts/content_scoring.py quality_output.json source.md --relevance-unavailable
python3 scripts/content_scoring.py quality_output.json source.md \
  --retry-quality-output retry.json \
  --relevance-output relevance_output.json \
  --context /Users/yuwei/code/skills/ywnext/runtime/repo-context/read-x.md \
  --config-from-base <run_dir>/base-config.json
```

`--config-from-base` 在 Base 快照存在时必须传入；`needs_relevance` 的第二次运行必须复用同一快照。`--relevance-output` 与 `--context` 必须同时提供，且不得与 `--relevance-unavailable` 同时使用。脚本不负责调用模型、刷新 YWNext 或持久化缓存。
