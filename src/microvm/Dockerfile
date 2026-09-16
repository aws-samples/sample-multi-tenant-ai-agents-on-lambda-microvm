# OpenClaw on Lambda MicroVMs — EFS-persisted state variant (arm64).
# PID 1 (tini) and the mount daemon run as ROOT: mounting NFS needs privileges + ALL os
# capabilities at image level. The gateway, its bridge and therefore every tool the
# agent can call run as the image's stock uid 1000 (see start.sh).
# Pinned by digest: `slim` is a moving tag and OpenClaw changes behaviour between
# releases (plugin allowlist, process title, state format). 3608b6f… is 2026.9.4.
FROM ghcr.io/openclaw/openclaw:slim@sha256:3608b6f29352a7948bf0e868a1e3c78d9f9c92e55e5bb50b3b6b11cf1c58c5ad

USER root
# nfs-common: the NFS client. iproute2: `ip rule` / `ip route`, which efs-monitor.sh uses
# to keep uid 1000 from reaching the EFS mount target at all (the guest kernel ships no
# iptables owner match, see efs-monitor.sh).
RUN apt-get update && apt-get install -y --no-install-recommends nfs-common iproute2 \
    && rm -rf /var/lib/apt/lists/*

COPY openclaw.json hooks.py start.sh efs-monitor.sh gw-bridge.cjs materialize-models.mjs patch-tts-ja.mjs /opt/poc/
RUN chmod +x /opt/poc/start.sh /opt/poc/efs-monitor.sh \
    && mkdir -p /home/node/.openclaw /mnt/efs \
    && cp /opt/poc/openclaw.json /home/node/.openclaw/openclaw.json \
    && HOME=/home/node node /app/openclaw.mjs plugins install @openclaw/amazon-bedrock-provider \
    # Key-free web search provider; 2026.9 moved it out of core into a plugin, and the
    # config's plugins.allow must name it or the gateway rejects the config.
    && HOME=/home/node node /app/openclaw.mjs plugins install @openclaw/duckduckgo-plugin \
    # Teach the Edge TTS voice picker to speak Japanese (stock only auto-switches
    # to Chinese for CJK; kana-based Japanese detection is missing upstream).
    && node /opt/poc/patch-tts-ja.mjs \
    # The state dir belongs to the uid the gateway runs as.
    && chown -R node:node /home/node \
    # Plugins: OpenClaw accepts a tree owned by the running uid OR by root ("suspicious
    # ownership" guard). Keep it root-owned and move it into the image — start.sh binds it
    # read-only into the state dir. Otherwise ~10k plugin files get copied onto EFS per
    # tenant, and the agent could edit the code it runs.
    && chown -R root:root /home/node/.openclaw/npm \
    && mv /home/node/.openclaw/npm /opt/poc/npm \
    && mkdir -p /home/node/.openclaw/npm && chown node:node /home/node/.openclaw/npm
# The agent runs with no_new_privs (start.sh), which already neuters setuid binaries;
# stripping the bits makes the property hold even for a process launched some other way.
RUN find / -xdev -perm -4000 -type f -exec chmod u-s {} +

ENV HOME=/home/node
EXPOSE 18789 8080
ENTRYPOINT ["tini", "-s", "--"]
CMD ["/opt/poc/start.sh"]
