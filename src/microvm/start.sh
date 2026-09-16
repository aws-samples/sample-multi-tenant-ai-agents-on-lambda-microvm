#!/usr/bin/env bash
# MicroVM entrypoint (EFS variant): hook sidecar (8080) + EFS mount daemon +
# supervised OpenClaw gateway (18789).
# This script is root (PID 1 is tini): mounting NFS needs it. The gateway and its bridge
# — and so every tool the agent can call — are dropped to the image's stock uid 1000
# with no capabilities and no_new_privs. The sidecar stays root (platform hooks, tenant
# file, mount diagnostics).
set -u
export OPENCLAW_GATEWAY_TOKEN="${OPENCLAW_GATEWAY_TOKEN:-poc-microvm-token-42}"
export HOME=/home/node

# Everything the agent can reach is launched through this.
#   --reuid/--regid 1000 + --clear-groups : the stock `node` user
#   --bounding-set=-all + --inh-caps=-all : no capability can ever be regained
#   --no-new-privs                        : setuid/file-cap binaries (mount, su, …) run
#                                           as if they had no such bits
AS_NODE=(setpriv --reuid=1000 --regid=1000 --clear-groups
         --bounding-set=-all --inh-caps=-all --no-new-privs)

# Plugin tree lives in the image (see Dockerfile); bind it into the state dir read-only.
# efs-monitor.sh re-applies this after its own bind mount covers the state dir.
mount --bind -o ro /opt/poc/npm /home/node/.openclaw/npm \
  && echo "[start] plugin tree bound read-only from image"

python3 /opt/poc/hooks.py &
echo "[start] hook sidecar on :8080 (pid $!)"

/opt/poc/efs-monitor.sh &
echo "[start] efs mount daemon (pid $!)"

# Persistent gateway bridge (warm WS to :18789) — kills per-message CLI spawn cost.
# Supervise it so it comes back if the gateway bounces during EFS adoption.
( while true; do "${AS_NODE[@]}" node /opt/poc/gw-bridge.cjs; echo "[start] gw-bridge exited; restart in 2s"; sleep 2; done ) &
echo "[start] gw-bridge supervisor on :8090 (pid $!)"

# Supervise the gateway: efs-monitor kills it once EFS is bound so it
# restarts against the EFS-backed state dir.
while true; do
  # efs-monitor holds the gateway down while it swaps the state dir: the gateway must not
  # be running while its state directory is replaced underneath it, and its lifecycle lock
  # has to be cleared before the replacement starts. Without this gate the supervisor
  # relaunches into the dying gateway's stale lock ("gateway already running (pid …)").
  while [ -f /var/run/openclaw-maintenance ]; do sleep 1; done
  "${AS_NODE[@]}" node /app/openclaw.mjs gateway \
    --allow-unconfigured \
    --port 18789 \
    --auth token \
    --token "$OPENCLAW_GATEWAY_TOKEN"
  echo "[start] gateway exited (rc=$?); restarting in 2s"
  sleep 2
done
