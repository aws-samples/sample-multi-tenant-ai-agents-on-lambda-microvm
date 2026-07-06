#!/usr/bin/env bash
# Register a tenant. For Telegram push, also pass a bot token + secret and this
# script sets the webhook to <api>/tg/<tenantId>.
# Usage: ./add-tenant.sh STACK REGION TENANT_ID [BOT_TOKEN] [WEBHOOK_SECRET]
set -euo pipefail
STACK="${1:?stack}"; REGION="${2:?region}"; TID="${3:?tenantId}"
BOT="${4:-}"; SECRET="${5:-}"
API="$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue" --output text)"

ITEM="{\"tenantId\":{\"S\":\"$TID\"},\"state\":{\"S\":\"COLD\"},\"generation\":{\"N\":\"0\"}"
[ -n "$BOT" ]    && ITEM="$ITEM,\"botToken\":{\"S\":\"$BOT\"}"
[ -n "$SECRET" ] && ITEM="$ITEM,\"webhookSecret\":{\"S\":\"$SECRET\"}"
ITEM="$ITEM}"
aws dynamodb put-item --region "$REGION" --table-name "${STACK}-tenants" --item "$ITEM"
echo "registered tenant '$TID' (state=COLD)"

if [ -n "$BOT" ] && [ -n "$SECRET" ]; then
  curl -s "https://api.telegram.org/bot${BOT}/setWebhook" \
    --data-urlencode "url=${API}/tg/${TID}" \
    --data-urlencode "secret_token=${SECRET}" \
    --data-urlencode "drop_pending_updates=true" | python3 -c 'import json,sys;print("setWebhook:",json.load(sys.stdin))'
  echo "webhook -> ${API}/tg/${TID}"
fi
