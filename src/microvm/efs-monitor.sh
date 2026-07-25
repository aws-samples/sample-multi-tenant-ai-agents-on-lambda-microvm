#!/usr/bin/env bash
# Tenant-aware persistent EFS mount daemon.
# Stays COMPLETELY quiet during image build (hard-NFS against unreachable IP
# wedges the snapshot). Waits until the orchestrator injects a tenant id via
# the sidecar (POST /tenant -> /var/run/tenant-id), then mounts the EFS and
# binds THIS TENANT's subdir over the state dir, then bounces the gateway.
STATE_DIR=/home/node/.openclaw
EFS_DIR=/mnt/efs
MARKER=/var/run/efs-mounted
TENANT_FILE=/var/run/tenant-id
# Mount target: prefer the EFS DNS name (regional, resolves to the AZ mount-target
# IP inside the VPC) built from EFS_ID; fall back to an explicit EFS_MOUNT_IP.
if [ -n "${EFS_ID:-}" ]; then
  EFS_HOST="${EFS_ID}.efs.${AWS_REGION:-us-east-1}.amazonaws.com"
elif [ -n "${EFS_MOUNT_IP:-}" ]; then
  EFS_HOST="${EFS_MOUNT_IP}"
else
  echo "[efs-monitor] need EFS_ID or EFS_MOUNT_IP"; exit 1
fi

mkdir -p "$EFS_DIR"
until [ -s "$TENANT_FILE" ]; do sleep 2; done
TENANT=$(tr -cd 'a-zA-Z0-9_-' < "$TENANT_FILE")
echo "[efs-monitor] tenant '$TENANT' assigned; mount target ${EFS_HOST}; starting mount attempts"

while true; do
  if [ ! -f "$MARKER" ]; then
    if timeout 15 mount -t nfs4 -o nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=150,retrans=2 \
         "${EFS_HOST}:/" "$EFS_DIR" 2>/tmp/efs-mount.err; then
      echo "[efs-monitor] EFS mounted from ${EFS_HOST} at $(date -u +%FT%TZ)"
      TDIR="$EFS_DIR/tenants/$TENANT"
      mkdir -p "$TDIR"
      if [ ! -f "$TDIR/openclaw.json" ]; then
        echo "[efs-monitor] tenant $TENANT first generation: seeding from local state"
        cp -a "$STATE_DIR/." "$TDIR/"
      else
        echo "[efs-monitor] tenant $TENANT has prior state - adopting it"
      fi
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
      # Plugins must be root-owned or the gateway blocks them (see Dockerfile note);
      # fix up trees seeded by older generations. Recursing over NFS costs one round
      # trip PER FILE — measured at 17.5s for this tree, over half of a cold start —
      # and it is pure waste once the tree is root-owned, which is the steady state.
      # A `find ! -uid 0` probe is NOT cheap enough: -quit short-circuits only when it
      # FINDS an offender, so the all-correct case (the common one) still walks every
      # file. Use a sentinel instead: written after a successful fixup, so the probe
      # is a single stat. Bump the suffix if the ownership rule ever changes.
      OWNED_MARK="$TDIR/.npm-root-owned"
      T0=$(date +%s%3N)
      if [ -d "$TDIR/npm" ] && [ ! -f "$OWNED_MARK" ]; then
        chown -R root:root "$TDIR/npm" 2>/dev/null
        : > "$OWNED_MARK"
        echo "[efs-monitor] npm tree re-owned to root in $(( $(date +%s%3N) - T0 ))ms"
      fi
      echo "[efs-monitor] timing: ownership step $(( $(date +%s%3N) - T0 ))ms"
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
      T0=$(date +%s%3N)
      mount --bind "$TDIR" "$STATE_DIR"
      echo "[efs-monitor] timing: bind mount $(( $(date +%s%3N) - T0 ))ms"
      touch "$MARKER"
      echo "[efs-monitor] state dir now EFS-backed for tenant $TENANT; bouncing gateway"
      pkill -f "openclaw.mjs gateway" || true
    fi
  fi
  sleep 5
done
