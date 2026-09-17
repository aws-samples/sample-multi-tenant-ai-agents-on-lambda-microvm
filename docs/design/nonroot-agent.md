# Running the agent as non-root inside the MicroVM — what it buys, what it cannot

> Implemented on this branch and verified live on a dedicated stack (`openclaw-nr`,
> us-east-1, 2026-09-16). Companion to [storage-options.md](storage-options.md). The
> server-side alternative — one EFS Access Point + one IAM role per tenant — is on branch
> `feat/per-tenant-efs-isolation`; the last section compares the two.

## The question

The stock layout mounts the EFS **root** into every VM and bind-mounts the tenant's
subdirectory over the state dir. Everything in the VM runs as root with `ALL` OS
capabilities (mounting NFS needs them), so the agent's shell tool can simply
`ls /mnt/efs/tenants/` and read every tenant. Making `efs-monitor.sh` read-only does not
help: the script is not what grants access, the door is already open by the time the
agent runs, and root can undo any in-VM file protection anyway.

There are two places to enforce "a tenant sees only its own directory": on the EFS side
(access point + IAM, which forces a TLS mount and costs ~20s per cold start), or inside
the VM by taking privileges away from the agent. This document is the in-VM variant.

## Why file modes alone are not enough

NFS with `sec=sys` — the only flavour EFS speaks — trusts whatever uid the **client**
asserts. The kernel's NFS client fills in the calling process's uid, so `0700` directories
do constrain processes that go through the kernel mount. But a uid-1000 process that can
open a TCP connection to the mount target can speak NFS itself and put `uid 0` in the RPC
credential; the server has no way to tell. Measured in the stock VM: a uid-1000 process
connects to the mount target on port 2049 without any problem.

So the agent must be unable to **reach the server**, not just unable to read the mount.

## What the guest kernel allows (probed live)

Probed on the stock image with the agent's own exec tool, as root
(`6.1.166-24.303.amzn2023.aarch64`, no `/lib/modules`: whatever is missing is missing
for good):

| Mechanism | Result |
|---|---|
| iptables `-m owner` (nft and legacy backends) | **unavailable** — `xt_owner` not built in |
| nftables `inet` family | **unavailable** — "Operation not supported" |
| netfilter core + conntrack, `ip` filter table | present |
| network namespaces (`unshare -n`) | works |
| cgroup v2 (cpuset cpu io memory hugetlb pids), `/sys/fs/bpf` | present |
| **`ip rule … uidrange` + `ip route … prohibit`** | **works** — uid 1000 gets `EACCES` on the mount target while Bedrock endpoint, IMDS and root's own NFS mount are unaffected |

Policy routing by uid is what this variant uses: it needs only `iproute2`, no netfilter.

## What this variant does

1. **The agent is uid 1000 with nothing to escalate with** (`start.sh`). The gateway and
   the gw-bridge — and therefore every tool the agent can invoke — start through
   `setpriv --reuid=1000 --regid=1000 --clear-groups --bounding-set=-all --inh-caps=-all
   --no-new-privs`. Empty capability sets, and setuid/file-capability binaries execute as
   if they had no such bits. The image additionally strips every setuid bit
   (`mount`, `umount`, `su`, `passwd`, …). PID 1, the mount daemon and the sidecar stay root.
2. **The agent cannot reach the EFS server** (`efs-monitor.sh`, before anything is
   mounted). A policy-routing rule sends uid 1000's sockets through table 100, which holds
   only `prohibit` routes to the mount target IP(s) resolved from the file system's DNS
   name; everything else falls through to the main table. `connect()` to the mount target
   fails with `EACCES` for the agent and works for root.
3. **No path to other tenants' directories exists once adopted** (`efs-monitor.sh`). The
   EFS root and `/tenants` are `chmod 700` (modes live on the EFS inodes, so they hold in
   every VM); the tenant's directory is `0700` and owned by uid 1000; only it is
   bind-mounted over the state dir; and the EFS root mount is then **unmounted** — the
   bind keeps its own reference to the NFS superblock, so afterwards not even root has a
   path to `/tenants/<other>` short of mounting the file system again.
4. **The plugin tree is in the image, read-only** (`Dockerfile`, `start.sh`). OpenClaw
   accepts plugin files owned by the running uid *or root*, so the ~10k-file `npm` tree
   stays root-owned under `/opt/poc/npm` and is bind-mounted read-only into the state dir.
   The agent cannot edit the code it runs, and a first-generation seed copies a few dozen
   files to EFS instead of ten thousand.

Everything the root daemon writes into the tenant dir (`openclaw.json`, the model cache,
the healed sessions file) is chowned to uid 1000 afterwards, because the gateway must be
able to rewrite them.

## The root daemon must not trust the tenant's own directory

