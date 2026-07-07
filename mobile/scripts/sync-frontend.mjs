#!/usr/bin/env node
/**
 * sync-frontend.mjs — regenerate mobile/www/ from the single source of truth (frontend/).
 *
 * WHAT IT DOES
 *   1. Wipes and recreates mobile/www/ (www/ is gitignored — a pure build artifact).
 *   2. Copies from ../frontend: index.html, manifest.webmanifest, sw.js, icon.svg,
 *      measure.html, and the whole vendor/ tree (three.js + device-catalog.json).
 *   3. Copies mobile/src/wavr-mobile-shim.js into www/ (owned by the shim author), and
 *      mobile/src/wavr-lib.js into www/ (the pure-logic lib the shim consumes).
 *   4. Injects <script src="wavr-mobile-shim.js"></script> into www/index.html
 *      IMMEDIATELY BEFORE the main inline app <script> — so the shim sets
 *      window.WAVR_MOBILE (and installs its fetch/WebSocket -> WavrNet routing)
 *      BEFORE the page's app code runs. The importmap (<script type="importmap">)
 *      and any src'd script are skipped; the first bare inline <script> is the app.
 *      Then injects <script src="wavr-lib.js"></script> IMMEDIATELY BEFORE that shim
 *      <script> tag, so window.WavrLib exists before the shim's IIFE runs.
 *
 * INVARIANTS
 *   - NEVER modifies anything under frontend/ (source stays the single source of truth).
 *   - Idempotent: re-running produces byte-identical www/index.html; the shim and lib
 *     tags are each injected exactly once (guarded by marker comments).
 *   - Pure Node fs (no shell heredoc/redirect — avoids the Windows zero-byte-file trap).
 *
 * Run: npm run sync-frontend   (from mobile/)
 */

import { fileURLToPath } from 'node:url';
import path from 'node:path';
import fs from 'node:fs';

const scriptDir = path.dirname(fileURLToPath(import.meta.url)); // mobile/scripts
const mobileDir = path.dirname(scriptDir);                      // mobile
const repoRoot = path.dirname(mobileDir);                       // repo root
const frontendDir = path.join(repoRoot, 'frontend');
const wwwDir = path.join(mobileDir, 'www');
const shimSrc = path.join(mobileDir, 'src', 'wavr-mobile-shim.js');
const libSrc = path.join(mobileDir, 'src', 'wavr-lib.js');

const SHIM_FILENAME = 'wavr-mobile-shim.js';
const SHIM_MARKER = 'Wavr Mobile: native shim';
const SHIM_TAG =
  `<!-- ${SHIM_MARKER} — sets window.WAVR_MOBILE and routes central I/O through the\n` +
  `     WavrNet native plugin. Injected by mobile/scripts/sync-frontend.mjs. Do NOT edit\n` +
  `     here; edit mobile/src/${SHIM_FILENAME}. Regenerated on every sync-frontend run. -->\n` +
  `<script src="${SHIM_FILENAME}"></script>\n`;

const LIB_FILENAME = 'wavr-lib.js';
const LIB_MARKER = 'Wavr Mobile: pure-logic lib';
const LIB_TAG =
  `<!-- ${LIB_MARKER} — exposes window.WavrLib (consent->actions, mDNS parse, etc.) that\n` +
  `     the shim consumes. Injected by mobile/scripts/sync-frontend.mjs. Do NOT edit here;\n` +
  `     edit mobile/src/${LIB_FILENAME}. Regenerated on every sync-frontend run. -->\n` +
  `<script src="${LIB_FILENAME}"></script>\n`;

// Named files copied verbatim from frontend/ -> www/ (dirs handled separately).
const FILES = [
  'index.html',            // gets the shim injected after copy
  'manifest.webmanifest',
  'sw.js',
  'icon.svg',
  'measure.html',
];
// Directory trees copied recursively from frontend/ -> www/.
const DIRS = ['vendor']; // three.js build/examples + device-catalog.json

function log(msg) {
  process.stdout.write(`[sync-frontend] ${msg}\n`);
}

function fail(msg) {
  process.stderr.write(`[sync-frontend] ERROR: ${msg}\n`);
  process.exit(1);
}

/**
 * Inject the shim <script> tag immediately before the first inline app <script>.
 * "App <script>" = the first opening <script ...> that has NEITHER a `type=` attribute
 * (skips <script type="importmap"> and any JSON/module blocks) NOR a `src=` attribute
 * (skips external scripts). In Wavr's index.html that is the big inline app script.
 * Idempotent via SHIM_MARKER.
 */
