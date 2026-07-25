# Cold-Start Optimization — 48s → ~12s

> Measured on a live stack (`openclaw-mt`, us-east-1, arm64, 2 GiB) over 2026-07-25,
> tenant `captain` with pre-existing EFS state. Every number below comes from a real
> cold start (`cold: True`), three samples per revision. Nothing here is estimated.

## Result

| Metric | Before | After |
|---|---|---|
| **Wall clock to a reply** (what a user feels) | **48s** | **11–13s** |
| VM-internal startup | — | 4.3s |
| ↳ `run_microvm` → `RUNNING` (platform snapshot restore) | — | 2.1–3.2s |
| ↳ `RUNNING` → `efsReady` | — | 2.1s |
| ↳↳ of which: NFS connect | — | 2.0s |
| ↳↳ of which: whole tenant-adoption flow | — | 0.25s |

The ~7–8s of wall clock not accounted for by startup is **LLM generation plus the
orchestrator Lambda's own cold init** — the gateway acks a turn in 60–96 ms and the rest
is the model emitting tokens. So even a zero-cost startup would not go below ~8s wall
clock. Optimize startup, but measure it separately from the reply.

Progression, three cold starts per image version:

| Image | Wall clock | What changed |
|---|---|---|
| v27 | 48s | baseline, no hooks |
| v28 | 32 / 29 / 32s | `/ready` + `/validate` + `/run` hooks enabled |
| v29 | 33 / 32 / 31s | prewarm coverage fixed — **no change** (see [Ceiling](#the-prefetch-ceiling)) |
| v31 | 18 / 16 / 18s | NFS ownership walk made conditional |
| v32 | 13 / 13 / 11s | model-discovery cache + ownership sentinel |

## Cause 1 — demand-paged disk (48s → 32s)

Memory is restored eagerly from the snapshot, but **disk pages fault in lazily** at a few
MB/s. A fresh VM therefore re-pays for every byte of toolchain it touches: the NFS client,
node, the Bedrock plugin tree.

The platform's image-build hooks fix this, and are enabled on `AWS::Lambda::MicrovmImage`
via `Hooks.Port` + `MicrovmImageHooks` / `MicrovmHooks`:

- **`/ready`** gates the snapshot. The platform captures disk+memory only once this
  returns 200, so answering late (gateway healthy *and* the gw-bridge WebSocket
  established) bakes a warm gateway into every future launch.
- **`/validate`** runs on a throwaway VM restored *from* that fresh snapshot. While it
  runs, the platform samples which snapshot pages get touched and **prefetches exactly
  those** on all later launches. This is the actual lever.
- **`/run`** carries the tenantId as `runHookPayload`, so the sidecar knows its tenant
  before the endpoint accepts traffic — no post-launch assign-tenant round trip, and
  `efs-monitor` starts mounting while the orchestrator is still waiting for `RUNNING`.

Because prefetch is driven by *what validate touches*, `hooks.py`'s `prewarm()` walks the
**real** cold-start path — NFS mount, the adoption shell toolchain, model discovery, a
gateway bounce, then agent turns — rather than sweeping files at random. Every step is
*expected* to fail in that environment (no tenant EFS route); the page faults are the
payload, not the results.

### The build role needs Bedrock permissions

This is the non-obvious prerequisite. Without `bedrock:ListFoundationModels` /
`ListInferenceProfiles` / `InvokeModel` on the **build** role (`MicroVMBuildRole`, not just
the execution role), discovery and the turn probe die at IAM with `AccessDeniedException`
*before any SDK, TLS, or catalog-parsing code runs*. The pages that dominate cold start
then never get sampled. Symptom: prewarm finishes suspiciously fast (18s) and buys nothing.

### Both build hooks must self-release

A 503 must be returned **immediately** — a request held open past the hook timeout ends
the build. And a hook that can never answer 200 fails the build outright, so:

- `/ready` has a `READY_GRACE_S` escape hatch: past it, answer 200 anyway (logged) and let
  the platform snapshot an unready VM. That is exactly the pre-hooks behavior — a
  degradation, not a failure.
- `/validate` releases in a `finally`, so a crashed or wedged `prewarm()` cannot wedge the
  build.
- `prewarm()`'s NFS mount is `soft` + wrapped in `timeout`. A hard mount that hangs is
  unkillable and would stall validation until the platform kills the build.

## Cause 2 — `chown -R` over NFS (32s → 17s → 3ms)

The next chunk was not paging at all. `efs-monitor` re-owns the plugin tree to root
(OpenClaw refuses plugins with "suspicious ownership"), and a recursive chown over NFS
costs **one round trip per file**: 17.5s, more than half the cold start at that point. Yet
it only ever matters for trees a *previous generation* seeded wrong — in the steady state
it is pure waste.

**The trap:** the first fix was a `find "$TDIR/npm" ! -uid 0 -print -quit` probe, and it
was **worse — 19.8s**. `-quit` short-circuits only when it *finds* an offender, so the
common all-correct case still walked every file, paying the very round trips it was meant
to avoid. Measure the fix, not just the bug.

The working fix is a sentinel file (`.npm-root-owned`) written after a successful fixup,
making the probe a single `stat`: **19774ms → 3ms**.

## Cause 3 — model discovery on every cold start (1.8s → 85ms)

`materialize-models.mjs` exists to paper over an OpenClaw gap: the gateway's image guard
and `models list` read only the explicit `models` array in config, never the plugin's live
catalog. So discovery runs at cold start and writes its result *into* the config.

But the Bedrock catalog changes on the order of weeks, while this ran on every cold start.
It now caches per tenant on EFS (`.models-cache.json`) with a 24h TTL. Stale, empty,
corrupt, and future-dated cache files all degrade to live discovery — verified by unit
test, since a bad cache would silently pin a stale model list.

## Investigated and rejected — the gateway bounce

`efs-monitor` pkills the gateway after binding EFS so it restarts against EFS-backed
state. This *looks* like a wasted restart worth eliminating, and the tempting fix is to
make the gateway survive a state-dir swap.

The logs disprove the premise: the bridge sends its first request **0.2s after the pkill**
and gets an ack in 60–96 ms, and no `[start] gateway exited` line ever appears. The bounce
neither blocks the first request nor costs a measurable restart. Don't trade correctness
(does OpenClaw tolerate its state directory being replaced under it?) for ~0s.

## The prefetch ceiling

The single most useful finding: **once `/validate` is sampling properly, more prewarm
coverage buys nothing.** Fixing prewarm from 18s of broken coverage to 34s of real
coverage — turn probes going from IAM-denied to fully succeeding, plus the gateway restart
now sampled — moved cold start from 32s to 32s. Zero.

Past that point the bottleneck is the VM's own startup path, not paging. Profile it
instead of adding prewarm steps.

## How to re-measure

Per-step instrumentation is deliberately left in the logs — it is what found every cause
above:

- `[efs-monitor] timing: <step> <n>ms` — config copy, materialize-models, ownership,
  session heal, bind mount
- `[cold] <tenant> timing: run->RUNNING …, RUNNING->efsReady …` — orchestrator side
- `[prewarm] …` in the build/validate VM's log stream — which prewarm steps ran, and their
  exit codes

To force a genuine cold start: `terminate-microvm`, poll until `TERMINATED`, set the
tenant's registry `state` to `COLD`, then send a turn. Checking the registry alone is not
enough — it can say `RUNNING` while `list-microvms` reports nothing, and a stale VM will
happily serve the turn as a warm one (`cold: False`), which silently invalidates the
measurement.

**The regression check that matters** is not latency but state continuity: terminate the
VM, cold-start a new one, and ask an *existing* session to recall something only the EFS
transcript knows. Both optimizations here touch the adoption flow, and a subtly broken
bind-mount or ownership fix would show up as an agent that has lost its memory, not as a
slow start.
