#!/usr/bin/env bash
# Synchronous test chat for a tenant (cold-starts the VM if needed). Invokes the
# orchestrator worker directly so it isn't bound by API Gateway's 30s timeout —
# handy for validating cold starts (~90s) from a terminal.
# Usage: ./chat.sh STACK REGION TENANT_ID "message" [sessionKey]
set -euo pipefail
STACK="${1:?stack}"; REGION="${2:?region}"; TID="${3:?tenantId}"; MSG="${4:?message}"; SESS="${5:-cli}"
FN="${STACK}-orchestrator"
PAYLOAD="$(python3 -c 'import json,sys; print(json.dumps({"_worker":{"tenantId":sys.argv[1],"update":{"message":{"chat":{"id":sys.argv[2]},"text":sys.argv[3]}}}}))' "$TID" "$SESS" "$MSG")"
OUT="$(mktemp)"
aws lambda invoke --region "$REGION" --function-name "$FN" \
  --cli-binary-format raw-in-base64-out --payload "$PAYLOAD" \
  --cli-read-timeout 300 "$OUT" >/dev/null
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("cold:",d.get("cold"),"| reply:",d.get("reply"))' "$OUT"
