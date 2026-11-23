# 周期可靠性报告

> 本文件自 long-read/SKILL.md 移入，原文未改动；入口保留触发条件与硬规则。

部署前先用 `configure-report-target --profile <profile> --user-id <open_id> --chat-id <p2p_chat_id> --sender-id <app_id>` 验证并将报告目标写入私有状态库。配置时及每次报告投递前，只读验证当前用户身份、本人的 open_id、bot app_id、私聊类型和精确会话成员；任一信息不匹配或不可读时停止且不发送。该配置首次写入后不可静默更改，替换必须显式使用 `--replace`。常驻 `run-worker --serve`、`report --deliver` 和 `run-due-reports --deliver` 只使用已保存目标，任何传入断言不匹配时均拒绝投递。缺少持久目标时不得声称已具备自动投递。周报每周日 09:00 检查此前连续 7 天；只有明确最终失败占比 `F/N > 10%` 才发送。`N=0` 不发送；待核实与处理中计入分母，但不计入明确失败分子。月报每月 1 日 09:00 必须发送上个自然月报告，即使没有任务也写明无有效任务。

每份报告使用固定周期快照。完整 Markdown 附件和 Card 2.0 摘要分别发送、分别读回核验。用 `status --report-key <key>` 只读查看报告与附件/卡片投递收据；收件目标或投递结果无法确认时，保留待处理状态，不谎报已发送、不重复投递。只有真实任务不少于 50、覆盖完整且没有未结项时，才判断长期失败率目标。

人工处置、已实施修复和修复对比通过 `long_read_workflow_cli.py record-event` 进入同一持久事件账本，并纳入后续报告。`manual_intervention`、`manual_reconciled`、`manual_repair` 使用 `category`、单行 `summary` 和可选 `reference`；category 为 `recovery`、`reconciliation`、`repair` 或 `other`。`repair_applied`、`root_cause_fixed` 使用 `repair_id`、`failure_fingerprint`、单行 `summary`、`change_ref`。`repair_comparison` 使用 `repair_id`、`metric`、`before_value`、`after_value`、`before_sample_size`、`after_sample_size`、`before_window`、`after_window`；metric 为 `final_failure_rate`、`stage_failure_rate`、`recovery_rate` 或 `p90_duration_seconds`，窗口为 `YYYY-MM` 或 `YYYY-MM-DD..YYYY-MM-DD`。例如：

```bash
long_read_workflow_cli.py record-event --job-id <job-id> --event-type repair_applied --details-json '{"repair_id":"fix-42","failure_fingerprint":"bridge:response-unstable","summary":"保留原 Bridge 会话以便只读恢复","change_ref":"abc1234"}'
```

不要写入文章正文、凭据或敏感个人信息；同一任务、类型和规范字段的重复命令会复用原事件。

长文终态任务的输入快照与分析/图片产物保留 30 天，之后清除并脱敏任务记录；仍有未处理副作用的任务继续保留。双上下文包任务的冻结资料在长期归档验证能力完成前持续保留。过期扫描同时清理无任务引用的输入快照和中断提交遗留的临时快照。逐任务统计与已完成报告快照保留 180 天。原因、阶段、验证和错误码只保留已知机器值或数字；未知文本统一记为 `unknown`，避免把文章内容留在长期统计中。未解决任务及其产物、未完成的报告投递保留至处理。180 天后仅保留无任务标识的生命周期成功/失败计数，供长期指标使用；超出统计留存窗口的漏跑周期不重造报告。
