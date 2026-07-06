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
      mount --bind "$TDIR" "$STATE_DIR"
      touch "$MARKER"
      echo "[efs-monitor] state dir now EFS-backed for tenant $TENANT; bouncing gateway"
      pkill -f "openclaw.mjs gateway" || true
    fi
  fi
  sleep 5
done
