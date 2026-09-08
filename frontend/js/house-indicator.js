// ==========================================================================
// house-indicator.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ---- House indicator (home/away) — works in both live and demo modes ----
const houseEl = document.getElementById("house");
const heroLine1El = document.getElementById("heroLine1");
const heroLine2El = document.getElementById("heroLine2");
const heroWhoHomeEl = document.getElementById("heroWhoHome");
const roomOcc = {};
const roomConf = {};
// `confWord()` (js/shared.js) also feeds a CSS attribute selector
// (`.ring-pct small[data-conf="Confident"]`), so the value it returns has to
// stay the English key or the panel's confidence dot loses its style. The word
// a person READS is translated here instead — one literal per rung, and a value
// this build has never heard of falls through unchanged.
function confLabel(word){
  return word === "Confident" ? WavrT("Confident")
       : word === "Likely" ? WavrT("Likely")
       : word === "Uncertain" ? WavrT("Uncertain")
       : word;
}
// Neutral first-paint state (no blank flash) before the first RoomState arrives.
houseEl.textContent = WavrT("Space: —"); houseEl.className = "house";
if(heroLine1El){ heroLine1El.textContent = WavrT("Loading…"); heroLine1El.className = "hero-line1"; }
// "Who is home" roster (non-biometric, house-level). Driven ONLY by the 'casa'
// pseudo-room's identities — the sole place the backend attaches them, and only when
// identity is explicitly enabled (default OFF). Empty/absent -> hidden, no empty widget.
// Person names are PII: rendered via textContent ONLY (never innerHTML). Honest wording:
// "Home now" = house-level presence of a named device, NOT a per-room or biometric claim.
let whoHomeSig = null;   // last rendered roster signature — skip rebuild + aria re-announce when unchanged
function renderWhoHome(identities){
  const el = heroWhoHomeEl;
  if(!el) return;
  const list = Array.isArray(identities) ? identities : [];
  // Signature over (person, source) pairs; casa arrives every scan, but the roster rarely
  // changes — avoid churning the DOM (and re-triggering aria-live) on identical frames.
  const sig = list.map(function(id){ return (id && id.person) + "" + (id && id.source); }).join("");
  if(sig === whoHomeSig) return;
  whoHomeSig = sig;
  el.textContent = "";
  if(!list.length){ el.hidden = true; return; }
  const label = document.createElement("span");
  label.className = "who-label";
  label.textContent = WavrT("Here now:");
  el.appendChild(label);
  list.forEach(function(id){
    const person = id && id.person;
    if(typeof person !== "string" || !person) return;   // skip malformed entries
    const chip = document.createElement("span");
    chip.className = "who-chip";
    chip.textContent = person;                          // XSS-safe: never innerHTML
    const src = id && id.source;
    // Coarse proximity only — a single adapter localizes to the house, never a room.
    if(src === "ble" || src === "network"){
      chip.title = src === "ble" ? WavrT("seen over Bluetooth") : WavrT("seen on the network");
    }
    el.appendChild(chip);
  });
  el.hidden = el.childElementCount <= 1;   // label only (all entries malformed) -> hide
}
function updateHouse(rs){
  roomOcc[rs.room] = !!rs.occupied;
  roomConf[rs.room] = rs.confidence;
  // Identity rides ONLY on the house-level 'casa' room; updating from any other room would
  // wrongly clear the roster (their identities are always []). Gate strictly on 'casa'.
  if(rs.room === "casa") renderWhoHome(rs.identities);
  const home = Object.values(roomOcc).some(Boolean);
  // "Space: empty" read as "not saved" / "no data" — the same ambiguity the
  // hero line two rows down was already written to avoid ("Nobody here", never
  // "empty"). One word, two meanings, on the one pill that answers "is anyone
  // home" — say what it means instead.
  houseEl.textContent = home ? WavrT("Space: someone's in") : WavrT("Space: nobody home");
  houseEl.className = "house " + (home ? "home" : "away");
  // First frame arrived: reveal the heartbeat badge (§2.1 — it keeps ticking in the
  // quiet state so "calm" never reads as "broken"). Label is honest per MODE.
  const liveB = document.getElementById("heroLive");
  if(liveB && liveB.hidden){
    liveB.hidden = false;
    const lt = document.getElementById("heroLiveTxt");
    if(lt) lt.textContent = (typeof MODE!=="undefined" && MODE==="simulated") ? WavrT("demo") : WavrT("live");
  }
  // Above-the-fold hero: same RoomState-derived occupancy the pill uses, formatted as
  // the glanceable first answer a phone companion needs (line 1: home/away; line 2:
  // which room(s) + the highest confidence among them).
  if(heroLine1El){
    heroLine1El.textContent = home ? WavrT("Someone's here") : WavrT("Nobody here");
    heroLine1El.className = "hero-line1 " + (home ? "home" : "away");
  }
  if(heroLine2El){
    // `casa`/HOUSE_ROOM is the whole-Space pseudo-room (see shared.js), never a room a
    // household has — this subtitle was the one reader that forgot to filter it, so a
    // fresh install's DEFAULT sensing (network/BLE, house-level only) printed the raw
    // Portuguese token right under "Someone's here" on an English screen.
    const occRooms = Object.keys(roomOcc).filter(r => isRealRoom(r) && roomOcc[r]);
    if(occRooms.length){
      const maxConf = Math.max(...occRooms.map(r => roomConf[r] ?? 0));
      // P4: layer-1 word first, exact % kept right beside it (progressive disclosure)
      heroLine2El.textContent = occRooms.join(", ") + " · " +
        confLabel(confWord(maxConf)).toLowerCase() + " · " + Math.round(maxConf*100) + "%";
    } else if(home){
      // House-level evidence only (network/BLE reporting under HOUSE_ROOM): say so in
      // words rather than the raw token — the SAME sentence whoshome.js's qcTile uses
      // for the identical signal, so the two can never disagree.
      heroLine2El.textContent = WavrT("Space-level signal only · no specific room confirmed");
    } else {
      // Quiet state (§2.1): calm, reassuring copy — the sensors are watching, nothing moves.
      heroLine2El.textContent = WavrT("no presence detected · sensors monitoring");
    }
  }
  // Off-screen map summary (§7#12): what a sighted user reads off the canvas, as text.
  // Not aria-live (frames arrive every ~1.5s — announcing each would be noise); a screen
  // reader gets it on demand via the canvas/svg aria-describedby.
  const srEl = document.getElementById("mapSrSummary");
  if(srEl){
    // Built from mapState(), which is the one function that knows all five
    // states the canvas paints. This used to read `roomOcc` alone, so the
    // amber (a camera is down), the blue (a camera is covered) and the slate
    // (nothing is watching) rooms were all announced under "Empty" — and a
    // blind room was not announced at all.
    //
    // The worst case was the quiet one: every camera offline and nothing
    // occupied produced "Nobody here — no presence detected", which told a
    // screen-reader user the Space was confirmed empty at the exact moment
    // nothing could see. The canvas said amber; the text said verified.
    //
    // Same rule as everywhere else in this product: the map is not allowed to
    // be more certain in words than it is in colour.
    // Asked of the map, not re-derived. `mapStateOf` is null until the map
    // module has initialised, and on a Core with no floor plan it never does —
    // so fall back to the presence-only view rather than throwing, which would
    // take the summary away entirely and leave no textual alternative at all.
    const byState = {};
    if (mapStateOf && mapRoomNames) {
      mapRoomNames().forEach(r => {
        const st = mapStateOf(r);
        (byState[st] = byState[st] || []).push(r);
      });
    } else {
      Object.keys(roomOcc).filter(isRealRoom).forEach(r => {
        const st = roomOcc[r] ? "occupied" : "empty";
        (byState[st] = byState[st] || []).push(r);
      });
    }
    const occ = (byState.occupied || [])
      .map(r => r + " (" + confLabel(confWord(roomConf[r] ?? 0)).toLowerCase() + ", " + Math.round((roomConf[r] ?? 0)*100) + "%)");
    // Built as whole clauses with a {rooms} slot rather than a label glued to
    // a list: "Occupied:" and the names are one sentence, and a language that
    // puts the verb elsewhere needs to move the label, not receive it
    // pre-attached. The room names themselves are whatever this Space called them
    // and are never translated.
    const parts = [];
    if(occ.length) parts.push(WavrT("Occupied: {rooms}", {rooms: occ.join(", ")}));
    if(byState.empty?.length)
      parts.push(WavrT("Empty: {rooms}", {rooms: byState.empty.join(", ")}));
    // Each not-knowing said in its own words, because the errands differ: a
    // camera that is down is a fault, a covered one is a choice, and a room
    // with nothing in it is a gap in coverage.
    if(byState.offline?.length)
      parts.push(WavrT("Cannot see, camera down: {rooms}", {rooms: byState.offline.join(", ")}));
    if(byState.privacy?.length)
      parts.push(WavrT("Camera deliberately covered: {rooms}", {rooms: byState.privacy.join(", ")}));
    if(byState.unknown?.length)
      parts.push(WavrT("Camera not running: {rooms}", {rooms: byState.unknown.join(", ")}));
    if(byState.blind?.length)
      parts.push(WavrT("No sensor covers: {rooms}", {rooms: byState.blind.join(", ")}));
    const txt = parts.length
      ? parts.join(". ") + "."
      : WavrT("No rooms to report yet.");
    if(srEl.textContent !== txt) srEl.textContent = txt;
  }
}

