#!/bin/sh
set -eu

skill=.agents/skills/long-read/SKILL.md

require() {
  grep -Fq -- "$1" "$skill" || {
    echo "missing long-read publication rule: $1" >&2
    exit 1
  }
}

require '| 公众号、GitHub、其他公开网页 | `public_link_readable`'
require '| 飞书文档、用户粘贴文本 | `internal_only`'
require '只可公开本轮 `docs +create` 返回的 `document_id`'
require '"link_share_entity":"anyone_readable"'
require '"share_entity":"only_full_access"'
require '"security_entity":"only_full_access"'
require '"comment_entity":"anyone_can_edit"'
require '"invite_external":false'
require 'permission.public get --token <document_id> --type docx --as user --format json'
require '失败时不发送文档链接'

test "$(grep -Fc '执行上方「文档发布门」' "$skill")" -eq 2
echo 'long-read publication rules: ok'
