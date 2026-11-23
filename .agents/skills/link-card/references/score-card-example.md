# 评分卡 JSON 示例

> 本文件自 link-card/SKILL.md 移入，原文未改动；入口保留触发条件与索引。

```json
{
  "schema": "2.0",
  "header": {"title": {"tag": "plain_text", "content": "评分完成"}, "template": "indigo"},
  "body": {"elements": [{"tag": "markdown", "content": "《标题》\n\n**质量 {quality_score}/10 · {quality_label}**\n**重要性 {importance_score}** · 大问题思考 {problem_significance_score} · 相关性 {relevance_score 或 不可用} · 兴趣 {interest_score 或 不可用} · 决策 {decision_score}/10\n**三维质量**\n证据与论证 {quality_dimensions.evidence_quality.score} · 洞察解释 {quality_dimensions.insight_explanatory.score} · 长期迁移 {quality_dimensions.transfer_durability.score}\n{scoring_result.conclusion}\n\n正在精读，稍后发文档。"}]}
}
```
