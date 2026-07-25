// Materialize Bedrock model discovery into openclaw.json at cold start.
//
// Why: the gateway's image-attachment guard and `models list` read only the
// explicit `models.providers.<id>.models` config array — they never consult the
// plugin's live discovery catalog (OpenClaw integration gap, verified 2026-07).
// So we run the plugin's own discoverBedrockModels() once per cold start
// (measured ~1.3s warm / ~3.3s cold) and write the result into the config the
// guard does read. Fallback: on any failure the config keeps whatever models
// array it already has (the static seed list baked into the image).
//
// Discovery costs ~1.6s of every cold start while the Bedrock catalog changes on the
// order of weeks, so the result is cached (per tenant, on EFS) and reused until the
// TTL elapses. A cache hit is a single file read.
//
// Runs as: node materialize-models.mjs <config-path> [cache-path]  (before gateway starts)
import { readFileSync, writeFileSync, readdirSync } from "node:fs";

const CONFIG_PATH = process.argv[2] || "/home/node/.openclaw/openclaw.json";
const CACHE_PATH = process.argv[3] || null;
const CACHE_TTL_MS = 24 * 60 * 60 * 1000;

function readCache() {
  if (!CACHE_PATH) return null;
  try {
    const c = JSON.parse(readFileSync(CACHE_PATH, "utf8"));
    if (!Array.isArray(c.models) || !c.models.length) return null;
    const age = Date.now() - (c.at || 0);
    if (age < 0 || age > CACHE_TTL_MS) return null;
    return { models: c.models, ageMs: age };
  } catch {
    return null;
  }
}

// The plugin project dir carries a content-hash suffix that changes across
// plugin versions — locate it instead of hardcoding.
function findPluginDir() {
  const projects = "/home/node/.openclaw/npm/projects";
  const entry = readdirSync(projects).find((d) =>
    d.startsWith("openclaw-amazon-bedrock-provider-"),
  );
  if (!entry) throw new Error("bedrock provider plugin dir not found");
  return `${projects}/${entry}/node_modules/@openclaw/amazon-bedrock-provider`;
}

try {
  const cfg = JSON.parse(readFileSync(CONFIG_PATH, "utf8"));
  const provider = cfg?.models?.providers?.["amazon-bedrock"];
  if (!provider) throw new Error("no amazon-bedrock provider in config");

  const discovery =
    cfg?.plugins?.entries?.["amazon-bedrock"]?.config?.discovery ?? {};
  if (discovery.enabled === false) {
    console.log("[materialize-models] discovery disabled; keeping static list");
    process.exit(0);
  }

  const cached = readCache();
  let models, source;
  if (cached) {
    models = cached.models;
    source = `cache (age ${Math.round(cached.ageMs / 3600000)}h)`;
  } else {
    const { discoverBedrockModels } = await import(
      `${findPluginDir()}/dist/discovery.js`
    );
    models = await discoverBedrockModels({
      region: discovery.region || process.env.AWS_REGION || "us-east-1",
      config: { ...discovery, refreshInterval: 0 },
    });
    if (!Array.isArray(models) || models.length === 0)
      throw new Error("discovery returned no models");
    source = "live discovery";
    if (CACHE_PATH) {
      try {
        writeFileSync(CACHE_PATH, JSON.stringify({ at: Date.now(), models }));
      } catch (e) {
        console.log(`[materialize-models] cache write failed: ${e.message}`);
      }
    }
  }

  provider.models = models;
  writeFileSync(CONFIG_PATH, JSON.stringify(cfg, null, 1));
  const withImage = models.filter((m) => m.input?.includes("image")).length;
  console.log(
    `[materialize-models] wrote ${models.length} models (${withImage} vision)`
      + ` from ${source} to ${CONFIG_PATH}`,
  );
} catch (e) {
  console.log(
    `[materialize-models] FAILED (${String(e.message).slice(0, 120)}); keeping static list`,
  );
}
