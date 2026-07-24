// Persistent gateway bridge: holds ONE WebSocket to the warm OpenClaw gateway and
// runs agent turns over it — eliminating the ~20s per-message CLI spawn cost.
// Exposes a tiny local HTTP API on 127.0.0.1:8090 that the sidecar calls.
//
//   GET /agent?m=<msg>&s=<sessionKey>  -> {"reply","media"}         (sync)
//   POST /agent {m,s,attachments}      -> {"reply","media"}         (sync, images)
//   POST /agent-async {m,s,attachments}-> {"turnId": "..."}         (returns at once)
//   GET /progress?id=<turnId>          -> {"text","done","reply"?,"media"?}
//   GET /ready                         -> {"connected": true}
//
// Frame protocol (reverse-engineered + verified against gateway 2026.6.11):
//   server sends event connect.challenge{nonce}
//   client sends req connect{minProtocol,maxProtocol,role,scopes,caps,client{mode:backend},auth{token}}
//   server res ok -> hello-ok
//   client sends req chat.send{message,sessionKey,idempotencyKey[,attachments]}
//   server res ok -> {runId,status:"started"} (ack only), then "chat" events keyed
//   by runId: state:"delta" carries accumulated message.content text, state:"final"
//   carries the full content[] incl. {type:"attachment"} entries (e.g. TTS audio).
//
// chat.send (vs the lower-level `agent` method) runs the gateway's full channel
// pipeline: native slash commands (/tts, ...) execute, and auto-TTS attaches
// voice audio to replies — none of which the bare `agent` RPC ever does.
const http = require("http");
const crypto = require("node:crypto");
const fs = require("node:fs");
const WebSocket = require("/app/node_modules/ws");

// TTS audio may reach us two ways: (1) as an attachment in the final chat
// event's content[] (auto-TTS via /tts on), or (2) NOT on the wire at all when
// the agent invokes the `tts` tool itself — the tool writes an mp3 to this dir
// and no frame references it. The bridge runs INSIDE the VM, so we reconcile
// by scanning this dir for voice files created during the run.
const OUTBOUND_DIR = "/home/node/.openclaw/media/outbound";

function scanOutboundVoice(sinceMs) {
  const out = [];
  let names;
  try { names = fs.readdirSync(OUTBOUND_DIR); } catch { return out; }
  for (const name of names) {
    if (!/^voice-.*\.(mp3|ogg|opus|m4a)$/i.test(name)) continue;
    const full = `${OUTBOUND_DIR}/${name}`;
    let st;
    try { st = fs.statSync(full); } catch { continue; }
    if (st.mtimeMs < sinceMs) continue;
    if (st.size === 0) continue; // failed synthesis leaves a 0-byte file
    const ext = name.split(".").pop().toLowerCase();
    out.push({ url: full, kind: "audio",
      mimeType: ext === "mp3" ? "audio/mpeg" : `audio/${ext}` });
  }
  return out;
}

// Merge wire-declared media with filesystem-discovered voice files, de-duped
// by url so an attachment already on the wire isn't sent twice. When the run
// is known to have invoked the tts tool, the mp3 may still be mid-write when
// the gateway fires `final` (synthesis runs up to ~5s), so poll briefly for it.
async function reconcileMedia(wireMedia, sinceMs, expectVoice) {
  const seen = new Set((wireMedia || []).map((m) => m.url));
  const pick = () => scanOutboundVoice(sinceMs).filter((m) => !seen.has(m.url));
  let extra = pick();
  if (!extra.length && expectVoice) {
    for (let i = 0; i < 16 && !extra.length; i++) { // ~8s @ 500ms
      await new Promise((r) => setTimeout(r, 500));
      extra = pick();
    }
  }
  return (wireMedia || []).concat(extra);
}

const TOKEN = process.env.OPENCLAW_GATEWAY_TOKEN || "poc-microvm-token-42";
const GW = "ws://127.0.0.1:18789";
const PORT = 8090;

