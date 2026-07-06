#!/usr/bin/env bash
# End-to-end deploy for multi-tenant OpenClaw on Lambda MicroVMs.
#
# CloudFormation does almost everything — including the MicroVM image and the VPC egress
# connector, which ARE native CFN resources (AWS::Lambda::MicrovmImage /
# AWS::Lambda::NetworkConnector). The only thing CFN can't do is put *bytes* in S3, so this
# script's imperative part is just: ensure a bucket + upload the two zips. Then one
# `cloudformation deploy` builds the image, provisions the connector, and wires the
# orchestrator Lambda via GetAtt.
#
# Prereqs: AWS CLI v2 >= 2.35 (has the lambda-microvms models), python3, uv (or a modern
# pip), zip. Credentials for a MicroVMs launch region with Bedrock Claude access.
#
# Usage:  ./deploy.sh [STACK_NAME] [REGION]
set -euo pipefail

STACK="${1:-openclaw-mt}"
REGION="${2:-us-east-1}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
GATEWAY_TOKEN="${GATEWAY_TOKEN:-poc-microvm-token-42}"
BUCKET="${STACK}-artifact-${ACCOUNT}-${REGION}"
IMG_KEY="microvm-images/openclaw.zip"
CODE_KEY="lambda/orchestrator.zip"

say(){ printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }

# ---------- 1. Artifact bucket (the one imperative prerequisite) ----------
say "1/4 Ensure artifact bucket: $BUCKET"
if ! aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  if [ "$REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null
  else
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
      --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
  fi
  aws s3api put-public-access-block --bucket "$BUCKET" \
    --public-access-block-configuration BlockPublicAcls=true,BlockPublicPolicy=true,IgnorePublicAcls=true,RestrictPublicBuckets=true
fi

# ---------- 2. MicroVM image artifact (Dockerfile + app) ----------
say "2/4 Package & upload MicroVM image artifact"
IMGZIP="$(mktemp -d)/microvm.zip"
( cd "$HERE/microvm" && zip -j -q "$IMGZIP" Dockerfile openclaw.json hooks.py start.sh efs-monitor.sh gw-bridge.cjs )
aws s3 cp "$IMGZIP" "s3://${BUCKET}/${IMG_KEY}" --region "$REGION"

# ---------- 3. Orchestrator Lambda zip (boto3 + lambda-microvms model overlay + handler) ----------
say "3/4 Bundle & upload orchestrator Lambda"
PKG="$(mktemp -d)"
if command -v uv >/dev/null 2>&1; then
  uv pip install --quiet --python "$(command -v python3)" --target "$PKG" boto3
else
  python3 -m pip install --quiet --upgrade --target "$PKG" boto3 \
    || { echo "ERROR: install uv (https://docs.astral.sh/uv/) or a recent pip"; exit 1; }
fi
# PyPI boto3 may lag the lambda-microvms service model; overlay it from the local AWS CLI's
# botocore data (the CLI ships it — that's how this very script calls the service).
CLI_DATA="$(python3 - "$(readlink "$(command -v aws)" || command -v aws)" <<'PY'
import os,sys,glob
root=os.path.realpath(sys.argv[1])
for _ in range(6):
    root=os.path.dirname(root)
    hit=glob.glob(os.path.join(root,'**','botocore','data','lambda-microvms'),recursive=True)
    if hit: print(os.path.dirname(hit[0])); break
PY
)"
if [ -n "$CLI_DATA" ] && [ -d "$PKG/botocore/data" ]; then
  cp -R "$CLI_DATA/lambda-microvms" "$PKG/botocore/data/" 2>/dev/null || true
  cp -R "$CLI_DATA/lambda-core"     "$PKG/botocore/data/" 2>/dev/null || true
fi
python3 -c "import sys;sys.path.insert(0,'$PKG');import boto3;assert 'lambda-microvms' in boto3.session.Session().get_available_services(),'overlay failed';print('  boto3',boto3.__version__,'+ lambda-microvms model')"
cp "$HERE/orchestrator/handler.py" "$PKG/"
CODEZIP="$(mktemp -d)/orchestrator.zip"; ( cd "$PKG" && zip -q -r "$CODEZIP" . )
aws s3 cp "$CODEZIP" "s3://${BUCKET}/${CODE_KEY}" --region "$REGION"

# ---------- 4. One CloudFormation deploy: builds image, connector, everything, wired ----------
say "4/4 CloudFormation deploy (builds MicroVM image ~5min + connector + all infra)"
aws cloudformation deploy --region "$REGION" --stack-name "$STACK" \
  --template-file "$HERE/template.yaml" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
      ProjectName="$STACK" \
      GatewayToken="$GATEWAY_TOKEN" \
      ArtifactBucketName="$BUCKET" \
      MicrovmImageKey="$IMG_KEY" \
      OrchestratorCodeKey="$CODE_KEY"

API="$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue" --output text)"

say "DONE"
cat <<EOF

  API endpoint : ${API}
  Add a tenant : ./add-tenant.sh ${STACK} ${REGION} <tenantId> [telegramBotToken] [webhookSecret]
  Test (HTTP)  : ./chat.sh ${STACK} ${REGION} <tenantId> "your message"
  Teardown     : ./teardown.sh ${STACK} ${REGION}
EOF
