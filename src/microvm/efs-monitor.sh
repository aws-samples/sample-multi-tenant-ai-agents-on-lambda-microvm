#!/usr/bin/env bash
# Tenant-aware persistent EFS mount daemon (root).
# Stays COMPLETELY quiet during image build (hard-NFS against unreachable IP wedges the
# snapshot). Waits until the orchestrator injects a tenant id via the sidecar (/run hook
# or POST /tenant -> /var/run/tenant-id), then:
#   1. cuts uid 1000 — the gateway and every agent tool, see start.sh — off from the EFS
#      mount target at the routing layer;
#   2. mounts the EFS root and makes it and /tenants traversable by root only;
#   3. seeds/adopts THIS tenant's subdir (owned by uid 1000);
#   4. stops the gateway, binds that subdir over the state dir, unmounts the EFS root so
#      no path to other tenants' dirs is left, and starts the gateway again.
# Why step 1 exists: NFS with sec=sys trusts whatever uid the CLIENT asserts. File modes
# bind the kernel's NFS client, but a uid-1000 process speaking NFS itself over TCP could
# claim uid 0 — so the agent must not be able to reach the server at all. What none of
# this covers: root code execution inside the VM still sees every tenant, because the
# EFS side does no per-tenant enforcement in this variant.
# Every step that the isolation rests on is verified, not assumed: an unusable tenant id,
# a missing route, a gateway that will not die and a bind that did not take all abort the
# attempt and leave the readiness marker unset, which fails the cold start in the
# orchestrator. Serving a tenant from the wrong directory — or from local state that dies
# with the VM — is worse than not serving it.
STATE_DIR=/home/node/.openclaw
EFS_DIR=/mnt/efs
MARKER=/var/run/efs-mounted
TENANT_FILE=/var/run/tenant-id
MAINT=/var/run/openclaw-maintenance   # start.sh won't launch the gateway while this exists
AGENT_UID=1000
NPM_IMG=/opt/poc/npm
# The gateway retitles its process to "openclaw-gateway" (2026.9+); the anchor keeps
# pkill/pgrep from matching their own command line.
GW='^(openclaw-gateway|node /app/openclaw.mjs gateway)'
# Policy-routing table consulted only for uid 1000's sockets. It holds nothing but
# prohibit routes to the mount target(s); every other destination falls through to main.
UID_TABLE=100
# Mount target: prefer the EFS DNS name (regional, resolves to the AZ mount-target
# IP inside the VPC) built from EFS_ID; fall back to an explicit EFS_MOUNT_IP.
if [ -n "${EFS_ID:-}" ]; then
  EFS_HOST="${EFS_ID}.efs.${AWS_REGION:-us-east-1}.amazonaws.com"
elif [ -n "${EFS_MOUNT_IP:-}" ]; then
  EFS_HOST="${EFS_MOUNT_IP}"
else
  echo "[efs-monitor] need EFS_ID or EFS_MOUNT_IP"; exit 1
fi

# `ip rule … uidrange` needs no netfilter — the guest kernel has neither xt_owner nor
# the nft inet family, verified live — and `prohibit` makes connect() fail with EACCES.
# The template creates one mount target, so DNS yields one IP; a multi-AZ file system
# would have to list every mount target here.
# Fails closed: this block is the only thing stopping a uid-1000 process from speaking
# NFS to the mount target itself, so every command is checked AND the result re-read.
# Returning 0 with a rule silently missing would mount EFS with the agent unconfined.
block_agent_from_efs() {
  local ips a
  ips=$(getent ahostsv4 "$EFS_HOST" 2>/dev/null | awk '{print $1}' | sort -u)
  [ -n "$ips" ] || return 1
  ip rule show | grep -q "uidrange ${AGENT_UID}-${AGENT_UID} lookup ${UID_TABLE}" \
    || ip rule add uidrange "${AGENT_UID}-${AGENT_UID}" lookup "$UID_TABLE" priority 100 \
    || return 1
  for a in $ips; do
    ip route replace prohibit "${a}/32" table "$UID_TABLE" || return 1
  done
  ip rule show | grep -q "uidrange ${AGENT_UID}-${AGENT_UID} lookup ${UID_TABLE}" || return 1
  for a in $ips; do
    ip route show table "$UID_TABLE" | grep -qF "prohibit ${a}" || return 1
  done
  echo "[efs-monitor] uid ${AGENT_UID} prohibited from reaching $(echo $ips) (table ${UID_TABLE})"
}