let ws = null;
let connected = false;
const pending = new Map(); // requestId -> {resolve, reject, timer}
// Async turns for the poll-based streaming path:
// turnId -> {text, done, reply, media, error, at}
const turns = new Map();
// Gateway assigns its own runId per run (returned on the ack frame); delta and
// final events are keyed by that runId, so map it back to our turnId.
const runToTurn = new Map();
// runIds observed invoking the `tts` tool — their reply is voice, so if the
// mp3 isn't on disk yet at `final` we poll for it instead of giving up.
const ttsRuns = new Set();
const TURN_TTL_MS = 10 * 60 * 1000;
// Async turns may legitimately run for hours (the Lambda pollers chain across
// invocations); the VM's 8h max lifetime is the real bound, not a poll timeout.
const ASYNC_TURN_TIMEOUT_MS = 8 * 60 * 60 * 1000;

// GC: drop turns 10 min after their last activity (progress poll or completion).
// An in-flight turn being polled keeps refreshing its timestamp, so only turns
// abandoned by every poller age out.
function gcTurns() {
  const cutoff = Date.now() - TURN_TTL_MS;
  for (const [id, t] of turns) if (t.at < cutoff) turns.delete(id);
  for (const [rid, tid] of runToTurn) if (!turns.has(tid)) runToTurn.delete(rid);
  // ttsRuns entries are normally consumed at `final`; cap growth if a run never
  // finalized (aborted mid-flight) by clearing the set when it gets large.
  if (ttsRuns.size > 256) ttsRuns.clear();
}

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
    // agent "item" events announce tool calls. A `tts` tool call means this
    // run's reply is voice — flag the runId so `final` waits for the mp3.
    if (m.type === "event" && m.event === "agent") {
      const pl = m.payload || {};
      if (pl.runId && (pl.data || {}).kind === "tool" && (pl.data || {}).name === "tts") {
        ttsRuns.add(pl.runId);
      }
      return;
    }
    // "chat" events carry the run's message stream, keyed by the runId from the
    // chat.send ack. state:"delta" -> accumulated text so far; state:"final" ->
    // complete content[] including {type:"attachment"} entries (TTS audio etc).
    if (m.type === "event" && m.event === "chat") {
      const pl = m.payload || {};
      const rid = pl.runId;
      const content = ((pl.message || {}).content) || [];
      const text = content.filter((c) => c.type === "text" && c.text)
        .map((c) => c.text).join(" ");
      if (pl.state === "final" || pl.state === "error") {
        console.error(`[bridge] evt chat run=${String(rid).slice(0, 8)} state=${pl.state}`
          + ` claimed=${pending.has(rid)} txt=${text.length}`
          + ` att=${content.filter((c) => c.type === "attachment").length} sess=${pl.sessionKey}`);
      }
      if (!rid || !pending.has(rid)) {
        // Unclaimed run (gateway-initiated followup). If a conflicted turn is
        // parked on this session, its answer arrives here — hand it over.
        // Event sessionKey is fully qualified ("agent:main:<key>"); ours is bare.
        if (pl.state === "final" && pl.sessionKey) {
          const bare = String(pl.sessionKey).split(":").pop();
          const list = sessionWaiters.get(bare);
          if (list && list.length) {
            const w = list.shift();
            clearTimeout(w.timer);
            const wireMedia = content
              .filter((c) => c.type === "attachment" && c.attachment && c.attachment.url)
              .map((c) => ({ url: c.attachment.url, kind: c.attachment.kind || "",
                             mimeType: c.attachment.mimeType || "" }));
            reconcileMedia(wireMedia, w.startedMs || 0, ttsRuns.has(rid) || !text.length)
              .then((media) => w.resolve({
                reply: text || (media.length ? "🔊" : "(no reply)"), media }));
          }
          ttsRuns.delete(rid);
        }
        return;
      }
      const tid = runToTurn.get(rid);
      const t = tid ? turns.get(tid) : null;
      if (pl.state === "delta") {
        if (t && text.length >= t.text.length) t.text = text;
        return;
      }
      if (pl.state === "final" || pl.state === "error") {
        const p = pending.get(rid);
        pending.delete(rid);
        clearTimeout(p.timer);
        if (pl.state === "error") {
          p.reject(new Error(pl.errorMessage || "chat run error"));
          return;
        }
        const wireMedia = content
          .filter((c) => c.type === "attachment" && c.attachment && c.attachment.url)
          .map((c) => ({ url: c.attachment.url, kind: c.attachment.kind || "",
                         mimeType: c.attachment.mimeType || "" }));
        // A voice-only reply (agent-invoked tts tool) has empty text but a
        // freshly-written audio file. If this run called the tts tool (or the
        // reply is textless), wait for the mp3 to land rather than give up.
        const expectVoice = ttsRuns.has(rid) || !text.length;
        ttsRuns.delete(rid);
        reconcileMedia(wireMedia, p.startedMs, expectVoice).then((media) => {
          p.resolve({ reply: text || (media.length ? "🔊" : "(no reply)"), media });
        });
      }
      return;
    }
    if (m.type === "res" && pending.has(m.id)) {
      const pl = m.payload || {};
      const p = pending.get(m.id);
      if (p.legacy) {
        // Legacy `agent` method: ack (status accepted) then final res with
        // payload.result.payloads[] on the same id.
        const result = pl.result;
        const isError = m.ok === false || m.error || pl.status === "error";
        const isFinal = (result && Array.isArray(result.payloads)) || isError;
        if (!isFinal) return; // ack
        pending.delete(m.id);
        clearTimeout(p.timer);
        if (isError) { p.reject(new Error(JSON.stringify(m.error || pl.error || m))); return; }
        const legacyText = result.payloads.map((x) => x.text).filter(Boolean).join(" ");
        reconcileMedia([], p.startedMs, !legacyText).then((media) => {
          p.resolve({ reply: legacyText || (media.length ? "🔊" : "(no reply)"), media });
        });
        return;
      }
      // chat.send res is an ACK: {runId, status:"started"}. Re-key the pending
      // entry (and turn mapping) from our request id to the gateway runId,
      // which all subsequent "chat" events carry.
      if (m.ok === false || m.error) {
        pending.delete(m.id);
        clearTimeout(p.timer);
        p.reject(new Error(JSON.stringify(m.error || m)));
        return;
      }
      if (pl.runId) {
        pending.delete(m.id);
        p.runId = pl.runId;
        pending.set(pl.runId, p);
        if (p.turnId && turns.has(p.turnId)) runToTurn.set(pl.runId, p.turnId);
      }
    }
  });
  ws.on("close", () => { connected = false; setTimeout(connect, 1000); });
  ws.on("error", (e) => { console.error("[bridge] ws error", e.message); });
}