function injectShim(html) {
  if (html.includes(SHIM_MARKER) || html.includes(`src="${SHIM_FILENAME}"`)) {
    log('shim tag already present in index.html — leaving as-is (idempotent).');
    return html;
  }
  // Match an opening <script> tag whose attribute list contains no `type=` and no `src=`.
  const appScriptRe = /<script(?![^>]*\b(?:type|src)\s*=)[^>]*>/i;
  const m = appScriptRe.exec(html);
  if (!m) {
    fail(
      'could not locate the main inline <script> in index.html to inject the shim before. ' +
        'The page structure changed — update the appScriptRe anchor in sync-frontend.mjs.'
    );
  }
  const at = m.index;
  log(`injecting shim <script> before the main inline app script at byte offset ${at}.`);
  return html.slice(0, at) + SHIM_TAG + html.slice(at);
}

/**
 * Inject the lib <script> tag immediately before the shim <script> tag (which injectShim()
 * has already placed before the main inline app script). window.WavrLib must exist before
 * the shim's IIFE runs, so the lib tag must load first. Idempotent via LIB_MARKER.
 */
function injectLib(html) {
  if (html.includes(LIB_MARKER) || html.includes(`src="${LIB_FILENAME}"`)) {
    log('lib tag already present in index.html — leaving as-is (idempotent).');
    return html;
  }
  // Anchor on the WHOLE shim block (its marker comment + <script> tag), not just the bare
  // <script> line, so the lib's own comment + tag lands entirely before it — no interleaving.
  const at = html.indexOf(SHIM_TAG);
  if (at === -1) {
    fail(
      'could not locate the shim <script> tag block in index.html to inject the lib tag before it. ' +
        'injectShim() should have placed it already — check injection order in main().'
    );
  }
  log(`injecting lib <script> before the shim <script> block at byte offset ${at}.`);
  return html.slice(0, at) + LIB_TAG + html.slice(at);
}

function main() {
  if (!fs.existsSync(frontendDir)) fail(`frontend/ not found at ${frontendDir}`);
  const indexPath = path.join(frontendDir, 'index.html');
  if (!fs.existsSync(indexPath)) fail(`frontend/index.html not found at ${indexPath}`);

  // 1. Fresh www/ (it is gitignored; wipe to avoid stale files surviving a source deletion).
  fs.rmSync(wwwDir, { recursive: true, force: true });
  fs.mkdirSync(wwwDir, { recursive: true });
  log(`regenerated ${path.relative(repoRoot, wwwDir)}/`);

  // 2. Named files (index.html handled specially for injection).
  for (const name of FILES) {
    const src = path.join(frontendDir, name);
    const dest = path.join(wwwDir, name);
    if (!fs.existsSync(src)) {
      // manifest/sw/icon/measure are expected; warn but don't hard-fail on optional extras.
      if (name === 'index.html') fail(`required source missing: ${src}`);
      log(`WARNING: source missing, skipped: frontend/${name}`);
      continue;
    }
    if (name === 'index.html') {
      const shimmed = injectShim(fs.readFileSync(src, 'utf8'));
      const injected = injectLib(shimmed);
      fs.writeFileSync(dest, injected);
      log(`copied + processed frontend/${name} -> www/${name}`);
    } else {
      fs.copyFileSync(src, dest);
      log(`copied frontend/${name} -> www/${name}`);
    }
  }

  // 3. Directory trees.
  for (const dir of DIRS) {
    const src = path.join(frontendDir, dir);
    const dest = path.join(wwwDir, dir);
    if (!fs.existsSync(src)) {
      log(`WARNING: source dir missing, skipped: frontend/${dir}/`);
      continue;
    }
    fs.cpSync(src, dest, { recursive: true });
    log(`copied frontend/${dir}/ -> www/${dir}/ (recursive)`);
  }

  // 4. The native shim (owned by the parallel shim-author agent; may not exist yet).
  if (fs.existsSync(shimSrc)) {
    fs.copyFileSync(shimSrc, path.join(wwwDir, SHIM_FILENAME));
    log(`copied mobile/src/${SHIM_FILENAME} -> www/${SHIM_FILENAME}`);
  } else {
    log(
      `WARNING: ${path.relative(repoRoot, shimSrc)} does not exist yet. The <script> tag ` +
        `WAS injected into www/index.html, but www/${SHIM_FILENAME} is missing — the app ` +
        `will 404 on it until the shim author lands mobile/src/${SHIM_FILENAME}, then re-run ` +
        `sync-frontend. (This is expected during parallel scaffolding.)`
    );
  }

  // 5. The pure-logic lib the shim consumes (may not exist yet during parallel scaffolding).
  if (fs.existsSync(libSrc)) {
    fs.copyFileSync(libSrc, path.join(wwwDir, LIB_FILENAME));
    log(`copied mobile/src/${LIB_FILENAME} -> www/${LIB_FILENAME}`);
  } else {
    log(
      `WARNING: ${path.relative(repoRoot, libSrc)} does not exist yet. The <script> tag ` +
        `WAS injected into www/index.html, but www/${LIB_FILENAME} is missing — the app ` +
        `will 404 on it until mobile/src/${LIB_FILENAME} lands, then re-run sync-frontend. ` +
        `(This is expected during parallel scaffolding.)`
    );
  }

  log('done.');
}

main();