# The marker claims "this tenant's EFS dir is the state dir". Read that back from
# /proc/mounts instead of trusting two exit codes: if the bind silently did not take, the
# gateway would come up healthy on local state and lose everything at termination.
# Suffix compared with index() rather than a regex — no metacharacter surprises.
mounts_ok() {
  awk -v sd="$STATE_DIR" -v suf=":/tenants/$TENANT" '
    $2 == sd && index($1, suf) == length($1) - length(suf) + 1 { a = 1 }
    $2 == sd "/npm" && $4 ~ /(^|,)ro(,|$)/ { b = 1 }
    END { exit !(a && b) }' /proc/mounts
}

gateway_ready() {
  curl -sf -o /dev/null http://127.0.0.1:18789/healthz \
    && curl -sf http://127.0.0.1:8090/ready | grep -q '"connected":true'
}

mkdir -p "$EFS_DIR"
until [ -s "$TENANT_FILE" ]; do sleep 2; done
TENANT=$(cat "$TENANT_FILE")
# One ASCII grammar for the tenant id, rejected rather than sanitized a second time.
# hooks.py used to filter with str.isalnum(), which is Unicode-aware, while a `tr -cd`
# here keeps ASCII only: "victim<non-ascii>" would have collapsed onto tenant "victim",
# and an all-non-ASCII id onto the empty string, making TDIR the whole tenants directory
# — which the recursive chown and the bind would then hand to the agent wholesale.
case $TENANT in
  "" | *[!A-Za-z0-9_-]*)
    echo "[efs-monitor] refusing tenant id from $TENANT_FILE: not ^[A-Za-z0-9_-]{1,64}$"
    exit 1 ;;
esac
if [ "${#TENANT}" -gt 64 ]; then
  echo "[efs-monitor] refusing tenant id from $TENANT_FILE: longer than 64 characters"
  exit 1
fi
echo "[efs-monitor] tenant '$TENANT' assigned; mount target ${EFS_HOST}; starting mount attempts"

# 1. Before anything is mounted: the agent uid must never be able to reach the server.
until block_agent_from_efs; do
  echo "[efs-monitor] uid ${AGENT_UID} block for ${EFS_HOST} not in place yet; retrying"; sleep 2
done

