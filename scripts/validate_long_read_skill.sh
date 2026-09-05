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
require '直接消费 `ljg_range`、`ljg_card` 与 `chatgpt_munger_doc`'
require 'scripts/run_isolated_analyses.py'
require 'scripts/run_chatgpt_munger.py'
require 'markdown_to_feishu_xml.py'
require 'render_long_read_delivery_card.py'
require '`chatgpt_munger_doc=true`'
require 'ChatGPT Bridge'
require '失败关闭'
require '`store=false`'
require '不得在脚本失败时回退角色扮演'
require '显式禁用环境 HTTP 代理'
require '严格校验 Evidence Schema'
require '--user-id <bridge_context.senderId>'
require '--chat-id <bridge_context.chatId>'
require '`senderType=bot` 时回退'
require '禁止 `view_image`'
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
echo 'long-read scoring and delivery rules: ok'
