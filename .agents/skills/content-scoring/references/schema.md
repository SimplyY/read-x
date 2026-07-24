# content-scoring 输入输出 Schema

## model_output（模型输出，脚本输入）

模型只输出维度等级与证据、加分、扣分、结论与问题，不输出分数。

```json
{
  "detected_domain": {"primary": "投资/价值投资", "secondary": "系统思考"},
  "dimensions": {
    "long_term_value": {"level": 8, "evidence": "一句证据"},
    "factual_reliability": {"level": 8, "evidence": "..."},
    "insight_depth": {"level": 6, "evidence": "..."},
    "wisdom_transfer": {"level": 6, "evidence": "..."},
    "information_efficiency": {"level": 8, "evidence": "..."},
    "structure_expression": {"level": 6, "evidence": "..."}
  },
  "context_bonus": {
    "personal_match": 0.4,
    "timing_action": 0.2,
    "scarcity_surprise": 0.1
  },
  "risk_penalty": {
    "outdated": 0.0,
    "unsupported_assertion": 0.3,
    "clickbait": 0.0
  },
  "confidence": "high",
  "provisional": false,
  "conclusion": "一句话核心结论",
  "questions": ["问题1", "问题2", "问题3"]
}
```

约束：

- `detected_domain`：主领域+次领域路径，主领域必填，次领域可空
- `level`：0~10 整数，六维度必填
- `context_bonus` 各项 0~单项上限（0.5/0.3/0.2），缺省 0
- `risk_penalty` 各项 0~硬上限（1.2/1.2/0.8），缺省 0
- `confidence`：`high` / `medium` / `low`
- `provisional`：摘要/片段时 `true`，不得冒充完整评分
- `questions`：1~3 个独立问题，供 long-read 分配 ljg

## scoring_result（脚本输出）

```json
{
  "score_version": "2.0",
  "content_fingerprint": "sha256前16位",
  "provisional": false,
  "detected_domain": {"primary": "投资/价值投资", "secondary": "系统思考"},
  "base_score": 7.1,
  "context_bonus": {
    "personal_match": 0.4,
    "timing_action": 0.2,
    "scarcity_surprise": 0.1,
    "total": 0.7,
    "cap": 0.7,
    "capped": false
  },
  "risk_penalty": {
    "outdated": 0.0,
    "unsupported_assertion": 0.3,
    "clickbait": 0.0,
    "total": 0.3
  },
  "final_score": 7.5,
  "dimensions": {
    "long_term_value": {"level": 8, "label": "长期价值", "evidence": "..."}
  },
  "confidence": "high",
  "decision": "selective_deep_read",
  "decision_label": "选择性深读",
  "route": "long_read",
  "ljg_range": [0, 1],
  "ljg_card": false,
  "conclusion": "...",
  "questions": ["...", "..."]
}
```

字段说明：

- `route`：`card` 或 `long_read`，消费方据此分派
- `ljg_range`：仅 `route=long_read` 时给出，`card` 时为 `null`
- `ljg_card`：`final_score ≥ 8.0` 且 `route=long_read` 时 `true`
- `content_fingerprint`：传入正文时由脚本计算，否则透传 model_output 的值
- `score_version`：评分规则变更时递增，配合指纹防重复评分
