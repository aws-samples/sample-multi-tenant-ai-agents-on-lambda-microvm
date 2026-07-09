"""Multi-tenant OpenClaw orchestrator on Lambda MicroVMs — Router + Worker + Sweeper.

One Lambda, three roles selected by event shape:
  - Function URL HTTP event      -> ROUTER (fast: ACK Telegram in <1s, hand off to worker)
  - {"_worker": {...}}           -> WORKER (async self-invoke: ensure VM, run turn, reply)
  - {"_sweeper": true} (EventBridge) -> SWEEPER (reap idle, reconcile)

Registry (DynamoDB): tenantId -> microvmId, endpoint, generation, state, launchedAt,
lastActiveAt, botToken(optional), webhookSecret(optional).

State lifecycle: a tenant's VM is disposable; state lives on per-tenant EFS subdir.
Router NEVER proactively renews (see design-orchestrator.md). Two branches only:
alive -> forward; dead -> cold-start.
"""
import json
import os
import time
import urllib.parse
import urllib.request

import boto3
from boto3.dynamodb.conditions import Attr

REGION = os.environ["AWS_REGION"]
TABLE = os.environ["TENANTS_TABLE"]
FN_NAME = os.environ["AWS_LAMBDA_FUNCTION_NAME"]
IMAGE_ARN = os.environ["IMAGE_ARN"]
IMAGE_VERSION = os.environ["IMAGE_VERSION"]
EXEC_ROLE_ARN = os.environ["EXEC_ROLE_ARN"]
INGRESS = os.environ["INGRESS_CONNECTOR"]
EGRESS = os.environ["EGRESS_CONNECTOR"]
IDLE_REAP_SECONDS = int(os.environ.get("IDLE_REAP_SECONDS", "3600"))

mv = boto3.client("lambda-microvms", region_name=REGION)
lam = boto3.client("lambda", region_name=REGION)
ddb = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


# ---------- helpers ----------
def now() -> int:
    return int(time.time())


def get_tenant(tid):
    return ddb.get_item(Key={"tenantId": tid}).get("Item")


def mv_state(microvm_id):
    try:
        return mv.get_microvm(microvmIdentifier=microvm_id).get("state")
    except Exception:
        return None


def call_vm(endpoint, path, token, method="GET", body=None, port=8080, timeout=280):
    url = f"https://{endpoint}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"X-aws-proxy-auth": token,
                                          "X-aws-proxy-port": str(port),
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def mint_token(microvm_id):
    # Only the sidecar (8080) is reachable through the proxy; the OpenClaw gateway
    # (18789) stays loopback-only inside the VM.
    r = mv.create_microvm_auth_token(
        microvmIdentifier=microvm_id, expirationInMinutes=55,
        allowedPorts=[{"port": 8080}])
    return r["authToken"]["X-aws-proxy-auth"]


def tg_send(bot_token, chat_id, text):
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text[:4000]}).encode()
    with urllib.request.urlopen(
            f"https://api.telegram.org/bot{bot_token}/sendMessage", data, timeout=20) as r:
        body = json.loads(r.read())
        print(f"[worker] tg_send ok={body.get('ok')} "
              f"message_id={body.get('result', {}).get('message_id')} chat={chat_id}", flush=True)
        return body