function runAgentOnce(message, sessionKey, attachments, turnId, timeoutMs) {
  return new Promise((resolve, reject) => {
    if (!connected) return reject(new Error("gateway not connected"));
    const id = crypto.randomUUID(); // fresh per attempt (idempotencyKey too)
    // Voice files written on/after this run's start count as this turn's audio.
    // 2s slack absorbs clock skew between the tool's write and our timestamp.
    const p = { resolve, reject, timer: null, runId: null, turnId,
                startedMs: Date.now() - 2000 };
    p.timer = setTimeout(() => {
      pending.delete(id);
      if (p.runId) pending.delete(p.runId);
      reject(new Error("agent turn timeout"));
    }, timeoutMs || 280000);
    pending.set(id, p);
    // Attachment wire format matches the gateway's own subagent-spawn caller:
    // [{type:"image", source:{type:"base64", media_type, data}}]
    const gwAttachments = (attachments || [])
      .filter((a) => a && a.data && a.media_type)
      .map((a) => ({
        type: "image",
        source: { type: "base64", media_type: a.media_type, data: a.data },
      }));
    console.error(`[bridge] send req=${id.slice(0, 8)} sess=${sessionKey} len=${(message || "").length}`);
    ws.send(JSON.stringify({
      type: "req", id, method: "chat.send",
      params: {
        message, sessionKey, idempotencyKey: id,
        ...(gwAttachments.length ? { attachments: gwAttachments } : {}),
      },
    }));
  });
}

