// ==========================================================================
// shared.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ---- Shared date/time formatting helpers (device first/last-seen, internet-down "since") ----
function fmtShortDate(iso){
  if(typeof iso!=="string" || !iso) return null;
  const d = new Date(iso);
  if(isNaN(d.getTime())) return null;
  return WavrFmt.date(d);
}
function fmtRelative(iso){
  if(typeof iso!=="string" || !iso) return null;
  const t = new Date(iso).getTime();
  if(isNaN(t)) return null;
  const diffS = Math.max(0, Math.floor((Date.now()-t)/1000));
  if(diffS < 60) return "now";
  if(diffS < 3600) return `${Math.floor(diffS/60)}min ago`;
  if(diffS < 86400) return `${Math.floor(diffS/3600)}h ago`;
  return `${Math.floor(diffS/86400)}d ago`;
}
function fmtDateTime(iso){
  if(typeof iso!=="string" || !iso) return null;
  const d = new Date(iso);
  if(isNaN(d.getTime())) return null;
  return WavrFmt.dateTime(d);
}

// ---- P4 (progressive disclosure, layer 1): plain-language word for a confidence value.
// Shown BESIDE the exact % (layer 2) — never instead of it. Consumers: Home hero,
// each room card's ring caption (upsert) and the off-screen map summary. Always applied
// via textContent, never innerHTML, so it is XSS-inert by construction.
function confWord(conf){
  const pct = Math.round((+conf || 0) * 100);
  if(pct >= 80) return "Confident";
  if(pct >= 50) return "Likely";
  return "Uncertain";
}


/* The HOUSE-LEVEL pseudo-room, named once.
 *
 * `events.py` emits presence for the whole building under this name when the
 * evidence cannot localise — the network scan and BLE always do. It is an
 * internal token, not a room anybody drew, and it is Portuguese in an
 * otherwise-English product because it predates the rest.
 *
 * Four modules each hardcoded the literal `"casa"` to filter it out, and a
 * fifth — `render.js`, the rooms list — did not, so a fresh install's Space tab
 * showed exactly one card titled "Casa" and none of the household's actual
 * rooms. One constant, so a sixth reader cannot forget.
 *
 * NOT translated: it is a wire value, matched against what the Core sends.
 */
const HOUSE_ROOM = "casa";

/* Is this a room somebody named, or the whole-building placeholder? */
function isRealRoom(name) {
  return !!name && name !== HOUSE_ROOM;
}
