#!/bin/sh
set -eu

skill=.agents/skills/long-read/SKILL.md

require() {
  grep -Fq -- "$1" "$skill" || {
    echo "missing long-read contract rule: $1" >&2
    exit 1
  }
}

require '`score_status=scored` 且 `route=long_read`'
require '直接消费 `ljg_range` 与 `ljg_card`'
require 'scripts/run_isolated_analyses.py'
require '`store=false`'
require '不得在脚本失败时回退角色扮演'
require '显式禁用环境 HTTP 代理'
require '严格校验 Evidence Schema'
require '--user-id <bridge_context.senderId>'
require '--chat-id <bridge_context.chatId>'
require '`senderType=bot` 时回退'
require '禁止 `view_image`'

! grep -Fq 'final_score' "$skill"
! grep -Fq 'public_wiki' "$skill"
! grep -Fq 'Skill("article-decode")' "$skill"
echo 'long-read scoring and delivery rules: ok'