// chat.send session discipline (verified against gateway source):
// - Two reply inits racing on one session throw "reply session initialization
//   conflicted" (optimistic-concurrency). The loser's TEXT still lands in the
//   transcript as a fallback user turn, so RESENDING duplicates the turn —
//   the agent answers it twice. Never resend.
// - The turn WILL be answered by the gateway's own next run on that session
//   (followup drain / next inbound), under a fresh runId we never ack'd.
// Strategy: serialize our own sends per session (+ short cooldown covering
// post-final bookkeeping); if a conflict still occurs (e.g. racing a boot-time
// drain of stale turns), do NOT resend — park the turn as a session waiter and
// resolve it with the next UNCLAIMED final event on that session.
const sessionQueues = new Map(); // sessionKey -> tail promise
const sessionLastDone = new Map(); // sessionKey -> ts of last settled run
const sessionWaiters = new Map(); // sessionKey -> [{resolve, reject, timer}]
const SESSION_COOLDOWN_MS = 3000;
// A conflicted turn's text sits in the transcript UNANSWERED until some run
// touches the session (verified in gateway source: the conflict path persists
// the turn but never enqueues a followup). Frame-level evidence: after a turn
// completes, post-run bookkeeping keeps writing the session store for 60s+,
// so EVERY chat.send in that window insta-conflicts — including nudges sent
// via chat.send. The legacy `agent` RPC bypasses reply-session init entirely
// (no conflict surface; weeks of production use), so the nudge goes there.
const WAITER_TIMEOUT_MS = 5000;
const NUDGE_MESSAGE = "(system: the user's previous message was accepted but "
  + "not yet answered — answer it now, addressing the user directly)";

function awaitSessionFinal(sessionKey) {
  return new Promise((resolve, reject) => {
    const w = { resolve, reject, timer: null, startedMs: Date.now() - 2000 };
    w.timer = setTimeout(() => {
      const list = sessionWaiters.get(sessionKey) || [];
      const i = list.indexOf(w);
      if (i >= 0) list.splice(i, 1);
      reject(new Error("queued turn not answered in time"));
    }, WAITER_TIMEOUT_MS);
    (sessionWaiters.get(sessionKey) || sessionWaiters.set(sessionKey, []).get(sessionKey)).push(w);
  });
}

function runAgent(message, sessionKey, attachments, turnId, timeoutMs) {
  const attempt = async () => {
    const since = Date.now() - (sessionLastDone.get(sessionKey) || 0);
    if (since < SESSION_COOLDOWN_MS) {
      await new Promise((r) => setTimeout(r, SESSION_COOLDOWN_MS - since));
    }
    try {
      return await runAgentOnce(message, sessionKey, attachments, turnId, timeoutMs);
    } catch (e) {
      if (!/initialization conflicted/.test(String(e.message))) throw e;
      // The message text is already in the transcript (never resend it — that
      // duplicates the turn). Give a genuine gateway run a short window to
      // answer it; otherwise trigger one ourselves with a neutral nudge, whose
      // reply IS the answer to the parked message.
      console.error(`[bridge] session busy (${sessionKey}); awaiting followup, nudge in ${WAITER_TIMEOUT_MS / 1000}s`);
      try {
        return await awaitSessionFinal(sessionKey);
      } catch (_) {
        console.error(`[bridge] no followup for (${sessionKey}); nudging via legacy agent`);
        return await runAgentLegacy(NUDGE_MESSAGE, sessionKey, timeoutMs);
      }
    } finally {
      sessionLastDone.set(sessionKey, Date.now());
    }
  };
  const prev = sessionQueues.get(sessionKey) || Promise.resolve();
  const run = prev.catch(() => {}).then(attempt);
  sessionQueues.set(sessionKey, run);
  run.finally(() => {
    if (sessionQueues.get(sessionKey) === run) sessionQueues.delete(sessionKey);
  }).catch(() => {});
  return run;
}

