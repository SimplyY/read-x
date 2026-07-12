#!/usr/bin/env bash
# send-to-ready.sh — 将长文精读卡片转发到 read-y 外部群（自定义机器人 webhook）
# 用法: ./scripts/send-to-ready.sh <card.json>
# card.json 格式：lark-cli 的 interactive 卡片 JSON（含 msg_type 和 content 字段）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -f "$REPO_ROOT/.env" ]; then
  set -a; source "$REPO_ROOT/.env"; set +a
fi

WEBHOOK_URL="${READY_WEBHOOK_URL:-}"
WEBHOOK_SECRET="${READY_WEBHOOK_SECRET:-}"

if [ -z "$WEBHOOK_URL" ]; then
  echo "[send-to-ready] READY_WEBHOOK_URL 未配置，跳过转发" >&2
  exit 0
fi

CARD_FILE="${1:-}"
if [ -z "$CARD_FILE" ] || [ ! -f "$CARD_FILE" ]; then
  echo "[send-to-ready] 用法: $0 <card.json>" >&2
  exit 1
fi

# ============================================================
# 签名函数：飞书算法 HMAC-SHA256(key=timestamp+"\n"+secret, msg="")
# ============================================================
compute_sign() {
  local ts="$1"
  local hmac_key="${ts}"$'\n'"${WEBHOOK_SECRET}"
  echo -n "" | openssl dgst -sha256 -hmac "$hmac_key" -binary | base64
}

# ============================================================
# 1. 卡片格式转换
# ============================================================
CARD_OBJECT=$(jq -c '.content | fromjson' "$CARD_FILE" 2>/dev/null || true)
if [ -z "$CARD_OBJECT" ]; then
  CARD_OBJECT=$(cat "$CARD_FILE")
fi

TIMESTAMP=$(date +%s)

build_body() {
  local ts="$1" sign="$2" card_obj="$3"
  if [ -n "$sign" ]; then
    jq -n --argjson card "$card_obj" --arg timestamp "$ts" --arg sign "$sign" \
      '{timestamp:$timestamp, sign:$sign, msg_type:"interactive", card:$card}'
  else
    jq -n --argjson card "$card_obj" --arg timestamp "$ts" \
      '{timestamp:$timestamp, msg_type:"interactive", card:$card}'
  fi
}

if [ -n "$WEBHOOK_SECRET" ]; then
  SIGN=$(compute_sign "$TIMESTAMP")
  BODY=$(build_body "$TIMESTAMP" "$SIGN" "$CARD_OBJECT")
else
  SIGN=""
  BODY=$(build_body "$TIMESTAMP" "" "$CARD_OBJECT")
fi

# ============================================================
# 2. 大小检查：超过 20KB 降级为 text（重新计算签名）
# ============================================================
BODY_SIZE=$(echo -n "$BODY" | wc -c | tr -d ' ')
if [ "$BODY_SIZE" -gt 20480 ]; then
  echo "[send-to-ready] 卡片 JSON 大小 ${BODY_SIZE} 字节超过 20KB 限制，降级为 text 摘要" >&2
  TITLE=$(echo "$CARD_OBJECT" | jq -r '.header.title.content // "精读完成"' 2>/dev/null || echo "精读完成")
  TIMESTAMP=$(date +%s)
  if [ -n "$WEBHOOK_SECRET" ]; then
    SIGN=$(compute_sign "$TIMESTAMP")
    BODY=$(jq -n --arg timestamp "$TIMESTAMP" --arg sign "$SIGN" \
      --arg text "📖 ${TITLE} — 卡片内容过长，请查看原群消息" \
      '{timestamp:$timestamp, sign:$sign, msg_type:"text", content:{text:$text}}')
  else
    BODY=$(jq -n --arg timestamp "$TIMESTAMP" \
      --arg text "📖 ${TITLE} — 卡片内容过长，请查看原群消息" \
      '{timestamp:$timestamp, msg_type:"text", content:{text:$text}}')
  fi
fi

# ============================================================
# 3. 发送到 webhook（带重试：2 次，间隔 2s/5s）
# ============================================================
RESP_FILE="/tmp/ready_webhook_resp_$$.txt"

send_attempt() {
  local http_code resp_code
  http_code=$(curl -s -o "$RESP_FILE" -w "%{http_code}" \
    -X POST "$WEBHOOK_URL" \
    -H "Content-Type: application/json" \
    -d "$BODY" 2>/dev/null || echo "000")

  # 飞书即使签名错误也返回 HTTP 200，必须检查 body 里的 code
  if [ "$http_code" = "200" ] && [ -s "$RESP_FILE" ]; then
    resp_code=$(jq -r '.code // 0' "$RESP_FILE" 2>/dev/null || echo "0")
    if [ "$resp_code" != "0" ]; then
      echo "[send-to-ready] webhook 返回错误 code=$resp_code: $(jq -r '.msg // "unknown"' "$RESP_FILE" 2>/dev/null)" >&2
      echo "000"
      return
    fi
  fi
  echo "$http_code"
}

HTTP_CODE=$(send_attempt)
if [ "$HTTP_CODE" = "200" ]; then
  echo "[send-to-ready] 转发成功 → read-y" >&2
  rm -f "$RESP_FILE"
  exit 0
fi

echo "[send-to-ready] 首次发送失败 (HTTP $HTTP_CODE)，2s 后重试..." >&2
sleep 2

HTTP_CODE=$(send_attempt)
if [ "$HTTP_CODE" = "200" ]; then
  echo "[send-to-ready] 重试成功 → read-y" >&2
  rm -f "$RESP_FILE"
  exit 0
fi

echo "[send-to-ready] 第二次失败 (HTTP $HTTP_CODE)，5s 后最后重试..." >&2
sleep 5

HTTP_CODE=$(send_attempt)
if [ "$HTTP_CODE" = "200" ]; then
  echo "[send-to-ready] 第三次成功 → read-y" >&2
  rm -f "$RESP_FILE"
  exit 0
fi

echo "[send-to-ready] 三次发送均失败，静默退出" >&2
rm -f "$RESP_FILE"
exit 0
