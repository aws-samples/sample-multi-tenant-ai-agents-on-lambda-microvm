# OpenClaw on Lambda MicroVMs — EFS-persisted state variant (arm64).
# Runs as ROOT (mounting NFS needs privileges + ALL os capabilities at image level).
# Base is PINNED BY DIGEST deliberately: `:slim` is a moving tag (today it resolves to
# the same digest as `:latest`) and drifting off this version breaks the build silently
# or not-so-silently. What a bump has to deal with, as of 2026.9.2:
#   - 2026.8.1 unbundled the DuckDuckGo search provider into a separate official plugin,
#     so openclaw.json's `tools.web.search.provider: duckduckgo` makes the gateway refuse to start
#     ("web_search provider is not available", exit 78) — which fails the snapshot and
#     therefore the whole image build. A bump needs
#     `plugins install @openclaw/duckduckgo-plugin` BEFORE openclaw.json is in place:
#     `plugins install` validates the config too, so the config naming an uninstalled
#     provider makes the install itself exit 1.
#   - patch-tts-ja.mjs patches compiled OpenClaw internals and only WARNS when its
#     trigger stops matching, so Japanese TTS degrades without failing the build.
#   - efs-monitor.sh's session heal reaches into the state layout by path
#     (agents/main/sessions/sessions.json); that layout is version-coupled, so re-check
#     it against the new version or the heal silently becomes a no-op.
FROM ghcr.io/openclaw/openclaw:2026.6.11-slim@sha256:3814fb1f62f9cfc5944de088c5817c68c88b5d721feebe36420b666a90a61ce7

USER root
RUN apt-get update && apt-get install -y --no-install-recommends nfs-common \
    && rm -rf /var/lib/apt/lists/*

COPY openclaw.json hooks.py start.sh efs-monitor.sh gw-bridge.cjs materialize-models.mjs patch-tts-ja.mjs /opt/poc/
RUN chmod +x /opt/poc/start.sh /opt/poc/efs-monitor.sh \
    && mkdir -p /home/node/.openclaw /mnt/efs \
    && cp /opt/poc/openclaw.json /home/node/.openclaw/openclaw.json \
    && HOME=/home/node node /app/openclaw.mjs plugins install @openclaw/amazon-bedrock-provider \
    # Teach the Edge TTS voice picker to speak Japanese (stock only auto-switches
    # to Chinese for CJK; kana-based Japanese detection is missing upstream).
    && node /opt/poc/patch-tts-ja.mjs \
    && chown -R node:node /home/node \
    # Gateway runs as root; OpenClaw blocks plugins whose files aren't root-owned
    # ("suspicious ownership" guard), so the plugin tree must stay root:root.
    && chown -R root:root /home/node/.openclaw/npm

ENV HOME=/home/node
EXPOSE 18789 8080
ENTRYPOINT ["tini", "-s", "--"]
CMD ["/opt/poc/start.sh"]