// Fallback turn via the legacy `agent` RPC: two res frames on the SAME request
// id (ack with status "accepted", then final with payload.result.payloads[]).
// No reply-session init -> immune to the conflicted-store window. No TTS/media
// on this path (it bypasses the channel pipeline) — text-only degradation.
function runAgentLegacy(message, sessionKey, timeoutMs) {
  return new Promise((resolve, reject) => {
    if (!connected) return reject(new Error("gateway not connected"));
    const id = crypto.randomUUID();
    const p = { resolve, reject, timer: null, legacy: true, startedMs: Date.now() - 2000 };
    p.timer = setTimeout(() => {
      pending.delete(id); reject(new Error("agent turn timeout"));
    }, timeoutMs || 280000);
    pending.set(id, p);
    console.error(`[bridge] legacy-agent send req=${id.slice(0, 8)} sess=${sessionKey}`);
    ws.send(JSON.stringify({
      type: "req", id, method: "agent",
      params: { message, sessionKey, idempotencyKey: id },
    }));
  });
}

async function readJsonBody(req) {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  return JSON.parse(Buffer.concat(chunks).toString() || "{}");
}

http.createServer(async (req, res) => {
  const u = new URL(req.url, "http://x");
  if (u.pathname === "/ready") {
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ connected }));
  }
  if (u.pathname === "/agent-async" && req.method === "POST") {
    // Fire an agent turn and return immediately; poll /progress for text.
    let body;
    try { body = await readJsonBody(req); } catch {
      res.writeHead(400, { "Content-Type": "application/json" });
      return res.end(JSON.stringify({ error: "bad json body" }));
    }
    gcTurns();
    const turnId = crypto.randomUUID();
    turns.set(turnId, { text: "", done: false, reply: null, media: null, error: null, at: Date.now() });
    runAgent(body.m || "", body.s || "default", body.attachments || null, turnId,
             ASYNC_TURN_TIMEOUT_MS)
      .then((r) => {
        const t = turns.get(turnId);
        if (t) { t.done = true; t.reply = r.reply; t.media = r.media; t.at = Date.now(); }
      })
      .catch((e) => {
        console.error("[bridge] async agent turn failed:", e);
        const t = turns.get(turnId);
        if (t) { t.done = true; t.error = "agent turn failed"; t.at = Date.now(); }
      });
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ turnId }));
  }
  if (u.pathname === "/progress") {
    const t = turns.get(u.searchParams.get("id") || "");
    if (!t) {
      res.writeHead(404, { "Content-Type": "application/json" });
      return res.end(JSON.stringify({ error: "unknown turn" }));
    }
    t.at = Date.now(); // being polled = alive; see gcTurns

    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({
      text: t.text, done: t.done,
      ...(t.reply !== null ? { reply: t.reply } : {}),
      ...(t.media && t.media.length ? { media: t.media } : {}),
      ...(t.error ? { error: t.error } : {}),
    }));
  }
  if (u.pathname === "/agent") {
    let msg = u.searchParams.get("m") || "";
    let sess = u.searchParams.get("s") || "default";
    let attachments = null;
    if (req.method === "POST") {
      // JSON body variant for image turns (base64 too big for a query string).
      try {
        const chunks = [];
        for await (const c of req) chunks.push(c);
        const body = JSON.parse(Buffer.concat(chunks).toString() || "{}");
        msg = body.m || msg;
        sess = body.s || sess;
        attachments = body.attachments || null;
      } catch {
        res.writeHead(400, { "Content-Type": "application/json" });
        return res.end(JSON.stringify({ error: "bad json body" }));
      }
    }
    try {
      const r = await runAgent(msg, sess, attachments);
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ reply: r.reply,
        ...(r.media && r.media.length ? { media: r.media } : {}) }));
    } catch (e) {
      // Log the detail server-side only; never echo exception text to the client
      // (CodeQL js/stack-trace-exposure).
      console.error("[bridge] agent turn failed:", e);
      res.writeHead(500, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "agent turn failed" }));
    }
    return;
  }
  res.writeHead(404); res.end();
}).listen(PORT, "127.0.0.1", () => console.error(`[bridge] http on :${PORT}`));

connect();
