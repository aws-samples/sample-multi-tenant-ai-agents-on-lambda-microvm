"""Sidecar (multi-tenant EFS variant): lifecycle hooks + health + /tenant + /chat + /tg + /files."""
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOOK = "/aws/lambda-microvms/runtime/v1/"
OPENCLAW = "http://127.0.0.1:18789"
BRIDGE = "http://127.0.0.1:8090"
GATEWAY_TOKEN = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "poc-microvm-token-42")
TENANT_FILE = "/var/run/tenant-id"
MARKER = "/var/run/efs-mounted"
BOOT = time.monotonic()
# Ready gate: never let a slow/broken readiness signal fail the whole image build —
# past this point we let the platform snapshot anyway (logged), which is exactly the
# hookless behaviour this image had before.
READY_GRACE_S = 240


def agent_turn(message: str, session: str, attachments=None) -> bytes:
    """Run one agent turn via the PERSISTENT gateway bridge (no per-message CLI spawn).

    The bridge holds a warm WebSocket to the gateway; a turn is ~2s instead of ~22s.
    Image attachments (base64) POST to the bridge; text-only turns keep the GET path.
    Returns the same JSON shape callers expect: {"result":{"payloads":[{"text":...}]}}.
    """
    if attachments:
        req = urllib.request.Request(
            "http://127.0.0.1:8090/agent",
            data=json.dumps({"m": message, "s": session,
                             "attachments": attachments}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=290) as r:
            d = json.loads(r.read())
    else:
        url = ("http://127.0.0.1:8090/agent?m=" + urllib.parse.quote(message)
               + "&s=" + urllib.parse.quote(session))
        with urllib.request.urlopen(url, timeout=290) as r:
            d = json.loads(r.read())
    if "error" in d:
        return json.dumps({"error": d["error"]}).encode()
    payload = {"text": d.get("reply", "")}
    if d.get("media"):
        payload["media"] = d["media"]
    return json.dumps({"result": {"payloads": [payload]}}).encode()


def check(path: str):
    try:
        with urllib.request.urlopen(OPENCLAW + path, timeout=3) as r:
            return r.status, None
    except Exception as e:
        return getattr(e, "code", None), f"{type(e).__name__}: {e}"


def bridge_connected() -> bool:
    try:
        with urllib.request.urlopen(BRIDGE + "/ready", timeout=3) as r:
            return bool(json.loads(r.read()).get("connected"))
    except Exception:
        return False


def write_tenant(raw: str) -> str:
    tid = "".join(c for c in (raw or "") if c.isalnum() or c in "-_")[:64]
    if tid:
        with open(TENANT_FILE, "w") as f:
            f.write(tid)
    return tid


# ---------- image-build hooks ----------
# /ready gates the snapshot: the platform captures disk+memory only once we answer
# 200, so answering late (gateway up, bridge's WS established) bakes a warm gateway
# and a warm page cache into every future launch instead of re-paying boot per VM.
# /validate then runs on a throwaway VM restored FROM that snapshot, and the
# platform samples which snapshot pages get touched to prefetch them at run time.
# So validate must walk the real cold-start path, not sweep files at random.
PREWARM_DEADLINE_S = 420
_prewarm = {"thread": None, "done": False}
# ThreadingHTTPServer serves overlapping hook polls, so guard the spawn: two
# concurrent /validate calls must not each start a prewarm (double gateway kill).
_prewarm_lock = threading.Lock()


def sh(cmd: str, timeout: int):
    """Run a shell probe; failure is fine — faulting the pages in is the point."""
    try:
        r = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True,
                           timeout=timeout)
        print(f"[prewarm] rc={r.returncode} {cmd[:70]}", flush=True)
    except Exception as e:
        print(f"[prewarm] {type(e).__name__} {cmd[:70]}", flush=True)