Giving the tenant ownership of its state directory creates a hazard that did not exist
while everything ran as root: the agent can leave things there for the **next generation**,
where a root daemon reads them. Three concrete paths, all closed:

- **One tenant id grammar, rejected rather than sanitized.** The sidecar used to filter
  with `str.isalnum()`, which is Unicode-aware, while the shell kept ASCII only. So
  `victim` plus any non-ASCII character passed the sidecar and became plain `victim` in the
  shell — one tenant served another's directory — and an id with no ASCII at all became the
  empty string, which made the tenant directory `/tenants` **itself**: the recursive chown
  and the bind would then have handed every tenant to the agent at once. Both sides now
  enforce `^[A-Za-z0-9_-]{1,64}$` and fail closed. Locked by
  [`../../tests/test_tenant_id_grammar.py`](../../tests/test_tenant_id_grammar.py), which
  runs the daemon's own `case` pattern against the Python grammar.
- **No symlink survives into a root write.** A tenant could leave
  `.models-cache.json -> ../<other>/openclaw.json` and have the root-run model materializer
  overwrite another tenant's config, or `openclaw.json -> /mnt/efs` and have a plain `chown`
  hand the mode-700 shared root to uid 1000 (verified in-image: `chown` without `-h` does
  follow the link; `-h` does not). A state directory seeded from the image contains no
  symlinks at all, so the daemon deletes every one it finds before touching anything, logs
  what it removed, and uses `chown -h`. The purge is safe to do once, at that point: the
  gateway is still running against local state, so nothing can re-create a link before the
  writes.
- **`/tenants` stays root-only at mode 700**, which is also what makes the tenant's own
  directory entry trustworthy — the agent cannot replace it with a symlink, because it
  cannot write the parent.

## Verified, not assumed

Each step the isolation rests on is checked, and a failure leaves the readiness marker
unset, which fails the cold start in the orchestrator rather than serving the tenant from
the wrong directory:

| Step | What would happen if it silently failed | Check |
|---|---|---|
| uid policy route installed | EFS mounts with the agent unconfined | every `ip` command's status, then the rule and each prohibit route re-read from the kernel |
| gateway stopped before the swap | binding over a live writer risks a torn database | `pgrep` after the wait; abort the attempt if it is still alive |
| tenant directory bound over the state dir | a healthy gateway on **local** state — the tenant's memory silently dies with the VM | both mounts re-read from `/proc/mounts`, including that the source ends in `:/tenants/<this tenant>` and that the plugin bind is `ro` |
| gateway and bridge back up | first turn lands on a gateway still opening its database | poll both, and if they never come up leave the marker unset |

Aborting undoes only what that pass mounted, innermost first, so the boot-time plugin bind
underneath survives and the gateway comes back on local state rather than with no plugins.
Once the mounts verify, readiness is polled separately and adoption is never re-run — that
would kill a gateway already serving this tenant's EFS-backed state.

## What it does not cover

- **Root code execution inside the VM is still game over.** A bug in the root sidecar, a
  kernel privilege escalation, or any other path to uid 0 sees every tenant, because the
  EFS side does no per-tenant enforcement in this variant. Only the access-point + IAM
  variant survives that.
- **All VMs share one execution role.** The agent can still use the IMDS credentials
  exactly as before (Bedrock + CloudWatch Logs). That role has no EFS permissions, so
  nothing new is reachable through the AWS API; the Bedrock-spend exposure is the one
  already listed in the top-level README.
- **The root sidecar is reachable from the agent** on `127.0.0.1:8080`. Its endpoints
  were reviewed for this: `/media` serves only files under the tenant's own
  `media/outbound/`; `/files` runs a fixed report; `/tenant` rewrites the tenant id, which
  is inert after adoption (the daemon reads it once); the build hook `/validate` can be
  triggered and re-mounts the EFS root at `/mnt/efs` for the duration of a prewarm — as
  root, `0700`, then unmounted — so the worst case is the agent restarting its own gateway.
- **One mount target.** The prohibit routes are resolved from the file system's DNS name,
  which yields the mount target of the VM's AZ. A multi-AZ file system needs every mount
  target's IP in table 100.
- **Every tenant is uid 1000.** Isolation rests on the routing rule and the `0700` root,
  not on distinct POSIX identities per tenant. Distinct identities enforced by the server
  are exactly what an access point provides.

## Verifying it from outside the VM

The sidecar's `/files` report gained three sections so the property can be checked
without a shell in the VM:

```
--- efs root (empty once adopted: unmounted after the bind) ---
--- gateway identity ---        pid, Uid 1000, CapEff 0, CapBnd 0, NoNewPrivs 1
--- uid routing ---             ip rule show; ip route show table 100
```

