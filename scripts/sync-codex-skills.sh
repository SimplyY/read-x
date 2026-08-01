#!/bin/bash
# sync-codex-skills: 将 ~/.codex/skills 中新增的 skill 同步到 ~/.agents/skills（symlink）
# 幂等：已存在的跳过，新增的创建 symlink
# 用法: sync-codex-skills [--dry-run]

set -euo pipefail

AGENTS_SKILLS="$HOME/.agents/skills"
CODEX_SKILLS="$HOME/.codex/skills"
DRY_RUN=false

[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

if [ ! -d "$CODEX_SKILLS" ]; then
  echo "错误: $CODEX_SKILLS 不存在" >&2
  exit 1
fi

if [ ! -d "$AGENTS_SKILLS" ]; then
  echo "错误: $AGENTS_SKILLS 不存在" >&2
  exit 1
fi

created=0
skipped=0

for d in $(/bin/ls -1 "$CODEX_SKILLS"); do
  if [ -e "$AGENTS_SKILLS/$d" ]; then
    skipped=$((skipped+1))
  else
    if $DRY_RUN; then
      echo "  CREATE   $d"
    else
      /bin/ln -s "$CODEX_SKILLS/$d" "$AGENTS_SKILLS/$d"
      echo "  CREATE   $d"
    fi
    created=$((created+1))
  fi
done

echo ""
echo "新增: $created  跳过: $skipped  (总计: $((created+skipped)))"