def prewarm():
    """Exercise the cold-start path so the platform prefetches exactly those pages.

    Mirrors what efs-monitor.sh + start.sh + the first turn actually do, in order.
    Every step is expected to FAIL here (no tenant EFS route, build-role creds have
    no Bedrock access) — the page faults happen regardless, which is all we need.
    """
    deadline = time.monotonic() + PREWARM_DEADLINE_S
    efs_host = (f"{os.environ['EFS_ID']}.efs.{os.environ.get('AWS_REGION', 'us-east-1')}"
                ".amazonaws.com" if os.environ.get("EFS_ID") else "127.0.0.1")
    try:
        # 1. NFS client: mount.nfs4 + rpc libs are a rounding error in image size and
        #    everything in latency — they only ever execute during tenant adoption.
        #    SOFT mount + outer `timeout`: a hard mount that hangs is unkillable and
        #    would stall validation until the platform ends the build.
        sh("timeout 20 mount -t nfs4 -o nfsvers=4.1,soft,timeo=30,retrans=1 "
           f"'{efs_host}:/' /mnt/efs", timeout=30)
        # 2. Tenant-adoption toolchain: coreutils/python bits efs-monitor.sh shells to.
        sh("mkdir -p /tmp/prewarm-state && cp -a /home/node/.openclaw/. /tmp/prewarm-state/; "
           "chown -R root:root /tmp/prewarm-state; tr -cd 'a-z' </etc/hostname >/dev/null; "
           "python3 -c 'import json; json.dumps({})'; pkill --version", timeout=60)
        # 3. Live model discovery (node + the Bedrock plugin's dist tree + AWS SDK).
        sh("cp /opt/poc/openclaw.json /tmp/prewarm.json && "
           "node /opt/poc/materialize-models.mjs /tmp/prewarm.json", timeout=90)
        # 4. The gateway RESTART path — efs-monitor bounces the gateway after binding
        #    EFS, so every cold start pays a second gateway boot. Sample it by using
        #    the exact pattern efs-monitor.sh uses (pkill never matches itself).
        sh("pkill -f 'openclaw.mjs gateway'", timeout=10)
        # Wait for the supervisor's 2s backoff + a full re-boot, then confirm the
        # restarted gateway is actually serving before probing a turn through it.
        time.sleep(3)
        while time.monotonic() < deadline:
            if check("/healthz")[0] == 200 and bridge_connected():
                break
            time.sleep(1)
        print(f"[prewarm] gateway back after restart: healthz={check('/healthz')[0]} "
              f"bridge={bridge_connected()}", flush=True)
        # 5. Agent turns: fault in the whole turn pipeline (bridge WS frames, agent
        #    runtime, session store). The reply is worthless here — Bedrock is denied
        #    under the build role — but the resident pages are the payload. Each probe
        #    uses a FRESH session: a retried turn on a spent session trips the
        #    gateway's ordering/conflict guards and returns before doing real work.
        for i in range(3):
            if time.monotonic() > deadline:
                break
            try:
                with urllib.request.urlopen(
                        BRIDGE + "/agent?m=" + urllib.parse.quote(f"ping {i}")
                        + f"&s=prewarm-{i}",
                        timeout=max(10, int(deadline - time.monotonic()))) as r:
                    r.read()
                print(f"[prewarm] turn probe {i}: ok", flush=True)
            except Exception as e:
                print(f"[prewarm] turn probe {i}: {type(e).__name__}", flush=True)
        sh("umount -f /mnt/efs 2>/dev/null; rm -rf /tmp/prewarm-state /tmp/prewarm.json",
           timeout=20)
        print("[prewarm] complete", flush=True)
    except Exception as e:
        print(f"[prewarm] aborted: {type(e).__name__}: {e}", flush=True)
    finally:
        # Always release /validate — a stuck prewarm must not fail the image build.
        _prewarm["done"] = True


def hook(name: str, body: bytes):
    """Answer one platform lifecycle hook. Returns (code, payload)."""
    if name == "ready":
        # 503 must return IMMEDIATELY (a held-open request that outlives the hook
        # timeout ends the build); the platform re-polls.
        if check("/healthz")[0] == 200 and bridge_connected():
            return 200, b'{"ready":true}'
        if time.monotonic() - BOOT > READY_GRACE_S:
            print("[hooks] ready grace expired; snapshotting unready", flush=True)
            return 200, b'{"ready":false,"grace":"expired"}'
        return 503, b'{"ready":false}'
    if name == "validate":
        if _prewarm["done"]:
            return 200, b'{"prewarmed":true}'
        with _prewarm_lock:
            if _prewarm["thread"] is None:
                _prewarm["thread"] = threading.Thread(target=prewarm, daemon=True)
                _prewarm["thread"].start()
        return 503, b'{"prewarming":true}'
    if name == "run":
        # Per-VM init: the orchestrator's tenantId arrives here as runHookPayload,
        # so the tenant is assigned before the endpoint accepts any traffic — no
        # post-launch round trip, and efs-monitor starts mounting immediately.
        try:
            payload = json.loads(json.loads(body or b"{}").get("runHookPayload") or "{}")
        except Exception:
            payload = {}
        tid = write_tenant(payload.get("tenantId", ""))
        print(f"[hooks] run hook: tenant={tid or 'none'}", flush=True)
        return 200, json.dumps({"tenant": tid}).encode()
    return 200, b"{}"


