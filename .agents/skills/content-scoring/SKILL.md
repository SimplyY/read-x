---
name: content-scoring
description: "文章内容评分引擎：输入抓取后的正文与元数据，输出可复现的质量分、维度证据、决策、路由与文字深度数量。被 link-card 与 long-read 共用，同一正文只评一次。模型只输出维度等级与证据，分数由 scripts/content_scoring.py 计算。"
when_to_use: "由 link-card 或 long-read 调用，不直接由用户触发。抓取正文后、决定走卡片还是 long-read 前，必须调用本 Skill。"
user_invocable: false
---

# content-scoring：文章内容评分引擎

统一 `link-card` 与 `long-read` 的评分逻辑。**同一正文只评一次**：`link-card` 抓取后调用本 Skill，把结果传给 `long-read`；`long-read` 只消费结果决定深度，禁止重评。

## 1. 第一性

评分量化的是：对当前读者而言，继续投入注意力能获得多少**可靠、长期、可迁移的认知增量**。它不是文笔评分，也不是情绪评分，而是注意力分配指令。

分数服务于两个下游：路由决策（卡片还是 long-read）、深度决策（几个文字 ljg）。分数必须可复现、可校准、可纠偏。
评分须以对应领域专家视角进行：先识别主领域与次领域，以双重专家身份评判，而非通用文评人凭感觉。

## 2. 调用链

```
link-card 抓取正文
  └─ content-scoring(model_output, source_text) -> scoring_result
       ├─ route=card  -> link-card 自处理（一句话/中等卡片）
       └─ route=long_read -> long-read 接收 scoring_result，按 ljg_range 调度
```

`long-read` 入口已持有 `scoring_result`，不再自行评分，只用 `final_score` 决定 `ljg_range` 与 `ljg_card`。

## 3. 防重复评分

- `content_fingerprint = sha256(正文)[:16]`，由 `scripts/content_scoring.py` 计算。
- `score_version` 当前为 `2.0`，评分规则变更时递增。
- 同一 `content_fingerprint + score_version` 已有结果时直接复用；正文或评分版本变化才允许重评。
- 只有摘要或片段时标记 `provisional: true`，不得冒充完整评分。

## 4. 六维度（基础分上限 10.0）

模型对每个维度输出 `0~10` 整数等级及一句证据，**不输出分数**。脚本计算：

`base_score = Σ(等级 / 10 × 权重)`

| 维度 | 权重 | 评什么 |
|------|------|--------|
| 长期价值 long_term_value | 2.5 | 长期趋势、规律、结构 |
| 事实可靠 factual_reliability | 2.0 | 事实可核验，事实与观点分离 |
| 洞察深度 insight_depth | 2.5 | 因果、反馈、二阶影响、系统演化 |
| 智慧迁移 wisdom_transfer | 2.5 | 可沉淀为模型、原则、方法 |
| 信息效率 information_efficiency | 1 | 单位阅读时间的认知增量 |
| 结构表达 structure_expression | 1 | 论证与结构是否清晰 |

各维度 0~10 等级锚点见 `references/anchors.md`。

## 5. 上下文加分（上限 1.0）

| 项 | 上限 | 评什么 |
|----|------|--------|
| 个人匹配 personal_match | 0.5 | 推进读者关注的 AI Agent、投资、教育、长期主义、系统思考 |
| 时机与行动 timing_action | 0.3 | 对应当前研究、决策或实践 |
| 稀缺与意外 scarcity_surprise | 0.3 | 一手事实、反常洞察、跨领域连接 |

加分总分按 `base_score` 分档设上限：

| base_score | 加分上限 |
|------------|----------|
| `<6.0` | 0.5 |
| `6.0~6.9` | 0.6 |
| `7.0~7.9` | 0.8 |
| `≥8.0` | 1.0 |

## 6. 风险扣分

保持克制，避免重复处罚。模型在「通常」范围内给出扣分，脚本 clamp 到单项硬上限：

| 项 | 通常 | 硬上限 |
|----|------|--------|
| 信息过时 outdated | 0.2~0.3 | 1（核心失效） |
| 无证据断言 unsupported_assertion | 0.3~0.5 | 1（核心论点无支撑） |
| 标题党 clickbait | 0.2~0.3 | 0.5（严重错配） |

`final_score = clamp(round(base_score + context_bonus - risk_penalty, 1), 0, 10)`

只有严重误导才限制最高分；常规风险只扣分不封顶。

## 7. 决策阈值与路由

| final_score | 决策 | route | 文字 ljg | ljg-card |
|-------------|------|-------|----------|----------|
| `9.0~10` | 稀缺精读 | long_read | 2~3 | 文档交付后 |
| `8.0~8.9` | 完整深读 | long_read | 1~2 | 文档交付后 |
| `7.0~7.9` | 选择性深读 | long_read | 0~1 | 不强制 |
| `6.0~6.9` | 快速阅读 | card | - | - |
| `0~5.9` | 跳过 | card | - | - |

8.0~8.4 取 1 条 ljg，8.5~8.9 取 1~2 条，≥9.0 取 2~3 条，对齐 `long-read` 既有契约。`≥8.0` 且 `route=long_read` 时 `ljg_card=true`，在主文档交付成功后独立运行，仅私聊 PNG。

## 8. 输入与输出

模型输出 `model_output`（见 `references/schema.md`）含六维度等级+证据、加分、扣分、置信度、结论、最值得深挖的 1~3 个问题。脚本输出 `scoring_result` 含版本号、指纹、base/bonus/penalty/final、维度证据、决策、路由、ljg 区间、结论、问题。

调用：

```bash
python3 scripts/content_scoring.py <model_output.json> <source.md>
# 或从 stdin
cat model_output.json | python3 scripts/content_scoring.py - <source.md>
```

## 9. 模型 Prompt 要点

给模型的指令必须明确：

1. **领域识别（评分前置）**：读文后先判定主领域与次领域（路径式，如 `投资/价值投资` + `系统思考`），写入 `detected_domain`。主领域是文章核心所属，次领域是显著跨域；无明显次领域时次领域留空，不硬凑。
2. **双重专家视角（核心）**：以「主领域专家 ∧ 次领域专家」双重身份评六维度。两个领域的专家视角均**动态生成**--先列出该领域专家会看什么、什么是硬伤、什么是真发现，再据此定级。不预设领域清单。
3. 只输出六维度 `0~10` 整数等级 + 一句证据，**不要自己算分**；证据必须体现领域内行判断（禁通用套话），可区分「主领域视角看...次领域视角看...」。
4. 加分看读者相关性（AI Agent/投资/教育/长期主义/系统思考），扣分看风险；
5. 输出严格 JSON，字段见 `references/schema.md`；
6. 只有摘要/片段时 `provisional: true`；
7. **诚实降级**：若对某领域不具内行把握，`confidence` 降级（low/medium），不硬装专家；evidence 须写明判断依据，便于事后校验是真内行还是套话。
8. 给出最值得深挖的 1~3 个独立问题，供 `long-read` 分配 ljg。

## 10. 边界

- 不替代 `article-decode` 的解码，不替代 Evidence 提取；评分只读正文与元数据。
- 不读用户画像做事实判断；个人匹配只用于加分，不污染维度等级。
- 评分失败时 `link-card` 回退为一句话卡片 + 原文链接，不阻塞交付。
