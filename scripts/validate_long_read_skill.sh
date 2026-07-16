#!/bin/sh
set -eu

skill=.agents/skills/long-read/SKILL.md

require() {
  grep -Fq -- "$1" "$skill" || {
    echo "missing long-read publication rule: $1" >&2
    exit 1
  }
}

require '| 公众号、GitHub、其他公开网页 | `public_wiki`'
require '| 飞书文档、用户粘贴文本 | `internal_only`'
require '固定 `space_id=7663095985141796115`'
require '`open_sharing=open`'
require 'lark-cli wiki +node-create --space-id 7663095985141796115'
require 'lark-cli docs +update --as user --doc <obj_token> --command overwrite'
require '使用无 cookie 的浏览器访问最终页面'
require '从 `wiki +node-create` 的真实返回值取最终页面 `url`'
require '不创建页面，不发送文档链接'
require '`public_wiki` 不创建普通 Docx'

test "$(grep -Fc '执行上方「文档发布门」' "$skill")" -eq 2
! grep -Fq 'drive permission.public' "$skill"
! grep -Fq 'public_link_readable' "$skill"
! grep -Fq 'wiki +node-get' "$skill"
echo 'long-read publication rules: ok'
