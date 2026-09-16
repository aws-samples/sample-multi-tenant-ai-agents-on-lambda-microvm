# State persistence on Lambda MicroVMs — storage options & decision

> Design decision (why EFS). The empirical proof that EFS actually survives a MicroVM
> generation swap was run separately; this doc is the reasoning, not the run log.

> [!WARNING]
> **The premise of the comparison below has expired.** S3 + Mountpoint was rejected because
> it cannot append to OpenClaw's `.jsonl` session logs — and those files no longer exist:
> 2026.9 keeps sessions in a single SQLite database. That database on NFS is now the dominant
> cost of the whole system (~13s per warm turn against ~2s on local disk, ~42s of every cold
> start). The trade-off needs re-deriving from the current storage shape, not patching.
> Measurements: [../perf/cold-start.md](../perf/cold-start.md#correction-the-12s-figure-was-never-efs-backed).

## The problem

A MicroVM's snapshot (guest disk + RAM) dies with the instance at the 8-hour cap. For an
agent whose value is accumulated memory, state must live **outside** the instance so that
terminate + relaunch = same agent, same memory. The 8-hour cap should be a *compute*
boundary, not a *state* boundary.

## Options assessed

| Option | Verdict |
|---|---|
| **EFS via in-guest NFS mount** | ✅ **Chosen.** Full POSIX, survives termination. Cost: needs `ALL` OS capabilities + a VPC egress connector + (because that connector removes the internet path) a Bedrock VPC endpoint for the workload's AWS calls. |
| S3 + Mountpoint (FUSE) | Possible (`ALL` caps) but **no append/random-write** — breaks OpenClaw's `.jsonl` session appends. Not suitable. |
| Lambda "S3 Files" / function EFS config | **Lambda-function-only features** — no MicroVM API surface exposes them. |
| App-level `aws s3 sync` on the suspend hook | Pragmatic fallback; no VPC needed; but only eventually-consistent state and loses writes between syncs. |

**Decision:** EFS, mounted at runtime, bind-mounted over the agent's data dir. It's the only
option giving full POSIX semantics (which the append-only session log requires) plus
survival across instance generations. The VPC-endpoint tax that comes with it is documented
as gotcha #2 in the deployment notes ([`../../src/README.md`](../../src/README.md)).

## Production hardening (open items, not needed for the PoC)

- **EFS Access Point + non-root POSIX identity.** ~~The root-run gateway writes root-owned
  files onto EFS.~~ Partly done: the gateway now runs as uid 1000 and its tenant directory is
  owned by that uid, enforced inside the VM
  ([nonroot-agent.md](nonroot-agent.md)). An Access Point would move the enforcement to the
  EFS side, which is the part that survives root code execution in a VM.
- **One live instance per EFS state dir.** EFS enables *shared* state, but OpenClaw is
  single-instance — enforce a single live writer per tenant state dir (the same invariant as
  "one Telegram poller per bot token").
- **Suspend/resume with the NFS mount held.** Resume kills outbound connections; a hard NFS
  mount should recover via retransmission rather than a remount, but this specific path
  wasn't exercised in depth.
