// Persistent gateway bridge: holds ONE WebSocket to the warm OpenClaw gateway and
// runs agent turns over it — eliminating the ~20s per-message CLI spawn cost.
// Exposes a tiny local HTTP API on 127.0.0.1:8090 that the sidecar calls.
//
//   GET /agent?m=<msg>&s=<sessionKey>  -> {"reply": "..."}
//   GET /ready                         -> {"connected": true}
//
// Frame protocol (reverse-engineered + verified against gateway 2026.6.11):
//   server sends event connect.challenge{nonce}
//   client sends req connect{minProtocol,maxProtocol,role,scopes,caps,client{mode:backend},auth{token}}
//   server res ok -> hello-ok
//   client sends req agent{message,sessionKey,idempotencyKey}
//   server res ok -> result.payloads[].text   (async; deltas stream as events meanwhile)
const http = require("http");
const crypto = require("node:crypto");
const WebSocket = require("/app/node_modules/ws");

const TOKEN = process.env.OPENCLAW_GATEWAY_TOKEN || "poc-microvm-token-42";
const GW = "ws://127.0.0.1:18789";
const PORT = 8090;

let ws = null;
let connected = false;
const pending = new Map(); // requestId -> {resolve, reject, timer}

function connect() {
  connected = false;
  ws = new WebSocket(GW);
  ws.on("open", () => {});
  ws.on("message", (buf) => {
    let m;
    try { m = JSON.parse(buf.toString()); } catch { return; }
    if (m.event === "connect.challenge") {
      ws.send(JSON.stringify({
        type: "req", id: "connect", method: "connect",
        params: {
          minProtocol: 4, maxProtocol: 4, role: "operator",
          scopes: ["operator.admin"], caps: [],
          client: { id: "gateway-client", displayName: "gw-bridge",
                    version: "2026.6.11", platform: "linux", mode: "backend" },
          auth: { token: TOKEN },
        },
      }));
      return;
    }
    if (m.type === "res" && m.id === "connect") {
      connected = !!m.ok;
      if (!m.ok) { console.error("[bridge] connect failed", JSON.stringify(m.error)); ws.close(); }
      else console.error("[bridge] connected to gateway");
      return;
    }
    if (m.type === "res" && pending.has(m.id)) {
      // The agent method emits TWO res frames with the same id, BOTH ok:true:
      //   1) ack:   payload.status === "accepted"      (no result yet)
      //   2) final: payload.status === "ok" + payload.result.payloads[]
      // Everything of interest is nested under m.payload (not m.result).
      const pl = m.payload || {};
      const result = pl.result;
      const isError = m.ok === false || m.error || pl.status === "error";
      const isFinal = (result && Array.isArray(result.payloads)) || isError;
      if (!isFinal) return; // ack — keep waiting for the final frame
      const p = pending.get(m.id);
      pending.delete(m.id);
      clearTimeout(p.timer);
      if (isError) {
        p.reject(new Error(JSON.stringify(m.error || pl.error || m)));
      } else {
        p.resolve(result.payloads.map((x) => x.text).filter(Boolean).join(" ") || "(no reply)");
      }
    }
  });
  ws.on("close", () => { connected = false; setTimeout(connect, 1000); });
  ws.on("error", (e) => { console.error("[bridge] ws error", e.message); });
}

function runAgent(message, sessionKey) {
  return new Promise((resolve, reject) => {
    if (!connected) return reject(new Error("gateway not connected"));
    const id = crypto.randomUUID();
    const timer = setTimeout(() => {
      pending.delete(id); reject(new Error("agent turn timeout"));
    }, 280000);
    pending.set(id, { resolve, reject, timer });
    ws.send(JSON.stringify({
      type: "req", id, method: "agent",
      params: { message, sessionKey, idempotencyKey: id },
    }));
  });
}

http.createServer(async (req, res) => {
  const u = new URL(req.url, "http://x");
  if (u.pathname === "/ready") {
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ connected }));
  }
  if (u.pathname === "/agent") {
    const msg = u.searchParams.get("m") || "";
    const sess = u.searchParams.get("s") || "default";
    try {
      const reply = await runAgent(msg, sess);
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ reply }));
    } catch (e) {
      res.writeHead(500, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: String(e.message || e) }));
    }
    return;
  }
  res.writeHead(404); res.end();
}).listen(PORT, "127.0.0.1", () => console.error(`[bridge] http on :${PORT}`));

connect();
