# 相关性补算原协议（默认暂停，恢复时使用）

- 状态：2026-11-23 用户裁决默认暂停；本文件逐字保留原第 4 步协议，恢复默认时把它写回 SKILL.md 第 4 步、移除固定尾部与首次调用中的 `--relevance-unavailable` 旗标，并恢复三档齐全门原语义。

## 原第 4 步协议（逐字）

4. 若 `score_status=needs_relevance`，从 `/Users/yuwei/code/yw-context/scripts/context-route.mjs` 获取 P1 JSON，参数固定为 `--mode full --latest --consumer read-x.content-scoring`，输出存入本轮 `run_dir`。逐条核验本仓正文与 manifest 哈希；`coverage.complete=false` 时仍继续使用有效本仓 P1。生成绑定相同 commit/manifest 的 `external-followups.json`；哈希自洽只证明文件一致，最终评分还要复核权威源端。当前只有 `yw-context-rule-table` 可标 `verified`：内容必须是完整实时机读快照，revision 与真实 Base 读回一致；其他来源没有权威验证器时记录 `unavailable`，保留为未知，继续使用本仓 P1。`generate_relevance.py` 与最终 `content_scoring.py` 必须使用同一份收据文件；评分器会重读规则表（不可用时校验固定提交完整快照），复核来源版本、引用、正文和路由，并比对全部来源 ID。第二次运行必须复用同一个 `base_config.json` 并再次传 `--config-from-base`，同时传 `--relevance-output`、`--context` 和 `--external-context`。只有路由无效、没有可用本仓正文、远端不可验证、外部收据无效或相关性输出无效时才用 `--relevance-unavailable`；未验证来源会随评分结果保留为缺口，不读取任何旧 YWNext 文件。
