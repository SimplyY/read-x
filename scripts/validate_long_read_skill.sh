#!/bin/sh
set -eu

skill=.agents/skills/long-read/SKILL.md
link_card=.agents/skills/link-card/SKILL.md

require() {
  grep -Fq -- "$1" "$skill" || {
    echo "missing long-read contract rule: $1" >&2
    exit 1
  }
}

require_link_card() {
  grep -Fq -- "$1" "$link_card" || {
    echo "missing long-read handoff rule in link-card: $1" >&2
    exit 1
  }
}

require '`score_status=scored` 且 `route=long_read`'
require '直接消费 `ljg_range`、`ljg_card` 与 `chatgpt_munger_doc`'
require 'long_read_workflow_cli.py submit'
require '`status` 只读查看状态'
require '`resume --job-id'
require 'long_read_workflow_cli.py'
require '持久 Worker'
require 'run_isolated_analyses.py'
require 'run_chatgpt_munger.py'
require 'Bridge'
require 'live-dom+snapshot'
require 'Evidence Schema 与原文逐字引文'
require '并发 ≤3 路'
require '唯一完成门'
require '最多两轮额外恢复'
require '至少 240 秒'
require '周报每周日 09:00'
require '月报每月 1 日 09:00'
require 'configure-report-target'
require '首次写入后不可静默更改'
require '只使用已保存目标'
require '每次报告投递前'
require '精确会话成员'
require 'F/N > 10%'
require 'Markdown 附件'
require 'Card 2.0'
require 'status --report-key'
require '终态任务的输入快照与分析/图片产物保留 30 天'
require '逐任务统计与已完成报告快照保留 180 天'
require '未解决任务及其产物、未完成的报告投递保留至处理'
require '值得研究的相关问题'
require '正文第一个主章节必须是 `<h1>评分</h1>`'
require '独立的问题列表'
require '共同的上下文列表'
require '不得使用悬空的“该判断”“上述内容”“它”等指代'
require '≤300 字'

! grep -Fq 'final_score' "$skill"
! grep -Fq 'public_wiki' "$skill"
! grep -Fq 'Skill("article-decode")' "$skill"
! grep -Fq '对飞鱼的意义' "$skill"
! grep -Fq 'ljg-card' "$skill"
! grep -Fq 'python3 .agents/skills/long-read/scripts/run_long_read_pipeline.py' "$skill"
! grep -Fq 'lark-cli docs +media-insert --as bot' "$skill"
! grep -Fq 'lark-cli im +messages-send --as bot' "$skill"
! grep -Fq '500～1500' "$skill"
require_link_card '持久 long-read 工作流'
require_link_card '只有持久工作流返回 `accepted` 才能显示“已接管”'
require_link_card '`ljg_card` 仅作评分留档'
require_link_card '入口不得重复发送交付卡或提前显示完成'
! grep -Fq '`ljg_card=true` 时在文档交付后独立运行' "$link_card"
! grep -Fq 'run_chatgpt_core_image.py' "$link_card"
echo 'long-read scoring and delivery rules: ok'
