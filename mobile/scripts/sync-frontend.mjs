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

// WHICH dashboard gets bundled, and why it is overridable.
//
// The mobile shell lives on its own branch, and that branch's `frontend/` is
// frozen at whatever it was when the branch was cut. Ours was cut in July. Seven
// weeks and 234 commits later the app was still shipping the July dashboard: one
// monolithic inline script, no `js/` modules, no `data-i18n`, and none of the
// fixes made since — including the one that stops the internal room token
// `casa` being shown to a person as if it were a room. The first user paired his
// phone and read "casa · uncertain · 40%" on it.
//
// The app is not supposed to be a different product from the dashboard; it is
// supposed to be the same one, on a phone. So the source is a variable, and
// WAVR_FRONTEND_DIR points the build at the checkout whose dashboard should
// ship. Nothing about this branch's own files changes.
const frontendDir = process.env.WAVR_FRONTEND_DIR
  ? path.resolve(process.env.WAVR_FRONTEND_DIR)
  : path.join(repoRoot, 'frontend');
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
//
// `js/` is the dashboard. It did not exist when this list was written — the
// whole app was one inline script — and nothing added it when the page was
// split into 44 modules, because this build was never run against a dashboard
// that had them. Bundling index.html without `js/` produces an app that loads
// 44 scripts, 404s on every one, and renders the chrome with nothing behind it:
// no tab switches, no `WavrT`, no `MODE`. The same photograph-of-a-product
// failure the token-exempt-paths comment in `app.py` describes, arrived by a
// different door.
const DIRS = ['js', 'vendor']; // the dashboard's modules + three.js/device-catalog

function log(msg) {
  process.stdout.write(`[sync-frontend] ${msg}\n`);
}

function fail(msg) {
  process.stderr.write(`[sync-frontend] ERROR: ${msg}\n`);
  process.exit(1);
}

/**
 * Inject the shim <script> tag immediately before the app's FIRST module.
 *
 * The requirement has not changed: the shim must set `window.WAVR_MOBILE` and
 * install its fetch/WebSocket routing before a single line of app code runs.
 * What changed is the page. It used to be one enormous inline <script>, and the
 * anchor was "the first <script> with neither type= nor src=". The dashboard has
 * since been split into 44 classic scripts under `js/`, loaded in a pinned order
 * that `test_shell_modules.py` holds; there is no app inline script left.
 *
 * That old regex did not FAIL on the new page. It matched — on the word
 * `<script>` inside an HTML comment 373kB in, where a note about the first-run
 * wizard describes it as having its "own <script>". The shim tag would have
 * gone into the middle of a comment: no error, no warning, a build that looks
 * finished, and an app that boots with no native bridge at all.
 *
 * So the anchor is now the thing it always meant: the first script the app
 * actually loads. Matched by the `js/` path rather than by a filename, so
 * reordering the modules cannot quietly move the shim after one of them.
 *
 * Idempotent via SHIM_MARKER.
 */
function injectShim(html) {
  if (html.includes(SHIM_MARKER) || html.includes(`src="${SHIM_FILENAME}"`)) {
    log('shim tag already present in index.html — leaving as-is (idempotent).');
    return html;
  }
  const appScriptRe = /<script[^>]*\bsrc\s*=\s*["']js\/[^"']+["'][^>]*>/i;
  const m = appScriptRe.exec(html);
  if (!m) {
    fail(
      'could not locate the app\'s first module (<script src="js/…">) in index.html ' +
        'to inject the shim before. The page structure changed — update the ' +
        'appScriptRe anchor in sync-frontend.mjs, and check the new one is not ' +
        'matching inside a comment, which is how this went wrong once already.'
    );
  }
  // Being inside a comment is the exact failure above, and it is cheap to rule
  // out: if the nearest `<!--` before the match is later than the nearest `-->`,
  // the match is commented out.
  const antes = html.slice(0, m.index);
  if (antes.lastIndexOf('<!--') > antes.lastIndexOf('-->')) {
    fail(
      `the anchor at byte ${m.index} is inside an HTML comment, so the shim would ` +
        'be injected into commented-out markup and would never run.'
    );
  }
  log(`injecting shim <script> before the app's first module at byte offset ${m.index}.`);
  return html.slice(0, m.index) + SHIM_TAG + html.slice(m.index);
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

  // 3b. Assets only the phone needs, from THIS branch, laid over the top.
  //
  // `jsqr.js` READS a QR code from a camera frame. Nothing on a desktop does
  // that, so it lives under `mobile/vendor/` — with the surface that needs it —
  // rather than in the dashboard's own vendor directory. It used to live in the
  // mobile BRANCH's copy of the frontend, and the moment the build was pointed
  // at the real dashboard it stopped being copied: the pairing scanner, whose
  // only job is reading a QR, could not load the thing that reads QRs.
  //
  // What made that expensive to see: the scanner loads the library and opens
  // the camera in ONE promise chain, so the failure surfaced as "Couldn't open
  // the camera. Check no other app is using it." The camera was never touched.
  // The message is fixed separately; this is the missing file.
  //
  // Overlaid AFTER the dashboard's own copy, so the phone can add files without
  // being able to silently replace one the dashboard ships.
  const localVendor = path.join(mobileDir, 'vendor');
  const PHONE_ONLY = ['jsqr.js'];
  for (const name of PHONE_ONLY) {
    const src = path.join(localVendor, name);
    const dest = path.join(wwwDir, 'vendor', name);
    if (!fs.existsSync(src)) {
      fail(
        `phone-only asset missing: ${src}. ${name} is what reads the pairing ` +
          'QR; without it the scanner fails and blames the camera.'
      );
    }
    if (fs.existsSync(dest)) {
      log(`NOTE: ${name} already came from the dashboard — not overlaying.`);
      continue;
    }
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.copyFileSync(src, dest);
    log(`copied phone-only vendor/${name} -> www/vendor/${name}`);
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
