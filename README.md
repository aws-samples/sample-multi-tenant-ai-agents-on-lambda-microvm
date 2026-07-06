# Multi-tenant AI agents on AWS Lambda MicroVMs

> One isolated Firecracker MicroVM per tenant — cold-started, resumed, and reaped on
> demand — so a self-hosted AI agent scales to many tenants at near-zero idle cost.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![AWS Lambda MicroVMs](https://img.shields.io/badge/AWS-Lambda%20MicroVMs-FF9900?logo=amazonaws&logoColor=white)](https://aws.amazon.com/about-aws/whats-new/2026/06/aws-lambda-microvms/)
[![Verified live on AWS](https://img.shields.io/badge/verified%20live-Jun%202026-brightgreen.svg)](docs/)

A working, end-to-end system: run a self-hosted AI agent
([OpenClaw](https://github.com/openclaw/openclaw)) **one isolated MicroVM per tenant**,
with per-tenant state persisted on EFS, model calls served by Amazon Bedrock, and a
push-based (Telegram webhook) orchestrator that cold-starts, resumes, and reaps tenant
VMs on demand.

Built on [AWS Lambda MicroVMs](https://aws.amazon.com/about-aws/whats-new/2026/06/aws-lambda-microvms/)
(GA June 2026) — Firecracker-isolated, snapshot-resumable serverless compute with an 8-hour
lifetime. Everything here was verified live on AWS; the design decisions and the (many)
gotchas hit along the way are written up in [`docs/`](docs/).

## Demo

Messaging a tenant's Telegram bot cold-starts its MicroVM and drives a live agent turn —
here the freshly-booted agent introduces itself:

![A Telegram chat: the user asks "Hi, who are you?" and the OpenClaw agent, running inside its per-tenant MicroVM, introduces itself and asks what to call itself.](docs/images/demo-telegram.png)

## Why this project

Self-hosted agents are traditionally "always-on" — a container or VM per user, running
(and billing) 24/7 even while idle. That doesn't scale to many tenants. Lambda MicroVMs
flip the model, and this project shows how to exploit that for a multi-tenant agent:

- **Near-zero idle cost.** An idle tenant's VM auto-suspends; a fully idle tenant is
  terminated and its state parks on EFS for ≈$0. You pay for conversation, not for
  waiting — the economic foundation that makes one-VM-per-tenant affordable at scale.
- **Fast resume, not cold boot.** Resuming a suspended MicroVM restores the Firecracker
  snapshot — process memory and all — in ~seconds, so a returning user hits a warm agent
  (bundle loaded, provider pre-warmed) instead of waiting for a container to boot.
- **Automatic lifecycle management.** The platform suspends/resumes on traffic; the
  orchestrator cold-starts dead tenants on demand and a sweeper reaps idle ones. No
  cluster to run, no autoscaler to tune — tenants flow hot → warm → cold on their own.
- **Hard per-tenant isolation.** Each tenant gets its own Firecracker microVM, not a
  shared process or namespace — a strong security boundary between customers by default.
- **Zero static credentials.** The workload gets its AWS access from the MicroVM's IMDSv2
  execution role; no keys are baked into the image or env. The stock SDK/CLI just work.

## Architecture

```mermaid
flowchart TB
    TG[Telegram] -- "webhook /tg/:tenant" --> API["API Gateway · HTTP API"]
    API --> ORCH["Orchestrator Lambda<br/>router · worker · sweeper"]
    SWEEP["EventBridge · rate(10 min)"] -.-> ORCH
    ORCH <-.-> REG[("DynamoDB<br/>tenant registry")]
    ORCH -- "alive → forward<br/>dead → run-microvm" --> VM
    subgraph VM ["per-tenant Lambda MicroVM"]
        direction TB
        GW["OpenClaw gateway :18789"]
        BRG["gw-bridge :8090 · warm WebSocket (the perf fix)"]
        SIDE["sidecar :8080 · health · tenant · chat · hooks"]
        GW ~~~ BRG ~~~ SIDE
    end
    VM -- "NFS /tenants/:id" --> EFS[("EFS · per-tenant state")]
    VM -- "VPC endpoint" --> BED["Amazon Bedrock"]

    classDef entry   fill:#E3F2FD,stroke:#1565C0,color:#0D47A1;
    classDef control fill:#FFF3E0,stroke:#E65100,color:#E65100;
    classDef compute fill:#E8F5E9,stroke:#2E7D32,color:#1B5E20;
    classDef data    fill:#F3E5F5,stroke:#6A1B9A,color:#4A148C;
    classDef ext     fill:#ECEFF1,stroke:#455A64,color:#263238;

    class TG,API entry;
    class ORCH,SWEEP control;
    class GW,BRG,SIDE compute;
    class REG,EFS data;
    class BED ext;
```

Credentials reach the VM via its IMDSv2 execution role (no static keys); idle VMs
suspend and auto-resume, and are reaped within the 8-hour max lifetime — state
survives on EFS across VM generations.

## Quickstart

Four commands take you from an empty account to a talking agent. You need AWS CLI v2 with
the `lambda-microvms` subcommands, credentials for a [MicroVMs launch
region](#requirements--notes), and Bedrock access for Anthropic Claude — full prerequisites and the
Telegram-push path are in [`iac/README.md`](iac/README.md).

```bash
cd iac

# 1. Deploy the whole system (~10 min: CloudFormation + MicroVM image build + connector).
./deploy.sh openclaw-mt us-east-1

# 2. Register an HTTP-only tenant.
./add-tenant.sh openclaw-mt us-east-1 tenant1

# 3. Chat with it. The first turn cold-starts the tenant's MicroVM (~90s); later turns are warm.
./chat.sh openclaw-mt us-east-1 tenant1 "Remember my lucky number is 7777."
./chat.sh openclaw-mt us-east-1 tenant1 "What's my lucky number?"
# → cold: True ... then cold: False | reply: 7777   (state survived on EFS)

# 4. Tear it all down (terminates only this stack's VMs, then deletes the stack).
./teardown.sh openclaw-mt us-east-1
```

## Repository layout

| Directory | What it is | Details |
|---|---|---|
| [`iac/`](iac/) | **Start here.** Reproduce the whole system from zero — CloudFormation template, one-command deploy, tenant/lifecycle scripts, the MicroVM image, the orchestrator. | [`iac/README.md`](iac/README.md) — prerequisites, step-by-step deploy/test/teardown, gotchas |
| [`docs/`](docs/) | The "why" behind the code: the design decisions taken while building. | [`docs/README.md`](docs/README.md) — index of the design notes |

## Requirements & notes

- **Region.** Deploy in a MicroVMs launch region — `us-east-1`, `us-east-2`, `us-west-2`,
  `eu-west-1`, or `ap-northeast-1` — with Bedrock model access for Anthropic Claude enabled.
- **Security.** `poc-microvm-token-42` and similar strings in the code are **placeholder
  tokens**, not secrets; the real boundary is IAM + per-request auth tokens. Override
  `GatewayToken` in the CloudFormation parameters for real use. To report a vulnerability,
  see [CONTRIBUTING.md](CONTRIBUTING.md#security-issue-notifications).
- **Maturity.** This is a sample verified live on AWS (June 2026), not production-hardened —
  the open items for hardening are called out in [`docs/`](docs/).

## License

MIT — see [LICENSE](LICENSE). OpenClaw itself is MIT-licensed and is not vendored here.