def state_report() -> bytes:
    r = subprocess.run(
        ["sh", "-c",
         "echo '--- tenant ---'; cat /var/run/tenant-id 2>&1; "
         "echo; echo '--- mounts ---'; grep -E 'efs|openclaw|nfs' /proc/mounts; "
         "echo '--- marker ---'; ls -la /var/run/efs-mounted 2>&1; "
         "echo '--- state dir ---'; ls -la /home/node/.openclaw/ 2>&1 | head -14; "
         "echo '--- sessions ---'; ls -la /home/node/.openclaw/agents/main/sessions/ 2>&1 | head -8; "
         "echo '--- efs err ---'; tail -3 /tmp/efs-mount.err 2>&1"],
        capture_output=True, text=True, timeout=10,
    )
    return (r.stdout + r.stderr).encode()


class H(BaseHTTPRequestHandler):
    def _send(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            hz, herr = check("/healthz")
            rz, rerr = check("/readyz")
            self._send(200 if hz == 200 else 503, json.dumps(
                {"healthz": hz, "readyz": rz, "efsReady": os.path.exists(MARKER),
                 "tenant": open(TENANT_FILE).read().strip() if os.path.exists(TENANT_FILE) else None,
                 "healthzErr": herr, "readyzErr": rerr}).encode())
        elif self.path == "/files":
            self._send(200, state_report())
        elif self.path.startswith("/progress"):
            # Proxy to the bridge: poll accumulated stream text for an async turn.
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:8090" + self.path, timeout=10) as r:
                    self._send(200, r.read())
            except urllib.error.HTTPError as e:
                self._send(e.code, e.read())
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode())
        elif self.path.startswith("/media"):
            # Fetch a gateway-produced outbound media file (e.g. TTS audio) so the
            # orchestrator can forward it to the channel. Restricted to the
            # gateway's own outbound-media dir — no general file read.
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            path = os.path.realpath((q.get("path") or [""])[0])
            allowed = "/home/node/.openclaw/media/outbound/"
            if not path.startswith(allowed) or not os.path.isfile(path):
                self._send(404, b'{"error":"no such media"}')
                return
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path.startswith("/chat"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            msg = (q.get("m") or ["Say pong"])[0]
            sess = (q.get("s") or ["poc-demo"])[0]
            try:
                self._send(200, agent_turn(msg, sess, None))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode())
        else:
            self._send(404)

    def do_POST(self):
        if self.path == "/chat":
            # JSON body variant of GET /chat — used for turns with image attachments
            # (base64 payloads don't fit in a query string).
            n = int(self.headers.get("Content-Length") or 0)
            try:
                d = json.loads(self.rfile.read(n) or b"{}")
                msg = d.get("m") or "Say pong"
                sess = d.get("s") or "poc-demo"
                atts = d.get("attachments") or None
                self._send(200, agent_turn(msg, sess, atts))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode())
        elif self.path == "/chat-async":
            # Start a turn without blocking; returns {"turnId"} for /progress polling.
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = self.rfile.read(n) or b"{}"
                json.loads(body)  # validate before proxying
                req = urllib.request.Request(
                    "http://127.0.0.1:8090/agent-async", data=body,
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=15) as r:
                    self._send(200, r.read())
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode())
        elif self.path == "/tenant":
            # Orchestrator assigns the tenant; unblocks efs-monitor. Kept as a
            # fallback for the /run hook (and for chat.sh against an older image).
            n = int(self.headers.get("Content-Length") or 0)
            tid = write_tenant(json.loads(self.rfile.read(n) or b"{}").get("tenantId", ""))
            if not tid:
                self._send(400, b'{"error":"tenantId required"}')
                return
            print(f"[hooks] tenant assigned: {tid}", flush=True)
            self._send(200, json.dumps({"tenant": tid}).encode())
        elif self.path.startswith(HOOK):
            name = self.path[len(HOOK):].strip("/")
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            try:
                code, payload = hook(name, body)
            except Exception as e:
                print(f"[hooks] {name} raised {e}", flush=True)
                code, payload = 200, b"{}"
            print(f"[hooks] POST {self.path} -> {code}", flush=True)
            self._send(code, payload)
        else:
            self._send(200)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print("[hooks] sidecar listening on :8080", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
