# IaC tooling support for AWS Lambda MicroVMs

> **Desk research, 2026-07-05, from AWS official docs (Knowledge MCP) + constructs.dev.**
> Answers PLAN.md §4 ("is there an `AWS::LambdaMicroVMs::*` CFN resource type?") and the
> SAM question. This is documentation research, NOT an in-account test — the create/run/
> suspend/resume flow was proven separately via a live CLI/SDK run. Confirmed vs. inferred
> is flagged per row.

## TL;DR

**CloudFormation is the source of truth and most complete. CDK is a close second (L1 only, no L2 yet). SAM and Terraform's standard `aws` provider have NO native abstraction — SAM can only pass the raw CFN resources through; Terraform users must switch to the `awscc` provider.**

The feature GA'd **2026-06-22**. IaC support splits cleanly along one axis: tools that consume the CloudFormation resource registry got it on day one; hand-authored abstractions lag by weeks-to-months.

## The two native CloudFormation resources (the foundation everything sits on)

Both added **2026-06-22**, in lockstep with the feature GA (source: CFN Template Reference doc-history):

| Resource | Purpose |
|---|---|
| `AWS::Lambda::MicrovmImage` | The MicroVM image (Firecracker snapshot). Key props: `BaseImageArn`, `BuildRoleArn`, `CodeArtifact` (S3 zip w/ Dockerfile), `CpuConfigurations`, `Hooks`, `EnvironmentVariables`, `EgressNetworkConnectors`, `Resources[].MinimumMemoryInMiB`. |
| `AWS::Lambda::NetworkConnector` | VPC egress path — provisions ENIs so a MicroVM can reach private resources. Props: `Configuration` (→ `VpcEgressConfiguration`: `SubnetIds` 1–16, `SecurityGroupIds` 0–5, `NetworkProtocol` IPv4/DualStack, `AssociatedComputeResourceTypes` — only allowed value today is `MicroVm`), `Name`, `OperatorRole`. |

Note the naming: it's `AWS::Lambda::*`, **not** `AWS::LambdaMicroVMs::*` as PLAN.md §4 speculated. The default ingress/egress connector ARNs used in the verified probe (`...aws:network-connector:aws-network-connector:ALL_INGRESS` / `INTERNET_EGRESS`, from the verified probe) are AWS-managed and don't require you to declare a `NetworkConnector` unless you need custom VPC egress.

## Support matrix

| Tool | Support | Basis | Notes |
|---|---|---|---|
| **CloudFormation** | ✅ Complete (source) | **Confirmed** | Both resources GA 2026-06-22, all properties present. |
| **CDK** | ✅ Complete, **L1 only** | **Confirmed** | `aws-cdk-lib` ≥ **v2.261.0** ships `CfnMicrovmImage` / `CfnNetworkConnector` (all langs: TS/Py/Go/.NET/Java), plus an `IMicrovmImageRef` interface. **No L2 construct yet** — you write at raw-CFN property altitude, no `sam build`-style convenience. |
| **SAM** | ⚠️ Pass-through only | **Confirmed** | No `AWS::Serverless::*` type for MicroVMs. Since a SAM template is a CFN superset, you can drop the two native resources straight into it and `sam deploy`. But `sam build` / `sam local invoke` / SAM sugar do **not** apply to them. GA announcement lists Console/CFN/CDK/Agent-Toolkit as deploy paths — **SAM is conspicuously absent** (contrast: the same-era "Lambda Managed Instances 32GB" announcement *does* list SAM). |
| **Terraform — `awscc`** | ⚠️ Very likely available | **Inferred** | The `hashicorp/awscc` provider is auto-generated from the CFN resource registry, so it should expose `awscc_lambda_microvm_image` / `awscc_lambda_network_connector`. **Not version-verified** (registry page is JS-rendered; couldn't scrape). Run `terraform providers schema` on your pinned version to confirm. |
| **Terraform — standard `aws`** | ❌ Not yet | **Confirmed (absent)** | `hashicorp/aws` is hand-maintained; the MicroVM resources are not in it as of this research. New AWS resources typically land here weeks-to-months after GA. |
| **Pulumi — `aws-native`** | ⚠️ Very likely available | **Inferred** | Same mechanism as `awscc` (derived from CFN schema). Not verified. |
| **Pulumi — classic `aws`** | ❌ Not yet | **Inferred** | Wraps the Terraform `aws` provider → same lag. |

## Why the split (the mechanism that predicts all of the above)

New-AWS-feature IaC support is decided by **which generation pipeline a tool rides**:

- **Schema-derived (day-one):** CloudFormation → CDK L1 (`Cfn*`), Terraform `awscc`, Pulumi `aws-native`. These consume the CFN resource registry directly, so they appear the moment the CFN resource ships.
- **Hand-authored (lags):** Terraform `hashicorp/aws`, Pulumi classic, CDK **L2** constructs, SAM `AWS::Serverless::*`. Each needs a human to design an ergonomic wrapper → weeks-to-months behind. All of these currently lack native MicroVM support.

## Recommendation (for this PoC)

Aligns with PLAN.md's CDK choice:

1. **CDK with L1 constructs** — `CfnMicrovmImage` + `CfnNetworkConnector`. Officially listed deploy path, full property coverage, and CDK still handles the durable infra (S3 artifact bucket, IAM build/exec roles, log group, Bedrock wiring) as L2. Missing L2 for the MicroVM itself just means raw-property altitude — no functional loss. **The lifecycle calls (`create-microvm-image` build, `run-microvm`, suspend/resume) are still API/CLI-driven** regardless of IaC tool — IaC declares the image + connectors, not the running-instance lifecycle.
2. **Plain CloudFormation** — if avoiding the CDK toolchain; most source-faithful, zero deps.
3. **Terraform** — use `awscc`, not `aws`; verify the resource exists in your pinned provider version first.

## Honesty flags
- CloudFormation + CDK L1 rows: **directly confirmed** against AWS docs / constructs.dev (aws-cdk-lib v2.261.0).
- Terraform `awscc` + Pulumi `aws-native` rows: **inferred from the auto-generation mechanism**, not version-checked. Verify before relying on them.
- This is docs research only. The actual create/run/suspend/resume flow was empirically proven separately via CLI/SDK. IaC (CFN/CDK) was **not** used in that verified run.
- SAM could gain native support later (typical AWS cadence: CFN resource first → tooling follows). Watch the aws-sam-cli repo and SAM spec.
