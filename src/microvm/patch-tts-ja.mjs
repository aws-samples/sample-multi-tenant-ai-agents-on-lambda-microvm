#!/usr/bin/env node
// Build-time patch: teach OpenClaw's Edge TTS voice auto-selection to speak
// Japanese. Stock OpenClaw only auto-switches English->Chinese for CJK text;
// its isCjkDominant() checks Han ideographs but NOT kana, so Japanese either
// stays on the English voice (kana-heavy -> Edge returns empty audio) or is
// mis-detected as Chinese (kanji-dense -> spoken with Mandarin pronunciation).
//
// We inject a kana check (hiragana U+3040-309F, katakana U+30A0-30FF) BEFORE
// the Chinese branch: kana is exclusive to Japanese, so its presence reliably
// means Japanese and never false-triggers on Chinese. Chinese/English keep
// working exactly as before.
//
// Runs against the compiled provider in the base image at Docker build time
// (baked into the image, unaffected by EFS config authority). Idempotent.
import { readdirSync, readFileSync, writeFileSync, statSync } from "node:fs";
import { join } from "node:path";

const ROOTS = ["/app/dist", "/app"];
const MARKER = "ja-JP-NanamiNeural"; // present only after we patch

// Match the stock auto-switch block, whitespace-insensitively.
const TRIGGER = /if\s*\(\s*!overrideVoice\s*&&\s*voice === DEFAULT_EDGE_VOICE\s*&&\s*isCjkDominant\(req\.text\)\)\s*\{\s*voice = DEFAULT_CHINESE_EDGE_VOICE;\s*lang = DEFAULT_CHINESE_EDGE_LANG;\s*\}/;

const REPLACEMENT =
  'if (!overrideVoice && voice === DEFAULT_EDGE_VOICE) { '
  + 'if (/[\\u3040-\\u30ff]/.test(req.text)) { voice = "ja-JP-NanamiNeural"; lang = "ja-JP"; } '
  + 'else if (isCjkDominant(req.text)) { voice = DEFAULT_CHINESE_EDGE_VOICE; lang = DEFAULT_CHINESE_EDGE_LANG; } }';

function* walk(dir) {
  let entries;
  try { entries = readdirSync(dir, { withFileTypes: true }); } catch { return; }
  for (const e of entries) {
    const p = join(dir, e.name);
    if (e.isDirectory()) {
      if (e.name === "node_modules" || e.name === ".git") continue;
      yield* walk(p);
    } else if (e.name.endsWith(".js") || e.name.endsWith(".mjs")) {
      // .mjs matters: the bundle ships the provider as
      // /app/dist/speech-provider-<hash>.mjs, so a .js-only walk found nothing and this
      // patch silently did nothing. Scanning both costs ~270ms instead of ~130ms.
      yield p;
    }
  }
}

let patched = 0, alreadyOk = 0, candidates = 0;
const seen = new Set();
for (const root of ROOTS) {
  try { statSync(root); } catch { continue; }
  for (const file of walk(root)) {
    if (seen.has(file)) continue;
    seen.add(file);
    let src;
    try { src = readFileSync(file, "utf8"); } catch { continue; }
    if (!src.includes("DEFAULT_CHINESE_EDGE_VOICE") || !src.includes("isCjkDominant")) continue;
    candidates++;
    if (src.includes(MARKER)) { alreadyOk++; console.log(`[patch-tts-ja] already patched: ${file}`); continue; }
    if (!TRIGGER.test(src)) {
      console.error(`[patch-tts-ja] trigger not found in ${file} (OpenClaw internals changed?)`);
      continue;
    }
    writeFileSync(file, src.replace(TRIGGER, REPLACEMENT));
    patched++;
    console.log(`[patch-tts-ja] patched Japanese voice selection into ${file}`);
  }
}

// Fail the build rather than warn. This used to print a WARNING and exit 0, so the image
// kept claiming it teaches the voice picker Japanese long after a base-image change had
// moved the provider out of reach of the walk. A pinned base image means this can only
// break when the pin is deliberately moved, which is exactly when a build failure is the
// signal you want.
if (candidates === 0) {
  console.error("[patch-tts-ja] no speech-provider file found under " + ROOTS.join(", "));
  process.exit(1);
}
if (patched === 0 && alreadyOk === 0) {
  console.error(`[patch-tts-ja] found ${candidates} provider file(s) but patched none`);
  process.exit(1);
}
console.log(`[patch-tts-ja] done: patched=${patched} alreadyPatched=${alreadyOk} candidates=${candidates}`);