The live verification additionally drove the agent's own shell tool (which now runs as
uid 1000) through the attacks the variant is meant to stop: `id` and the capability
lines from `/proc/self/status`, `ls /mnt/efs` and `ls /mnt/efs/tenants/<other>`, a
`mount` attempt, a raw TCP connect to the mount target on 2049, and — as the control —
writing to its own state dir and connecting to the Bedrock endpoint.

The next-generation attack was run end to end as well: one tenant's agent planted
`openclaw.json -> /mnt/efs` and `.models-cache.json -> ../<other>/openclaw.json` in its own
EFS directory, then its VM was terminated and cold-started. The daemon logged both links as
removed, the other tenant's directory was untouched (its model cache kept its original
timestamp, no foreign files) and that tenant still recalled a memory stored two generations
earlier. The attacker's own state was re-seeded, because deleting the planted
`openclaw.json` makes the directory look like a first generation — self-inflicted, and the
alternative is following the link.

## Measured

Live on `openclaw-nr`, two tenants, arm64 / 2 GiB. Both isolation and correctness checks
pass, and the numbers below are the first ones this repository has for a gateway that
genuinely runs on EFS-backed state — see the correction in
[../perf/cold-start.md](../perf/cold-start.md#correction-the-12s-figure-was-never-efs-backed).

| | first cut | after stopping the gateway *before* the swap |
|---|---|---|
| Cold start, wall clock to a reply | 109s | **66s, 70s, 82s** |
| ↳ VM-internal `RUNNING` → `efsReady` | 105.8s | **47.5s, 47.9s, 50.0s, 55.3s** |
| ↳↳ gateway stop | 18.7s | **0.5s, 1.1s, 2.2s** |
| ↳↳ gateway start | 61.7s, 65.5s | **41.7s** |
| Warm turn | 14.8–23.9s | **12.7s** |
| First-generation seed: `chown -R` over NFS | 5.8s, 6.0s | unchanged |
| Adopted generation: ownership probe | 8–11ms (sentinel) | unchanged |
| Adopted generation: symlink purge walk | — | **141ms, 568ms** |

The postcondition checks cost nothing measurable; the symlink purge adds one walk of the
tenant tree, which is only cheap because the ~10k-file plugin tree lives in the image.

One outlier worth knowing: a cold start that re-seeds — first generation, or a generation
whose planted `openclaw.json` was purged — took **152.7s**, with 124s of that in the gateway
start and 90s of it before OpenClaw printed its first line. The `tar` seed writes tens of MB
through the page cache, which evicts the node bundle, and the restart then re-pays the
demand paging that [../perf/cold-start.md](../perf/cold-start.md#cause-1--demand-paged-disk-48s--32s)
describes. Steady-state generations do not tar and do not pay it.

Two findings worth keeping:

- **Stop the gateway before swapping its state dir, not after.** The original order bound
  the tenant directory over `~/.openclaw` while the gateway was still live, then killed it.
  Its clean shutdown closes the SQLite state database — which had just become an NFS file —
  and took 18.7s instead of 2.2s. Worse, the gateway exited holding a lock database whose
  name hashes the *resolved* state-db path, so the next start spent ~40s failing with
  `gateway already running (pid …); lock timeout` before the lease expired. On the
  second-generation cold start that pushed readiness past the orchestrator's patience and
  the first turn came back as HTTP 500; the retry succeeded. Killing first, then binding,
  then clearing `/tmp/openclaw-state-locks-*`, then releasing the supervisor removes all of
  it and yields exactly one gateway start.
- **The gateway restart is the cold start.** 41.7s of a 47.9s VM-internal startup is one
  OpenClaw boot against EFS-backed state (`slow OpenClaw agent database open` in its own
  log). This is plain NFSv4.1 with no TLS and no IAM, so it is *not* a cost of the
  access-point variant's mount helper — that variant measured 44–57s for the same step.
  Whatever fixes this has to change where the state database lives, not how EFS is mounted.

Not covered by this run: suspend/resume. The routing rule lives in the kernel's routing
table and is restored with the snapshot, and the hard NFS mount is expected to recover by
retransmission, but neither was exercised here.

## Compared with the access-point variant

| | This branch (in-VM) | `feat/per-tenant-efs-isolation` (server-side) |
|---|---|---|
| Where "own directory only" is enforced | inside the VM: uid, routing rule, modes | on EFS: access point root dir + IAM condition on the AP ARN |
| Survives root inside the VM | no | yes |
| EFS mount | plain NFSv4.1 (~2s) | efs-utils TLS + IAM via stunnel (~20–25s per cold start) |
| Per-tenant provisioning | registry row only | access point + IAM role per tenant (`add-tenant.sh`) |
| Per-turn cost with state on EFS | identical: it is the SQLite-on-NFS cost, see [../perf/cold-start.md](../perf/cold-start.md) | identical |
| Extra moving parts in the VM | `setpriv`, `iproute2` | efs-utils, stunnel, its watchdog |
