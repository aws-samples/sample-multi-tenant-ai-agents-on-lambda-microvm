# Docs

The "why" behind the code in [`../src/`](../src/): the design decisions made while building.

## design/ — decisions made before/while building

| File | What it covers |
|---|---|
| [PLAN.md](design/PLAN.md) | The original phased PoC plan and agent-as-swappable-box contract |
| [openclaw-adaptation.md](design/openclaw-adaptation.md) | Adapting OpenClaw to the MicroVM shape (ports, state, IM-gateway vs idle-suspend tension) |
| [storage-options.md](design/storage-options.md) | Why EFS (over S3/Mountpoint) for cross-generation state, and the production-hardening open items |
| [iam-bedrock-minipoc.md](design/iam-bedrock-minipoc.md) | Design of the smallest gate: CLI-driven IAM→Bedrock credential probe |
| [design-orchestrator.md](design/design-orchestrator.md) | Multi-tenant orchestrator: two-branch router, three-temperature lifecycle, push-vs-poll, why no proactive renewal |
| [iac-tooling-support.md](design/iac-tooling-support.md) | IaC tooling matrix: CFN has native `AWS::Lambda::MicrovmImage`/`NetworkConnector` (CDK L1 only; SAM/Terraform-aws lag) — why `src/` is CFN-native |