def tg_typing(bot_token, chat_id):
    try:
        data = urllib.parse.urlencode({"chat_id": chat_id, "action": "typing"}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{bot_token}/sendChatAction", data, timeout=10)
    except Exception:
        pass


# ---------- cold start (single-writer lock via conditional update) ----------
def cold_start(tid, item):
    gen = int(item.get("generation", 0)) + 1
    # Lock: only one launcher may flip state PENDING for this generation.
    try:
        ddb.update_item(
            Key={"tenantId": tid},
            UpdateExpression="SET #s=:p, generation=:g, launchedAt=:t",
            ConditionExpression=Attr("state").ne("LAUNCHING"),
            ExpressionAttributeNames={"#s": "state"},
            ExpressionAttributeValues={":p": "LAUNCHING", ":g": gen, ":t": now()})
    except Exception:
        # Another invocation is already launching; wait for it to publish endpoint.
        for _ in range(40):
            time.sleep(3)
            it = get_tenant(tid)
            if it and it.get("state") == "RUNNING" and it.get("endpoint"):
                return it
        raise RuntimeError("concurrent launch did not converge")

    r = mv.run_microvm(
        imageIdentifier=IMAGE_ARN, imageVersion=IMAGE_VERSION,
        executionRoleArn=EXEC_ROLE_ARN,
        ingressNetworkConnectors=[INGRESS],
        egressNetworkConnectors=[EGRESS],
        idlePolicy={"autoResumeEnabled": True, "maxIdleDurationSeconds": 900,
                    "suspendedDurationSeconds": 3600},
        maximumDurationInSeconds=28800)
    microvm_id, endpoint = r["microvmId"], r["endpoint"]

    # wait RUNNING
    for _ in range(40):
        if mv_state(microvm_id) == "RUNNING":
            break
        time.sleep(3)
    token = mint_token(microvm_id)

    # assign tenant -> unblocks efs-monitor -> mounts per-tenant subdir -> bounces gateway
    for _ in range(20):
        try:
            call_vm(endpoint, "/tenant", token, "POST", {"tenantId": tid}, timeout=15)
            break
        except Exception:
            time.sleep(3)

    # gate on EFS adoption + gateway healthy
    ready = False
    for _ in range(40):
        try:
            st, body = call_vm(endpoint, "/health", token, timeout=15)
            h = json.loads(body)
            if h.get("efsReady") and h.get("healthz") == 200:
                ready = True
                break
        except Exception:
            pass
        time.sleep(3)

    item = {**item, "tenantId": tid, "microvmId": microvm_id, "endpoint": endpoint,
            "generation": gen, "state": "RUNNING", "launchedAt": now(),
            "lastActiveAt": now(), "authToken": token}
    ddb.put_item(Item=item)
    if not ready:
        raise RuntimeError("cold start: VM did not become EFS-ready")
    return item


def ensure_vm(tid, item):
    """Two-branch decision: alive -> reuse; dead -> cold start."""
    mid = item.get("microvmId")
    state = mv_state(mid) if mid else None
    if state in ("RUNNING", "SUSPENDED"):
        # alive (SUSPENDED auto-resumes on the forwarded request)
        return item, False
    return cold_start(tid, item), True


# ---------- WORKER (async) ----------
def worker(payload):
    tid = payload["tenantId"]
    update = payload["update"]
    item = get_tenant(tid)
    bot = item.get("botToken")
    chat_id = str(((update.get("message") or {}).get("chat") or {}).get("id", ""))
    text = (update.get("message") or {}).get("text", "")
    if not text or not chat_id:
        return {"skipped": "no text/chat"}

    if bot and chat_id:
        tg_typing(bot, chat_id)

    item, cold = ensure_vm(tid, item)
    endpoint = item["endpoint"]
    # Always mint a fresh token per turn: token TTL (<=60min) < VM lifetime (8h),
    # so a cached token from cold-start time is often already expired -> 403.
    token = mint_token(item["microvmId"])

    # run the turn inside the tenant's VM via the sidecar /chat
    qs = urllib.parse.urlencode({"m": text, "s": f"tg-{chat_id}"})
    st, body = call_vm(endpoint, f"/chat?{qs}", token, timeout=280)
    d = json.loads(body)
    reply = " ".join(p.get("text", "") for p in
                     d.get("result", {}).get("payloads", [])) or "(no reply)"
    if bot and chat_id:
        tg_send(bot, chat_id, reply)
    ddb.update_item(Key={"tenantId": tid},
                    UpdateExpression="SET lastActiveAt=:t",
                    ExpressionAttributeValues={":t": now()})
    return {"tenant": tid, "cold": cold, "reply": reply[:80]}


# ---------- ROUTER (Function URL) ----------
def router(event):
    raw = event.get("rawPath", "/")
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64
        body = base64.b64decode(body).decode()
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    # /tg/<tenantId>
    parts = [p for p in raw.split("/") if p]
    if len(parts) == 2 and parts[0] == "tg":
        tid = parts[1]
        item = get_tenant(tid)
        if not item:
            return {"statusCode": 404, "body": "unknown tenant"}
        # verify Telegram secret token
        want = item.get("webhookSecret")
        got = headers.get("x-telegram-bot-api-secret-token")
        if want and got != want:
            return {"statusCode": 403, "body": "bad secret"}
        update = json.loads(body or "{}")
        # hand off to worker asynchronously; ACK Telegram immediately
        lam.invoke(FunctionName=FN_NAME, InvocationType="Event",
                   Payload=json.dumps({"_worker": {"tenantId": tid, "update": update}}).encode())
        return {"statusCode": 200, "body": "ok"}

    # /chat/<tenantId>?m=... — synchronous test entry (no Telegram); ensures VM + runs a turn inline.
    if len(parts) == 2 and parts[0] == "chat":
        tid = parts[1]
        item = get_tenant(tid)
        if not item:
            return {"statusCode": 404, "body": "unknown tenant"}
        qs = urllib.parse.parse_qs(event.get("rawQueryString", ""))
        msg = (qs.get("m") or ["Say pong"])[0]
        sess = (qs.get("s") or ["http-demo"])[0]
        try:
            item, cold = ensure_vm(tid, item)
            token = mint_token(item["microvmId"])  # fresh per call (see worker note)
            q2 = urllib.parse.urlencode({"m": msg, "s": sess})
            st, body = call_vm(item["endpoint"], f"/chat?{q2}", token, timeout=280)
            ddb.update_item(Key={"tenantId": tid},
                            UpdateExpression="SET lastActiveAt=:t",
                            ExpressionAttributeValues={":t": now()})
            d = json.loads(body)
            reply = " ".join(p.get("text", "") for p in
                             d.get("result", {}).get("payloads", [])) or body.decode()[:200]
            return {"statusCode": 200, "body": json.dumps({"tenant": tid, "cold": cold, "reply": reply})}
        except Exception as e:
            return {"statusCode": 500, "body": json.dumps({"error": str(e)})}

    if raw == "/health":
        return {"statusCode": 200, "body": json.dumps({"ok": True, "role": "router"})}
    return {"statusCode": 404, "body": "not found"}


# ---------- SWEEPER (EventBridge) ----------
def sweeper():
    reaped, reconciled = [], []
    for item in ddb.scan().get("Items", []):
        tid, mid = item["tenantId"], item.get("microvmId")
        if not mid:
            continue
        state = mv_state(mid)
        if state is None or state == "TERMINATED":
            if item.get("state") != "COLD":
                ddb.update_item(Key={"tenantId": tid},
                                UpdateExpression="SET #s=:c",
                                ExpressionAttributeNames={"#s": "state"},
                                ExpressionAttributeValues={":c": "COLD"})
                reconciled.append(tid)
            continue
        idle = now() - int(item.get("lastActiveAt", 0))
        if state == "SUSPENDED" and idle > IDLE_REAP_SECONDS:
            try:
                mv.terminate_microvm(microvmIdentifier=mid)
                ddb.update_item(Key={"tenantId": tid},
                                UpdateExpression="SET #s=:c",
                                ExpressionAttributeNames={"#s": "state"},
                                ExpressionAttributeValues={":c": "COLD"})
                reaped.append(tid)
            except Exception:
                pass
    return {"reaped": reaped, "reconciled": reconciled}


def handler(event, context):
    if isinstance(event, dict) and "_worker" in event:
        return worker(event["_worker"])
    if isinstance(event, dict) and event.get("_sweeper"):
        return sweeper()
    if isinstance(event, dict) and ("rawPath" in event or "requestContext" in event):
        return router(event)
    return {"statusCode": 400, "body": "unrecognized event"}
