# Multi-tenant OpenClaw on AWS Lambda MicroVMs — Infrastructure as Code

Reproduce the whole system from zero: one MicroVM per tenant, per-tenant state on
EFS, Bedrock via a VPC endpoint, and a push (Telegram-webhook) orchestrator behind
API Gateway that cold-starts / resumes tenant VMs on demand. (Architecture diagram:
[top-level README](../README.md#architecture).)

## Why CloudFormation + a thin upload script

Almost everything is declarative CloudFormation — **including** the MicroVM image and the
VPC egress connector, which ARE native CFN resources (`AWS::Lambda::MicrovmImage` and
`AWS::Lambda::NetworkConnector`, GA 2026-06-22; verified live via `describe-type`).

- `template.yaml` — the whole system: VPC + subnet + SG, EFS + mount target, Bedrock VPC
  endpoint, all **IAM roles** (MicroVM build/exec, network-connector operator,
  orchestrator), DynamoDB, **the MicroVM image (CFN runs the build)**, **the VPC egress
  connector**, the orchestrator Lambda (env fully wired via `GetAtt` — `IMAGE_ARN`,
  `IMAGE_VERSION`, `EGRESS_CONNECTOR`, no post-deploy patching), API Gateway, EventBridge.
- `deploy.sh` — the **only** thing CloudFormation can't do is put *bytes* in S3, so the
  script's imperative part is just: ensure a bucket + upload the two zips (the MicroVM
  image artifact and the bundled orchestrator Lambda). Then a single `cloudformation
  deploy` creates/updates everything.

> The **running MicroVM instances** are still created imperatively at runtime by the
> orchestrator (`run-microvm` per tenant on demand) — IaC declares the *image* and the
> *connectors*, not the ephemeral per-tenant instances. That's by design (instances are
> disposable, cold-started/reaped on demand), not an IaC gap.

## Prerequisites

- **AWS CLI v2 ≥ 2.35** — must include the `lambda-microvms` and `lambda-core`
  subcommands. Check: `aws lambda-microvms help` and `aws lambda-core help`.
- **python3 + pip** (to bundle boto3 ≥ 1.43 into the Lambda zip).
- **zip**, and **curl** (for setWebhook).
- Credentials for an account in a **MicroVMs launch region** (us-east-1/2, us-west-2,
  eu-west-1, ap-northeast-1). Bedrock model access for Anthropic Claude enabled.
- Docker is **not** required locally — the container build runs on AWS during
  `create-microvm-image`.

## Deploy (one command)

```bash
cd iac
./deploy.sh <stack-name> <region>        # e.g. ./deploy.sh openclaw-mt us-east-1
```

Takes ~10 min (CloudFormation + MicroVM image build ~5 min + connector ENIs ~2 min).
Prints the API endpoint and next-step commands at the end.

## Add a tenant

HTTP-only tenant (drive it with `chat.sh`):
```bash
./add-tenant.sh <stack> <region> tenant1
```

Telegram-push tenant (registers the webhook for you):
```bash
./add-tenant.sh <stack> <region> tenant1 <BOT_TOKEN> <WEBHOOK_SECRET>
```

Getting a `<BOT_TOKEN>`: message [@BotFather](https://t.me/BotFather) on Telegram →
`/newbot` → pick a display name and a unique username ending in `bot` → BotFather
replies with the token (`123456789:AA...`). Use a **dedicated bot per tenant** — the
webhook points that bot at this tenant's URL, so a bot you already use elsewhere would
have its webhook overwritten. `<WEBHOOK_SECRET>` is any string you invent; the script
passes it to Telegram's `setWebhook` and the router rejects updates that don't carry it
back in `X-Telegram-Bot-Api-Secret-Token`.

## Test

```bash
# synchronous (bypasses API GW 30s limit; good for watching a ~90s cold start)
./chat.sh <stack> <region> tenant1 "Remember my lucky number is 7777."
./chat.sh <stack> <region> tenant1 "What's my lucky number?"
# → cold: True ... then cold: False | reply: 7777

# Telegram: just message the bot; the webhook drives the same worker.
```

## Teardown

```bash
./teardown.sh <stack> <region>
```
Terminates **only this stack's** running MicroVMs (filtered by the VM's `imageArn`,
which names the `<stack>-openclaw` image — instances carry no stack tag, so a blind
"terminate all" would kill VMs from other stacks sharing the account/region), then
deletes the stack (CloudFormation removes the MicroVM image, connector, EFS, VPC, IAM,
DDB, Lambda, API), and finally empties & drops the artifact bucket.

## Files

| File | Purpose |
|---|---|
| `template.yaml` | All declarative infra + IAM |
| `deploy.sh` | Image build + connector + Lambda code/env wiring |
| `add-tenant.sh` / `chat.sh` / `teardown.sh` | Lifecycle helpers |
| `microvm/` | The MicroVM image: Dockerfile, `openclaw.json`, `hooks.py` (sidecar: /health,/tenant,/chat,/files + lifecycle hooks), `efs-monitor.sh` (tenant-aware EFS mount daemon), `gw-bridge.cjs` (persistent WS to the warm gateway — the perf fix), `start.sh` (supervisor) |
| `orchestrator/handler.py` | Router (fast-ACK) + Worker (ensure-VM, run turn, reply) + Sweeper |

## Design notes / gotchas baked into this IaC (learned the hard way)

1. **`AWS_REGION` is a reserved image env key** — don't set it in `--environment-variables`.
   `efs-monitor.sh` derives the EFS DNS name from `EFS_ID` + the platform-injected region.
2. **Build-phase hooks run under the *build* role.** Any autonomous agent activity
   (OpenClaw's heartbeat) during snapshot fails on missing Bedrock perms → build timeout.
   `openclaw.json` sets `heartbeat.every: "0m"`.
3. **Hard-NFS mount during image build wedges the snapshot** ("Internal service error").
   `efs-monitor.sh` stays quiet until a tenant is assigned at *runtime* (sidecar touches a
   marker on first GET), and bounds every mount with `timeout`.
4. **Only one egress connector per MicroVM** → choosing VPC egress removes the default
   internet path, so Bedrock must be reached via the **VPC endpoint** (in `template.yaml`).
   IMDS exec-role creds are link-local and unaffected.
5. **Network-connector operator role trust must be plain `lambda.amazonaws.com`** — an
   `aws:SourceAccount` condition makes the connector service fail to assume it.
6. **`update-microvm-image` replaces, not merges** — deploy.sh always re-sends base image,
   build role, capabilities, and env on updates.
7. **Gateway auth token TTL (≤60min) < VM lifetime (8h)** — the worker mints a fresh
   MicroVM auth token per turn instead of caching.
8. **SCP may block public Lambda Function URLs** — that's why ingress is API Gateway.
9. **Performance:** the sidecar talks to the *persistent* gateway over a warm WebSocket
   (`gw-bridge.cjs`) instead of spawning a CLI per message — turns dropped from ~22s to ~2s.

Each of these was hit and fixed during live verification; the reasoning is captured in
[`../docs/design/`](../docs/design/).
