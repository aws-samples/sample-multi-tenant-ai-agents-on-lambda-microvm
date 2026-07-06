# Mini-PoC (v2): MicroVM IAM execution role → Bedrock — AWS CLI, goal-driven

> Source: sub-agent design via AWS Knowledge MCP (official docs), 2026-07-04.
> **Probe = bash + AWS CLI. Zero app code, zero SDK.** This is the HARD GATE (Phase 0.5):
> nothing downstream starts until it's green.
>
> Design principle (Captain's call): the CLI cleanly separates two questions —
> (1) *are role creds injected at all?* → `aws sts get-caller-identity` returns the role ARN (needs NO IAM perm),
> (2) *can the standard toolchain USE them?* → `aws bedrock-runtime invoke-model` on Haiku.
> The script does NOT assume which injection mechanism MicroVM uses — it **probes all of them
> and reports**, then runs the CLI regardless. Success = "whatever the mechanism, stock tooling
> transparently gets the role creds."

## ★ THE KEY FINDING (answers the whole question)

**Lambda MicroVMs deliver execution-role creds via IMDSv2 — EC2-style — NOT via env vars like Lambda functions.**
- Documented (guided skill `iam-and-security.md`): creds at
  `http://169.254.169.254/latest/meta-data/iam/security-credentials/execution_role`;
  *"No need to bake credentials into env vars"*; *"Most AWS SDKs pick this up automatically via the default credential chain."*
- **AWS CLI v2 shares that same default chain** → `aws sts ...` / `aws s3 ls` / `aws bedrock ...` work with zero baked keys. Confirms the user's approach is sound.
- Path documented in guided skill only (not the main security page) → verify literal path live (A2).

## ★ SECOND KEY INSIGHT — AccessDenied on the List is STILL a PASS
`get-caller-identity` is the true "creds injected?" oracle (needs no IAM). The List step then
splits two failure modes:
- `Unable to locate credentials` = MicroVM did **NOT** inject creds → **design FAIL**.
- `AccessDeniedException` = creds injected but role not scoped for that action → **design PASS** (creds present & used).
So with strict least-privilege, `list-foundation-models` returning AccessDenied is the *healthy* result.
Only `invoke-model` must return 200. (Grant `bedrock:ListFoundationModels` only if you want a green List.)

## Suspend/resume credential behavior (the risk this gate kills)
- Suspend = Firecracker memory+disk snapshot; resume restores both.
- Documented: a **`/resume` lifecycle hook whose canonical purpose is "refresh credentials"** + re-establish connections. MicroVM stays SUSPENDED until hook returns 200.
- Documented: *"All outbound (non-local) connections are killed on run and resume."* CLI/SDK retry.
- **Load-bearing inference (A1):** creds come from a *live IMDS endpoint*, not frozen env vars, so
  re-reading after resume should give **fresh, non-expired** creds. Docs imply, don't guarantee →
  **Step 8 proves it** (2nd probe still returns "pong"; diff AccessKeyId before/after = fresh re-vend).

## Execution role — trust policy (CONFIRMED on official security page)
Principal is ordinary **`lambda.amazonaws.com`** (NOT `microvms.*`); needs **both** `sts:AssumeRole` + `sts:TagSession`.
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "lambda.amazonaws.com" },
    "Action": ["sts:AssumeRole", "sts:TagSession"],
    "Condition": {
      "StringEquals": { "aws:SourceAccount": "<ACCOUNT_ID>" },
      "ArnLike": { "aws:SourceArn": "arn:aws:lambda:us-east-1:<ACCOUNT_ID>:microvm-image/*" }
    }
  }]
}
```
⚠️ A6: guided skill uses `microvm-image/*` (slash) vs ARN-format section `microvm-image:<name>` (colon). If AssumeRole fails on trust, drop `ArnLike` first to isolate the mismatch.

## Execution role — permissions (least privilege)
```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "InvokeClaudeHaiku", "Effect": "Allow", "Action": "bedrock:InvokeModel",
      "Resource": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0" },
    { "Sid": "RuntimeLogs", "Effect": "Allow",
      "Action": ["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],
      "Resource": "arn:aws:logs:us-east-1:<ACCOUNT_ID>:log-group:/aws/lambda-microvms/*" }
  ]
}
```
Build role adds `s3:GetObject` on the artifact prefix + the three `logs:*` (so build logs reach CloudWatch).

## The probe container (arm64 al2023 + AWS CLI v2) — files at zip ROOT

### Dockerfile
```dockerfile
# ARM64-only; managed al2023 MicroVM base (re-seeds OpenSSL entropy on resume)
FROM --platform=linux/arm64 public.ecr.aws/lambda/microvms:al2023-minimal
RUN dnf install -y curl unzip python3 && \
    curl -s "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o /tmp/awscli.zip && \
    unzip -q /tmp/awscli.zip -d /tmp && /tmp/aws/install && \
    rm -rf /tmp/aws /tmp/awscli.zip && dnf clean all && aws --version
WORKDIR /app
COPY probe.sh server.py start.sh /app/
RUN chmod +x /app/probe.sh /app/start.sh
EXPOSE 8080
CMD ["/app/start.sh"]
```

### probe.sh (goal-driven: discover cred source, then prove toolchain uses it)
```bash
#!/usr/bin/env bash
set +e
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
MODEL="anthropic.claude-haiku-4-5-20251001-v1:0"
line(){ echo "----- $1 -----"; }
echo "===== MicroVM credential probe @ $(date -u +%FT%TZ) region=$REGION ====="
line "1. Env-var presence (only whether set)"
for v in AWS_ACCESS_KEY_ID AWS_SESSION_TOKEN AWS_CONTAINER_CREDENTIALS_FULL_URI \
         AWS_CONTAINER_CREDENTIALS_RELATIVE_URI AWS_CONTAINER_AUTHORIZATION_TOKEN \
         AWS_REGION AWS_DEFAULT_REGION; do
  [ -n "${!v}" ] && echo "  $v = SET" || echo "  $v = unset"; done
line "2. ECS-style container-credentials endpoint"
if [ -n "$AWS_CONTAINER_CREDENTIALS_FULL_URI" ]; then
  H=(); [ -n "$AWS_CONTAINER_AUTHORIZATION_TOKEN" ] && H=(-H "Authorization: $AWS_CONTAINER_AUTHORIZATION_TOKEN")
  echo "  FULL_URI -> $(curl -s --max-time 2 "${H[@]}" "$AWS_CONTAINER_CREDENTIALS_FULL_URI" -o /dev/null -w 'HTTP %{http_code}' || echo unreachable)"
else echo "  no container-cred env var -> skip"; fi
line "3. EC2-style IMDSv2 (documented MicroVM path)"
TOK=$(curl -s --max-time 2 -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
if [ -n "$TOK" ]; then
  echo "  IMDS role: $(curl -s --max-time 2 -H "X-aws-ec2-metadata-token: $TOK" http://169.254.169.254/latest/meta-data/iam/security-credentials/)"
else echo "  IMDS: no token (absent or v1-only)"; fi
line "4. What the CLI thinks the source is"; aws configure list 2>&1
line "5. Identity — proves creds injected (needs NO IAM perm)"
aws sts get-caller-identity --region "$REGION" 2>&1   # PASS: Arn has assumed-role/MicroVMExecutionRole/
line "6. Real IAM-gated List (AccessDenied here is STILL a pass for 'creds injected')"
aws bedrock list-foundation-models --region "$REGION" --query 'modelSummaries[?contains(modelId,`haiku`)].modelId' --output text 2>&1
line "7. Bedrock invoke-model — the capability we want (MUST succeed)"
printf '{"anthropic_version":"bedrock-2023-05-31","max_tokens":16,"messages":[{"role":"user","content":"Reply with the single word: pong"}]}' > /tmp/req.json
aws bedrock-runtime invoke-model --region "$REGION" --model-id "$MODEL" \
  --body fileb:///tmp/req.json --cli-binary-format raw-in-base64-out /tmp/resp.json 2>&1 \
  && echo "  RESPONSE: $(cat /tmp/resp.json)"
echo "===== probe complete ====="
```

### server.py (stdlib HTTP shim, no SDK — just shells out to probe.sh)
```python
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
HOOK = "/aws/lambda-microvms/runtime/v1/"
def run_probe(): return subprocess.run(["/app/probe.sh"], capture_output=True, text=True).stdout.encode()
class H(BaseHTTPRequestHandler):
    def _s(self, c, b=b""):
        self.send_response(c); self.send_header("Content-Type","text/plain"); self.end_headers(); self.wfile.write(b)
    def do_GET(self):  self._s(200, run_probe()) if self.path=="/probe" else self._s(404)
    def do_POST(self):
        if self.path==HOOK+"ready": self._s(200)
        elif self.path in (HOOK+"run", HOOK+"resume"): print(run_probe().decode(), flush=True); self._s(200)
        else: self._s(200)
    def log_message(self,*a): pass
HTTPServer(("0.0.0.0",8080), H).serve_forever()
```

### start.sh
```bash
#!/usr/bin/env bash
/app/probe.sh          # one probe at boot -> CloudWatch
exec python3 /app/server.py
```

## Test procedure
```bash
ACCT=<ACCOUNT_ID>; REGION=us-east-1; NAME=cred-probe; BUCKET=<your-bucket>
# 0. zip -j artifact.zip Dockerfile probe.sh server.py start.sh ; aws s3 cp ... s3://$BUCKET/microvm-images/artifact.zip
# 1. create MicroVMBuildRole + MicroVMExecutionRole (trust + perms above)
# 2. aws lambda-microvms create-microvm-image --name $NAME \
#      --base-image-arn arn:aws:lambda:$REGION:aws:microvm-image:al2023-1 \
#      --build-role-arn arn:aws:iam::$ACCT:role/MicroVMBuildRole \
#      --code-artifact '{"uri":"s3://'$BUCKET'/microvm-images/artifact.zip"}'
# 3. poll: aws lambda-microvms list-microvm-image-builds --image-identifier $NAME --image-version 1 --query 'items[0].state'
# 4. aws lambda-microvms run-microvm --image-identifier arn:...:microvm-image:$NAME --image-version 1 \
#      --execution-role-arn arn:aws:iam::$ACCT:role/MicroVMExecutionRole \
#      --ingress-network-connectors "arn:aws:lambda:$REGION:aws:network-connector:aws-network-connector:ALL_INGRESS" \
#      --egress-network-connectors  "arn:aws:lambda:$REGION:aws:network-connector:aws-network-connector:INTERNET_EGRESS" \
#      --idle-policy '{"autoResumeEnabled":true,"maxIdleDurationSeconds":60,"suspendedDurationSeconds":900}' \
#      --maximum-duration-in-seconds 3600      # -> capture MVM id + endpoint EP
# 5. TOKEN=$(aws lambda-microvms create-microvm-auth-token --microvm-identifier $MVM --expiration-in-minutes 30 \
#      --allowed-ports '[{"port":8080}]' --query 'authToken."X-aws-proxy-auth"' --output text)
# 6. FIRST: curl -s https://$EP/probe -H "X-aws-proxy-auth: $TOKEN" -H "X-aws-proxy-port: 8080"
#      PASS: §5 Arn has MicroVMExecutionRole ; §7 RESPONSE has "pong"
# 7. sleep 90 ; aws lambda-microvms get-microvm --microvm-identifier $MVM --query 'state'   # expect SUSPENDED
# 8. SECOND: curl -s https://$EP/probe ...   # traffic forces auto-resume
#      PASS: still identity + "pong" -> creds survived. FAIL: 502 / ExpiredToken / UnrecognizedClient
#      diff §5 AccessKeyId across probes: changed = fresh re-vend on resume
# 9. aws logs tail /aws/lambda-microvms/$NAME --since 20m --follow   # verify real group name (A5)
# 10. aws lambda-microvms terminate-microvm --microvm-identifier $MVM
```

## Risk table (top)
- **R1 creds frozen/expired after suspend** → CLI re-reads source each /probe; /resume hook re-probes. Signal: ExpiredTokenException.
- **R2 wrong trust principal / missing sts:TagSession** → no creds, no logs. Signal: "Unable to locate credentials". Drop ArnLike to isolate ARN-format (A6).
- **R3 no cred endpoint in guest** → §2/§3 both unreachable. Fallback: pass STS creds via run-hook payload → ~/.aws/credentials.
- **R4/R6 region/model or CRIS IAM** → us-east-1 In-Region Haiku confirmed; keep direct model id.
- **R5 no egress** → INTERNET_EGRESS connector attached; private-only = VPC egress + Bedrock VPC endpoint.
- **R7 Bedrock model access not enabled** → one-time account enablement, not a role fix.
- **R8 CLI missing on base / arm64 install fails** → Dockerfile installs v2 explicitly; check `aws --version` in build logs.
- **R9 last resort** → Secrets Manager Anthropic key (abandons IAM-only story; momentum only).

## Assumptions to verify live
- **A1** IMDS re-vends FRESH creds on resume (load-bearing; Step 8 proves).
- **A2** exact cred source & path (probe §1–4 answers empirically).
- **A3** is AWS_REGION injected? (probe §1; script defaults us-east-1).
- **A4** does ECS container-cred endpoint also exist? (probe §2).
- **A5** exact CloudWatch group name (`/aws/lambda-microvms/*` vs `/aws/lambda/microvms/*`).
- **A6** trust aws:SourceArn slash-vs-colon.
- **A7** IMDS cred TTL vs 8h runtime.
- **A8** hooks (/ready,/run,/resume) fire only if declared via `--hooks` at create/run.
- **A9** Bedrock Anthropic model access enabled for this account/region.
- **A10** does al2023-minimal ship AWS CLI v2? (install is idempotent if yes).

**What the gate delivers:** one `curl /probe` prints (a) which mechanism vended creds, (b) the
execution-role ARN via get-caller-identity with no IAM needed, (c) a live Haiku invoke-model result —
all from stock AWS CLI, zero baked keys. Re-run after auto-suspend proves creds still valid post-resume.
