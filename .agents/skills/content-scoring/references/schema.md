# Content Scoring v3.18 Schema

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

## scoring_result v3.18

权威解析输入是 `identity_packet v1`：仅含标题、作者/机构、实体、事件提示、通用主题和受控出处候选；搜索观察仅含查询 hash、最多四条公开 URL 的短证据、实体/主题判断及工具状态，不得含正文、用户上下文或原始查询。

```json
{
  "score_version": "3.18",
  "policy_source": "base|local",
  "quality_version": "3.16",
  "relevance_version": "3.0",
  "content_fingerprint": "sha256",
  "context_fingerprint": "sha256 或 null",
  "score_status": "scored|needs_relevance|needs_full_text|needs_review",
  "quality_score": 8.0,
  "quality_confidence": "high|medium|low",
  "importance_score": 9.0,
  "importance_confidence": "high|partial|unavailable",
  "authority_status": "verified|corroborated|inferred|source_missing|fetch_failed|mismatch|rejected",
  "authority_reason_code": "authority_verified",
  "importance_dimensions": {"authority_score": 9.0, "problem_significance_score": 9.0, "authority_status": "verified", "authority_reason_code": "entity_expertise_topic_verified", "authority_confidence": "high", "topic_match": "strong", "evidence": []},
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

权威分可用时，`importance_score = round1((authority_score + problem_significance_score) / 2)`；权威分不可用时，`importance_score = problem_significance_score`，即把完整的 30% 重要性权重交给大问题思考。随后统一计算 `base_priority = round1(0.70 * quality_score + 0.30 * importance_score)`，最终 `decision_score = round1(base_priority + relevance_score + interest_score)`。质量分仍只由三维内容质量计算，`quality_floor=6.0` 是路由硬门槛；`quality_label` 只按 `quality_score`，而路由和精读深度按 `decision_score`。

来源核验产物使用 `schema_version:"3.18"`、可空的 `authority_score`、`authority_confidence`、`entity`、`topic_match`、受控 `evidence[]`、`search_observation`、`reason_code` 和 `rationale`。`inferred` 分数由脚本封顶 8 且置信度为 `low`；百度百科必须有正规渠道交叉。出处缺失、访问失败、身份不匹配或安全拒绝时权威分保持空值，但不阻断大问题分和重要性分。

`authority_status` 为 `source_missing`、`fetch_failed`、`mismatch` 或 `rejected` 时，`authority_score` 必须为 `null`；状态与分数矛盾的产物会被拒绝并回退到大问题分。

```json
{
  "schema_version": "3.18",
  "authority_score": 9.0,
  "authority_status": "verified",
  "authority_confidence": "high",
  "entity": {"type": "person", "canonical": "示例人物", "ambiguity": false},
  "topic_match": "strong",
  "evidence": [
    {"url": "https://example.com", "title": "人物正式简介", "source_level": "official", "evidence_kind": "identity", "excerpt": "最多 200 字短证据", "verified": true},
    {"url": "https://example.com/interview", "title": "一手采访", "source_level": "reputable_secondary", "evidence_kind": "expertise", "excerpt": "最多 200 字短证据", "verified": true}
  ],
  "search_observation": {"query_count": 3, "result_count": 2, "tool_status": "ok"},
  "reason_code": "entity_expertise_topic_verified",
  "attempts": 3,
  "elapsed_ms": 420,
  "rationale": "实体、专业方向与主题匹配，并有可核验资料"
}
```

权威核验产物的 `evidence[]` 使用 `evidence_kind/title`。评分脚本在信任边界一次性兼容旧消费者的 `kind/label` 别名，但新生产者不得继续生成旧字段。

`chatgpt_munger_doc` 仅在 `score_status=scored`、`route=long_read` 且运行时芒格后处理门槛满足时为 true；其他状态固定为 false。消费者只消费该字段，不复制门槛。

`needs_relevance` 是质量已确定、只有相关性可改变路由时的内部暂停态。它可携带 `quality_score`，但 `decision_score`、`route`、`ljg_range` 必须为 null，不得发卡或分派。完成相关性，或用 `--relevance-unavailable` 明确不可用后，才会返回 `scored`。

`quality_score < quality_floor` 的低质量文章不运行相关性：`relevance_score=null`、`interest_score=null`、`context_fingerprint=null`；重要性仍可按上述权重计算，但 `route` 仍要求原始 `quality_score ≥ quality_floor`，防止低质量文章靠重要性或相关性加分晋级。`quality_label` 按质量分档生成、`priority_label=未计算（不影响本次路由）`、`interest_label=未计算（不影响本次路由）`。`quality_score ≥ quality_floor` 的文章一律计算相关性；`ljg_range`、`ljg_card` 与 `long_read_threshold` 按 `decision_score` 判断。

`relevance_score` 语义为 `quality_score ≥ quality_floor` 文章的 relevance 轴生效分（0 到 `relevance_bonus.relevance_max`，0.5）；`interest_score` 为兴趣轴生效分（0 到 `relevance_bonus.interest_max`，0.5）。权威性不可用时由大问题分承接 30% 权重并记录具体状态；相关性不可用时只回退到 `base_priority`。

旧版“决策分等同质量分”的描述不适用于 v3.18：权威性可用时按双轴公式计算；权威性缺失时由大问题分承接完整重要性权重，`inferred` 始终显式区别于已核验。

指纹由脚本生成：正文先做 Unicode NFC、统一换行、折叠连续空白，并消除中英文相邻处的纯排版空格，再与 `quality_version` 计算 SHA-256；相关性指纹再加入规范化后的 YWNext `runtime/core-context/full.md` 与 `relevance_version`。只有同时持有相同版本和对应评分产物时才允许复用。

## CLI

```bash
python3 scripts/content_scoring.py quality_output.json source.md
python3 scripts/content_scoring.py quality_output.json source.md --relevance-unavailable
python3 scripts/content_scoring.py quality_output.json source.md \
  --retry-quality-output retry.json \
  --relevance-output relevance_output.json \
  --context /Users/yuwei/code/skills/ywnext/runtime/core-context/full.md \
  --config-from-base <run_dir>/base-config.json
```

`--config-from-base` 在 Base 快照存在时必须传入；`needs_relevance` 的第二次运行必须复用同一快照。`--relevance-output` 与 `--context` 必须同时提供，且不得与 `--relevance-unavailable` 同时使用。脚本不负责调用模型、刷新 YWNext 或持久化缓存。