ADOPTED=
while true; do
  if [ -z "$ADOPTED" ]; then
    if timeout 15 mount -t nfs4 -o nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=150,retrans=2 \
         "${EFS_HOST}:/" "$EFS_DIR" 2>/tmp/efs-mount.err; then
      echo "[efs-monitor] EFS mounted from ${EFS_HOST} at $(date -u +%FT%TZ)"
      # 2. Only root traverses the shared root and the tenants directory. These modes
      #    live on the EFS inodes, so they hold for every VM; idempotent, three round trips.
      #    They are also what makes $TDIR itself trustworthy: with /tenants writable by
      #    root only, the agent cannot replace its own directory entry with a symlink.
      chmod 700 "$EFS_DIR"
      mkdir -p "$EFS_DIR/tenants"; chmod 700 "$EFS_DIR/tenants"
      TDIR="$EFS_DIR/tenants/$TENANT"
      mkdir -p "$TDIR"
      # The tenant owns this tree as uid 1000 and can leave symlinks in it for the next
      # generation, which this root daemon would then follow: `.models-cache.json ->
      # ../<other>/openclaw.json` redirects the model materializer, and a plain chown
      # through `-> /mnt/efs` hands the mode-700 shared root to uid 1000 (verified: chown
      # without -h dereferences). A state dir seeded from the image contains no symlinks
      # at all, so drop every one before root touches anything. Safe to do once here: the
      # gateway is still running against LOCAL state, so nothing can re-create one before
      # the writes below.
      # Costs a walk of the tenant tree — one NFS round trip per entry, which is why the
      # ~10k-file plugin tree lives in the image and not here.
      T0=$(date +%s%3N)
      SL=$(find "$TDIR" -type l -print -delete 2>/dev/null)
      [ -n "$SL" ] && echo "[efs-monitor] removed symlinks planted in the tenant tree: $(echo $SL)"
      echo "[efs-monitor] timing: symlink purge $(( $(date +%s%3N) - T0 ))ms"
      if [ ! -f "$TDIR/openclaw.json" ]; then
        echo "[efs-monitor] tenant $TENANT first generation: seeding from local state"
        # Plugins stay in the image (root-owned, bound read-only below), so the seed is
        # a few dozen files, not ~10k.
        ( cd "$STATE_DIR" && tar -cf - --exclude=./npm . | tar -xf - -C "$TDIR" )
        mkdir -p "$TDIR/npm"
      else
        echo "[efs-monitor] tenant $TENANT has prior state - adopting it"
      fi
      # 3. The tenant dir belongs to the agent uid. tar preserved the image's node:node
      #    ownership, so a fresh seed only needs this once; a dir written by an older,
      #    root-running image gets one recursive fixup. Sentinel-guarded because chown
      #    over NFS is a round trip per file.
      OWNED_MARK="$TDIR/.owned-by-agent-uid"
      T0=$(date +%s%3N)
      if [ ! -f "$OWNED_MARK" ]; then
        chown -R "${AGENT_UID}:${AGENT_UID}" "$TDIR"
        : > "$OWNED_MARK"; chown "${AGENT_UID}:${AGENT_UID}" "$OWNED_MARK"
        echo "[efs-monitor] tenant dir owned by uid ${AGENT_UID} in $(( $(date +%s%3N) - T0 ))ms"
      fi
      chmod 700 "$TDIR"
      echo "[efs-monitor] timing: ownership step $(( $(date +%s%3N) - T0 ))ms"
      # Config is image-owned; only agent state persists across generations. Without
      # this, a stale EFS openclaw.json shadows every config change shipped in the image.
      # Also drop OpenClaw's backups: its watchdog restores .last-good over any config
      # that lacks the meta stamp (missing-meta-vs-last-good), undoing the overwrite.
      T0=$(date +%s%3N)
      cp /opt/poc/openclaw.json "$TDIR/openclaw.json"
      rm -f "$TDIR/openclaw.json.last-good" "$TDIR/openclaw.json.bak"
      echo "[efs-monitor] timing: config copy $(( $(date +%s%3N) - T0 ))ms"
      # Materialize live Bedrock model discovery into the config. The gateway's image
      # guard and models list only read the explicit models array, never the plugin's
      # live catalog — so we bake the discovered catalog (with correct text+image
      # modalities) in here. Discovery costs ~1.6s and the catalog changes on the order
      # of weeks, so cache it per tenant and reuse until MODEL_CACHE_TTL elapses; on
      # any failure the static seed list shipped in the image stays as fallback.
      T0=$(date +%s%3N)
      node /opt/poc/materialize-models.mjs "$TDIR/openclaw.json" "$TDIR/.models-cache.json" \
        || true
      echo "[efs-monitor] timing: materialize-models $(( $(date +%s%3N) - T0 ))ms"
      # Clear per-session /model pins on cold start. A pin to a since-retired Bedrock
      # model fails every turn INCLUDING the /model command that would clear it,
      # permanently deadlocking the session. Cold start (reap -> relaunch) is the safe
      # reset point: the gateway isn't running against this dir yet. Pins still
      # survive suspend/resume; they only reset when the tenant went fully cold.
      T0=$(date +%s%3N)
      python3 - "$TDIR/agents/main/sessions/sessions.json" <<'HEAL' 2>/dev/null || true
import json, sys
p = sys.argv[1]
try:
    d = json.load(open(p))
except Exception:
    sys.exit(0)
changed = False
for k, e in d.items():
    if not isinstance(e, dict) or not e.get("modelOverride"):
        continue
    print(f"[efs-monitor] clearing model pin on {k}: {e['modelOverride']}")
    for f in ("providerOverride", "modelOverride", "modelOverrideSource",
              "model", "modelProvider"):
        e.pop(f, None)
    changed = True
if changed:
    json.dump(d, open(p, "w"), indent=1)
