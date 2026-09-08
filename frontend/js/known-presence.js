// ==========================================================================
// known-presence.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ---- Who's likely here — known-presence summary (live/central only, known-device-ui,
// 2026-07-11) ----
// Pure read of GET /api/identity/known-presence: a house-level "likely home" summary
// composed from devices already ASSIGNED to a person on the Network/Devices list (see
// renderNetwork()'s "assign person" control) + the already-fused network signal — no new
// scan, no re-fusion, nothing computed client-side. The endpoint deliberately never returns
// a full MAC (mac_prefix only, first 3 octets) so this stays a house-level summary, never a
// per-device drill-down; scope is always "house" and confidence_label always "coarse" — this
// UI never claims tighter than that. A corroborator's richer `details` block (first/last
// seen, device type) only appears when that specific device separately opted into consent #2
// (identity_store's `details` flag, toggled from the "My devices" panel above). Same
// live-only placement as renderIdentity() (not gated behind window.WAVR_MOBILE — Mobile's
// own native-pinned fetch reaches MODE==="live" the same way). `person` is PII — every name
// is rendered via textContent, NEVER innerHTML.
async function renderKnownPresence(){
  if(MODE!=="live") return;                  // live-only; never in companion/demo
  const tile = document.getElementById("whoHome");
  const list = document.getElementById("whoHomeList");
  /* Two consecutive misses, then retract. Never keep a verdict.
   *
   * This tile names people and says "here" or "away" beside each of them. That
   * is the most specific claim on the landing screen, so it is the one that
   * does most damage left standing over a Core that stopped answering: a tag
   * frozen at "here" is not a stale reading, it is an assertion about who is in
   * the building right now.
   *
   * The deadline is the other half. A refused request already landed in the
   * catch below; a host that goes DARK never settles the request at all, so
   * `refresh` never reached the catch either and the tags simply froze.
   *
   * Two misses, matching `house-status.js` and the transparency screen, because
   * one dropped request at a 15-second poll is not evidence of anything.
   */
  let misses = 0;
  function cannotTell(){
    misses += 1;
    if(misses < 2) return;                    // one dropped request proves nothing
    // The sibling Who's-home tab reads this hook; null is its already-defined
    // word for "genuinely unreachable", so it stops asserting at the same
    // moment this tile does rather than one poll later.
    window.__wavrKnownPresence = null;
    // Only retract a claim that was actually made. This tile stays hidden until
    // somebody has assigned a device to a person, and putting a "not answering"
    // notice into a tile that has never been on screen would invent a surface
    // on a fresh install instead of correcting one.
    if(tile.hidden) return;
    list.textContent = "";
    const p = document.createElement("p");
    p.className = "empty";
    p.textContent = WavrT("Wavr is not answering, so this cannot be checked. Check that the Core is running.");
    list.appendChild(p);
  }

  async function refresh(){
    let r;
    try{ r = await WavrAPI.fetch("/api/identity/known-presence", {timeoutMs: 8000}); }
    catch{ cannotTell(); return; }            // no answer, including a host that never replies
    misses = 0;                               // the Core answered — whatever it said, it is answering
    // Who's-home tab hook (feature #2, additive): expose this SAME already-fetched payload so
    // that tab never re-fetches known-presence itself. null = "genuinely unreachable" (403/
    // parse failure) so the other tab can say so honestly, distinct from a real empty array.
    if(!r.ok){ tile.hidden = true; window.__wavrKnownPresence = null; return; }  // 403 (multidevice 'user') / feature off — stay hidden
    let kp; try{ kp = await r.json(); }catch{ tile.hidden = true; window.__wavrKnownPresence = null; return; }
    window.__wavrKnownPresence = kp;
    const corroborators = Array.isArray(kp && kp.corroborators) ? kp.corroborators : [];
    if(!corroborators.length){ tile.hidden = true; return; }   // nobody assigned yet — nothing honest to show
    tile.hidden = false;
    list.textContent = "";
    corroborators.forEach(c => {
      const row = document.createElement("div"); row.className = "pair-dev-row";
      const left = document.createElement("span");
      const name = document.createElement("b"); name.textContent = c.person || WavrT("(unnamed)"); // textContent — PII
      const meta = document.createElement("span"); meta.className = "pair-dev-meta";
      let metaTxt = " · " + (c.mac_prefix || "") + " · " + WavrT("Space-level, coarse");
      if(c.details){                          // consent #2 — richer already-collected info
        const seenRel = fmtRelative(c.details.last_seen);
        if(seenRel) metaTxt += " · " + WavrT("last seen {when}", {when: seenRel});
        if(c.details.device_type) metaTxt += " · " + c.details.device_type;
      }
      meta.textContent = metaTxt;             // textContent — server data
      left.appendChild(name); left.appendChild(meta);
      row.appendChild(left);
      const tag = document.createElement("span");
      tag.className = "tag" + (c.present ? " known" : "");
      tag.textContent = c.present ? WavrT("here") : WavrT("away");
      row.appendChild(tag);
      list.appendChild(row);
    });
  }
  await refresh();
  setInterval(refresh, 15000);
}
renderKnownPresence();

