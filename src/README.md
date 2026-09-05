# Multi-tenant OpenClaw on AWS Lambda MicroVMs — Infrastructure as Code

Reproduce the whole system from zero: one MicroVM per tenant, per-tenant state on
EFS, Bedrock via a VPC endpoint, and a push (Telegram-webhook) orchestrator behind
API Gateway that cold-starts / resumes tenant VMs on demand. (Architecture diagram:
[top-level README](../README.md#architecture).)

> [!IMPORTANT]
> This stack is a sample of tenant orchestration; it is **not production-ready** and the
> security controls a real multi-tenant service needs are yours to implement. What it does
> and does not protect is spelled out in
> [Not production-ready](../README.md#not-production-ready) — read that before deploying
> anything you care about.

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
  script's imperative core is: ensure a bucket + upload the two zips (the MicroVM
  image artifact and the bundled orchestrator Lambda). Then a single `cloudformation
  deploy` creates/updates everything. Around that core it also: pre-flights the CLI and
  target region before anything is created, mints/reuses a random gateway token
  (`.gateway-token`, git-ignored), hard-verifies the `lambda-microvms` service model
  made it into the Lambda bundle, and names both S3 keys by **content hash** — a code
  change always triggers the CFN image rebuild / Lambda update, and an unchanged
  redeploy is a true no-op (with a fixed key, CFN sees identical resource properties
  and silently skips the update — code changes never reach AWS).

> The **running MicroVM instances** are still created imperatively at runtime by the
> orchestrator (`run-microvm` per tenant on demand) — IaC declares the *image* and the
> *connectors*, not the ephemeral per-tenant instances. That's by design (instances are
> disposable, cold-started/reaped on demand), not an IaC gap.

## Prerequisites

- **AWS CLI v2 ≥ 2.35** — must include the `lambda-microvms` and `lambda-core`
  subcommands. Check: `aws lambda-microvms help` and `aws lambda-core help`.
- **python3 + pip** (to bundle boto3 ≥ 1.43 into the Lambda zip).
- **zip**, and **curl** (for setWebhook).
- Credentials for an account in a **region where Lambda MicroVMs has launched** —
  `deploy.sh` probes the target region up front (the launch list keeps growing, so it
  isn't hardcoded anywhere) and fails fast with the reason if the service isn't
  reachable there.
- Docker is **not** required locally — the container build runs on AWS during
  `create-microvm-image`.

## Deploy (one command)

```bash
cd src
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
back in `X-Telegram-Bot-Api-Secret-Token`. That secret is **mandatory** for the webhook
route: an HTTP-only tenant, registered without a bot token and secret, has no webhook and
is deliberately unreachable through the API — drive it with `chat.sh`.

## Test

```bash
# synchronous (bypasses API GW 30s limit; good for watching a ~90s cold start)
./chat.sh <stack> <region> tenant1 "Remember my lucky number is 7777."
./chat.sh <stack> <region> tenant1 "What's my lucky number?"
# → cold: True ... then cold: False | reply: 7777

# Telegram: just message the bot; the webhook drives the same worker.
```

Over Telegram (not the HTTP test path) the worker also gives you:

- **Streaming replies** — a placeholder message that grows via `editMessageText`
  (~1.2 s cadence, just above Telegram's per-chat edit ceiling) while the model
  generates, with a `▌` cursor until the final edit.
- **Images** — send a photo (or an image file) with or without a caption; the worker
  pulls it from Telegram, ships it into the VM as a base64 attachment, and the agent
  answers about what it sees.
- **`/model` switching** — e.g. `/model amazon-bedrock/us.anthropic.claude-sonnet-5`,
  `/model default` to reset. The catalog is discovered live from Bedrock at each cold
  start (`materialize-models.mjs`), so newly launched models are switchable without a
  redeploy.

## Public surface

API Gateway exposes two routes and nothing else:

| Route | Auth |
|---|---|
| `POST /tg/<tenantId>` | that tenant's `webhookSecret`, in `X-Telegram-Bot-Api-Secret-Token`. Missing or mismatched → 403; a tenant with no secret stored is *always* 403 |
| `GET /health` | none — returns no tenant data |

Any other path is 404. There is deliberately no unauthenticated route that takes a
tenantId from the URL and runs a prompt in that tenant's MicroVM: anyone knowing a
tenantId could then execute inside another tenant's VM, which defeats the per-tenant
isolation this sample is built around. That is why the synchronous path is `chat.sh` —
a direct Lambda invoke with your AWS credentials — rather than an HTTP route. Both rules
are locked in by [`../tests/test_router_auth.py`](../tests/test_router_auth.py):

```bash
uv run --with pytest python -m pytest ../tests -q
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
| `deploy.sh` | Pre-flight + artifact upload (content-hashed keys) + one CFN deploy |
| `add-tenant.sh` / `chat.sh` / `teardown.sh` | Lifecycle helpers |
| `microvm/` | The MicroVM image: Dockerfile, `openclaw.json` (gateway + vision-capable model seed + discovery config), `hooks.py` (sidecar: /health,/tenant,/chat,/chat-async,/progress,/media,/files + the platform lifecycle hooks — ready/validate snapshot+prefetch, run tenant injection), `efs-monitor.sh` (tenant-aware EFS mount daemon + config authority + session heal), `materialize-models.mjs` (bakes live Bedrock model discovery into the config at cold start), `gw-bridge.cjs` (persistent WS to the warm gateway; sync turns, async turns with streamed-text polling, image attachments), `start.sh` (supervisor) |
| `orchestrator/handler.py` | Router (fast-ACK) + Worker (ensure-VM, run turn — streaming edits + images on Telegram) + Sweeper |

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
   IMDS exec-role creds are link-local and unaffected. The VM subnet additionally routes
   `0.0.0.0/0` through a **NAT gateway** so the agent's web search/fetch tools work —
   Bedrock and EFS traffic still take the private paths, bypassing the NAT.
5. **Network-connector operator role trust must be plain `lambda.amazonaws.com`** — an
   `aws:SourceAccount` condition makes the connector service fail to assume it.
6. **`update-microvm-image` replaces, not merges** — deploy.sh always re-sends base image,
   build role, capabilities, and env on updates.
7. **Gateway auth token TTL (≤60min) < VM lifetime (8h)** — the worker mints a fresh
   MicroVM auth token per turn instead of caching.
8. **SCP may block public Lambda Function URLs** — that's why ingress is API Gateway.
9. **Performance:** the sidecar talks to the *persistent* gateway over a warm WebSocket
   (`gw-bridge.cjs`) instead of spawning a CLI per message — turns dropped from ~22s to ~2s.
10. **A turn can outlive the Lambda invocation polling it** (300s timeout, 15 min hard
    max) — the turn keeps running inside the VM, but its reply would never be delivered.
    Fix: just before timing out, the worker hands polling to a fresh async self-invoke
    (turnId + message state), so a single turn is bounded by the VM's 8h lifetime, not
    by Lambda's 15 min.
11. **Cold start is ~12s wall clock (was 48s), via the image-build hooks + two NFS
    fixes.** `/ready` gates the snapshot, `/validate` drives page prefetch, `/run`
    delivers the tenantId. Two traps worth knowing before touching this: the prewarm in
    `hooks.py` needs Bedrock perms on the **build** role or it samples nothing, and
    recursive `chown` over NFS costs one round trip per file. Full measurements,
    rejected approaches, and how to re-measure:
    [`../docs/perf/cold-start.md`](../docs/perf/cold-start.md).

Each of these was hit and fixed during live verification; the reasoning is captured in
[`../docs/design/`](../docs/design/).
