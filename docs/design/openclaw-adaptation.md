# OpenClaw → Lambda MicroVM — Architecture Adaptation

> Design artifact (no commands run). Source: sub-agent research 2026-07-04.
> ⚠️ Every OpenClaw-*specific* claim is UNVERIFIED (this env fabricates data for the repo);
> confirm against a **local `git clone`** before building. Node/Docker/AWS/Firecracker
> reasoning below is reliable general knowledge.

## 1. Port mapping — expose ONE port: 18789
- MicroVM gives exactly ONE HTTPS URL → one inbound listener (HTTP/2, gRPC, WS supported on it; still one port). Inbound carries `X-aws-proxy-auth`.
- OpenClaw ports (UNVERIFIED): **18789** Control UI + its HTTP API, **18790** bridge (internal), **3978** MS Teams webhook receiver.
- **Front 18789** — it's request-driven (matches resume-on-request) and human-observable. Leave 18790/3978 bound internally, unexposed. No reverse-proxy multiplexing for a PoC.
- Do NOT expose a webhook port (3978) as the surface — push-based + time-sensitive, wrong for suspend/resume.

## 2. State/persistence — the crux (and it's SIMPLER than docker volumes)
- No runtime `docker run -v`. Persistence = Firecracker snapshot of **guest disk + guest RAM + running process**. So:
  - Anything on the guest FS persists. Anything in RAM persists (process frozen/thawed, not restarted).
  - What does NOT survive: live TCP/TLS sockets, wall-clock timers, tokens with absolute expiry.
- **Fix = don't fight it:** let OpenClaw write its data dir to a normal on-disk path (e.g. force `/data/openclaw`), ensure `$HOME` maps to persisted disk, avoid tmpfs. Snapshot captures it.
- Bedrock HTTPS calls = short-lived, SDK re-establishes → low risk. IM sockets = high risk (dead on resume) → avoided by §3.

## 3. Core tension: persistent IM gateway ⚔️ idle-auto-suspend
- OpenClaw is designed to sit **persistently connected** to Slack/Telegram/Teams. MicroVM wants to be **idle & suspended**, waking on a request to its URL. Fundamentally opposite lifecycles.
- **Resolution for PoC: drive OpenClaw via its HTTP API/Control UI, NOT real IM integrations.** This uses OpenClaw's real agent loop + real Bedrock calls + real session persistence, while dodging dead-socket + webhook-timeout races. IM integration = explicitly out of scope (phase 2).

## 4. Image build path — verify the base-image constraint FIRST
- **Option (a)** thin `FROM ghcr.io/openclaw/openclaw:slim` — trivial, inherits exact runtime. Works **only if MicroVM builder allows arbitrary FROM**.
- **Option (b)** `FROM <aws al2023 microvm base>` + install Node 24/pnpm/OpenClaw — needed if base is mandated. Compromise: multi-stage `COPY --from=ghcr...slim` the built app into the al2023 base.
- **Plan for (b), prefer (a).** Pivotal unknown: does the builder pin its own base image (for guest-init / proxy-auth agent)? → Phase 0 doc check.

## 5. Bedrock — default credential chain, NO static keys
- AWS SDK (JS v3) default provider chain auto-resolves the execution role (env vars / container-cred endpoint / IMDS — all SDK-compatible) and **auto-refreshes across suspend/resume** → Bedrock immune to the clock-jump risk.
- Config OpenClaw's Bedrock provider with **region + model id only, omit accessKeyId/secretAccessKey** so it falls through to the role.
- **TOP RISK (UNVERIFIED):** does OpenClaw's Bedrock provider config *allow* credential-less/ambient-chain mode, or does it *force* static keys? If forced → blocker for the role-only story. Verify in real repo.
- Grant role `bedrock:InvokeModel` (+ `...WithResponseStream` for streaming) on the model ARN; set `AWS_REGION`.

## 6. Sizing (ceiling 16/32/32) — go small
- **2 vCPU · 2 GB RAM · 8 GB disk** for slim PoC. Node gateway is I/O-bound; Bedrock does inference remotely. Smaller RAM → smaller snapshot → faster resume. Bump to 4–8 GB only for the `browser` variant.

## 7. Demo storyline — with the sharpest possible observable
1. Seed memory via HTTP API: *"Remember: launch code is TANGERINE-42."* (real agent + Bedrock + persist)
2. Also mint an **in-RAM session id / counter / uptime** at step 1.
3. Go idle → MicroVM **auto-suspends** (snapshot; no compute billed).
4. Resume via a request: *"What was the launch code?"* → **auto-resume**.
5. **Proof:** recalls TANGERINE-42 with no re-seed. **Sharpest observable:** the same in-RAM session id/counter/uptime comes back unchanged → proves *true Firecracker process resume*, not a container restart that merely reloaded a JSON file from disk. Bonus: near-instant answer (no cold Node boot).

## 8. Must-verify-against-local-clone checklist (before building)
- **Data:** exact data-dir path + override flag/env; is it plain disk (not tmpfs); RAM vs disk split for sessions.
- **Startup:** exact foreground start cmd (`openclaw gateway --port 18789`?); does `:slim` start a server by default or need a subcommand; is it arm64; health endpoint on 18789?
- **Ports:** does 18789 also serve the HTTP API you'll drive; UI↔bridge dependency.
- **Bedrock:** exact provider config keys (region field, model-id format `bedrock/<id>`? inference-profile ARN?); **does it allow credential-less config** (top risk); SDK v3?
- **Platform:** does builder allow arbitrary FROM; how are role creds delivered (env/endpoint/IMDS); two auth layers? (`X-aws-proxy-auth` platform token + OpenClaw's own gateway token).
- **Suspend/resume (empirical):** does OpenClaw survive the clock jump without crashing on expired timers/tokens.