HEAL
      echo "[efs-monitor] timing: session heal $(( $(date +%s%3N) - T0 ))ms"
      # Everything this root daemon just wrote must be the gateway's to rewrite. -h so
      # this cannot dereference: the symlink purge above already cleared the tree, but a
      # chown that follows a tenant-controlled path is the one that could give away the
      # shared root.
      chown -h "${AGENT_UID}:${AGENT_UID}" "$TDIR/openclaw.json" "$TDIR/.models-cache.json" \
        "$TDIR/agents/main/sessions/sessions.json" 2>/dev/null
      # 4. Stop the gateway BEFORE swapping its state dir. Doing it the other way round
      #    leaves a live process whose open handles point at the old (local) inodes while
      #    new lookups resolve to EFS, and its shutdown then writes state to a mix of the
      #    two. MAINT keeps start.sh from relaunching in the gap. pkill only signals, so
      #    wait for the process to actually go: a clean shutdown closes the SQLite state
      #    db and was measured at ~19s on NFS.
      echo "[efs-monitor] holding the gateway down to swap in EFS-backed state for $TENANT"
      T0=$(date +%s%3N)
      : > "$MAINT"
      pkill -f "$GW" || true
      for _ in $(seq 1 120); do pgrep -f "$GW" >/dev/null || break; sleep 0.5; done
      if pgrep -f "$GW" >/dev/null; then
        # Binding over the state dir of a process that is still writing it risks a torn
        # SQLite database, and two gateways on one dir is exactly what the lock files
        # below exist to prevent. Back out and let the next pass try again.
        echo "[efs-monitor] gateway still alive 60s after SIGTERM; aborting this attempt"
        rm -f "$MAINT"
        umount "$EFS_DIR" 2>/dev/null || umount -l "$EFS_DIR" 2>/dev/null
        sleep 5; continue
      fi
      echo "[efs-monitor] timing: gateway stop $(( $(date +%s%3N) - T0 ))ms"
      T0=$(date +%s%3N)
      BOUND_T= ; BOUND_N=
      mount --bind "$TDIR" "$STATE_DIR" && BOUND_T=1
      # The boot-time plugin bind is now hidden under the tenant bind; re-apply it.
      [ -n "$BOUND_T" ] && mount --bind -o ro "$NPM_IMG" "$STATE_DIR/npm" && BOUND_N=1
      if [ -z "$BOUND_T" ] || [ -z "$BOUND_N" ] || ! mounts_ok; then
        echo "[efs-monitor] $STATE_DIR is NOT backed by tenants/$TENANT after the bind; aborting this attempt"
        # Undo only what this pass mounted, innermost first, so the boot-time plugin bind
        # underneath survives and the gateway comes back on local state rather than on a
        # state dir with no plugins.
        [ -n "$BOUND_N" ] && umount "$STATE_DIR/npm" 2>/dev/null
        [ -n "$BOUND_T" ] && umount "$STATE_DIR" 2>/dev/null
        rm -f "$MAINT"
        umount "$EFS_DIR" 2>/dev/null || umount -l "$EFS_DIR" 2>/dev/null
        sleep 5; continue
      fi
      echo "[efs-monitor] timing: bind mount $(( $(date +%s%3N) - T0 ))ms"
      # The bind holds its own reference to the NFS superblock, so the root mount can go.
      # Afterwards nothing in the VM — root included — has a path to another tenant's
      # directory short of mounting the file system again.
      umount "$EFS_DIR" 2>/dev/null || umount -l "$EFS_DIR"
      echo "[efs-monitor] EFS root unmounted; only tenants/$TENANT stays reachable, as $STATE_DIR"
      # OpenClaw coordinates state writers through lock databases under the runtime dir
      # (/tmp/openclaw-state-locks-<uid>/), named after a hash of the RESOLVED state-db
      # path — which just changed. A gateway that exits while holding one leaves it
      # claiming the dead pid, and the next start then fails with "gateway already
      # running (pid …); lock timeout" for ~40s of retries. Nothing holds these now (the
      # gateway is gone; the sidecar and this daemon are not state writers), so clear them.
      rm -rf /tmp/openclaw-state-locks-* 2>/dev/null
      # 5. Release the supervisor: exactly one gateway start, against EFS-backed state.
      #    Wait for it to serve AND for the bridge's WebSocket to it — otherwise the
      #    orchestrator's first turn lands on a gateway that is still opening its
      #    database, or on a bridge that has not reconnected yet.
      echo "[efs-monitor] state dir now EFS-backed for tenant $TENANT; starting gateway"
      T0=$(date +%s%3N)
      rm -f "$MAINT"
      ADOPTED=1
      READY=
      for _ in $(seq 1 180); do
        gateway_ready && { READY=1; break; }
        sleep 1
      done
      if [ -n "$READY" ]; then
        echo "[efs-monitor] timing: gateway start $(( $(date +%s%3N) - T0 ))ms"
        touch "$MARKER"
      else
        echo "[efs-monitor] gateway/bridge still not ready 180s after the swap; marker stays unset"
      fi
    fi
  elif [ ! -f "$MARKER" ]; then
    # The mounts are verified and in place; only readiness was missing. Keep polling for
    # it — never re-run the adoption, which would kill a gateway that may by now be
    # serving this tenant's EFS-backed state.
    if gateway_ready; then
      echo "[efs-monitor] gateway/bridge ready; $STATE_DIR is EFS-backed for $TENANT"
      touch "$MARKER"
    fi
  fi
  sleep 5
done
