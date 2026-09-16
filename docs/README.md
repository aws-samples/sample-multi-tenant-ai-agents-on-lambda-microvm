# Docs

The "why" behind the code in [`../src/`](../src/): decisions made while building, and
measurements taken after.

## perf/ — measured tuning of the running system

| File | What it covers |
|---|---|
| [cold-start.md](perf/cold-start.md) | Cold start 48s → ~12s: image-build hooks (`/ready`, `/validate` page prefetch, `/run`), the NFS `chown` and model-discovery costs, the prefetch ceiling, and two rejected approaches |

## design/ — decisions made before/while building

| File | What it covers |
|---|---|
| [PLAN.md](design/PLAN.md) | The original phased PoC plan and agent-as-swappable-box contract |
| [openclaw-adaptation.md](design/openclaw-adaptation.md) | Adapting OpenClaw to the MicroVM shape (ports, state, IM-gateway vs idle-suspend tension) |
| [storage-options.md](design/storage-options.md) | Why EFS (over S3/Mountpoint) for cross-generation state, and the production-hardening open items |
| [nonroot-agent.md](design/nonroot-agent.md) | Confining the agent inside the VM: uid 1000 with no capabilities, uid policy routing to cut it off from the EFS server (the guest kernel has no iptables owner match), root-only EFS root — and why root-in-VM still defeats it |
| [iam-bedrock-minipoc.md](design/iam-bedrock-minipoc.md) | Design of the smallest gate: CLI-driven IAM→Bedrock credential probe |
| [design-orchestrator.md](design/design-orchestrator.md) | Multi-tenant orchestrator: two-branch router, three-temperature lifecycle, push-vs-poll, why no proactive renewal |
| [iac-tooling-support.md](design/iac-tooling-support.md) | IaC tooling matrix: CFN has native `AWS::Lambda::MicrovmImage`/`NetworkConnector` (CDK L1 only; SAM/Terraform-aws lag) — why `src/` is CFN-native |
