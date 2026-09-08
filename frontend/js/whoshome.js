/* Who's home — a calm glance answer to "who is here right now?".
 *
 * Lifted verbatim out of index.html. A classic script, NOT a module: it reads
 * `MODE`, `MIDDOT`, `roomOcc`, `roomConf` and `confWord` from the main
 * document's shared top-level scope, exactly as trust.js does, and must keep
 * executing in document order.
 *
 * ## Document order is load-bearing here
 *
 * This file chains onto `window.__wavrRS` by wrapping whatever handler is
 * already there. Several blocks do that, and each one calls the previous
 * before doing its own work — so WHERE the <script> tag sits decides the order
 * those renderers run in. Its tag therefore replaces the inline block at
 * exactly the position the block occupied. Moving it down beside the other
 * js/*.js tags would look tidier and would silently reorder the chain.
 *
 * It adds no fetch and no poll of its own: everything on this tab is built
 * from data the dashboard already holds.
 */
// ============================================================================
// Who's home (feature #2): a calm, at-a-glance answer to "who is home?" for a
// non-technical user. Purely additive, zero new fetches/polls of its own —
// every value below comes from data the dashboard ALREADY collects, via the
// SAME hook-chaining idiom the Stage-2/3 and Core-panel blocks use:
//   - roomOcc/roomConf: the dashboard's own top-level RoomState accumulator
//     (updateHouse() populates it inside handle(), BEFORE window.__wavrRS
//     fires — the same "readable from later script blocks" pattern the Core
//     panel's renderHero() already relies on for its own room summary).
//   - sensing on/off: chains onto window.__wavrSystem (sysInfo.running — the
//     SAME flag deriveTier() uses to render "Off" on the sensing-level meter)
//     for live mode, and window.__wavrStatus (status.features.hub_level) for
//     a companion viewer — the SAME field the sensing-level meter reads for a
//     paired device. Demo/simulated never claims "off" — it's illustrative.
//   - named + anonymous "home now": window.__wavrKnownPresence, one additive
//     hook added inside renderKnownPresence() (the "Who's likely home" tile)
//     that exposes its ALREADY-fetched GET /api/identity/known-presence
//     payload — live/central only, same gate as that tile. A corroborator
//     with person===null is an anonymously-registered ("yellow") device that
//     still counts as present without a name (wavr.identity_store's own
//     consent contract) — exactly the honest "+N other devices" this screen
//     asks for, never a guess.
// Nothing here ever asserts "nobody home" when sensing is off, and nothing
// here polls on its own — it re-renders whenever one of those three hooks
// fires, debounced 250ms so a burst of RoomState frames repaints once.
// ============================================================================
(function(){
  "use strict";
  var tile = document.getElementById("qcTile");
  if(!tile) return;   // additive: a stale cached page without this markup just no-ops
  var headline = document.getElementById("qcHeadline");
  var sub = document.getElementById("qcSub");
  var namesEl = document.getElementById("qcNames");
  var anonEl = document.getElementById("qcAnon");
  var note = document.getElementById("qcNote");
  // OPTIONAL. The per-room list this module used to own was dropped when Who's
  // home moved into Space: that list said "every room, occupied or empty",
  // which is the room column's whole job, and keeping both on one screen is
  // the card sprawl the reorganisation removed. The code below still knows how
  // to render it, so a surface that wants it back only has to provide the
  // element — but nothing here may assume it exists.
  var qcRoomsEl = document.getElementById("qcRooms");

  var haveFrame = false, qcSysInfo = null, qcStatusInfo = null;

  function sensingOff(){
    if(MODE === "live") return !!(qcSysInfo && qcSysInfo.running === false);
    if(MODE === "companion") return !!(qcStatusInfo && qcStatusInfo.features
      && qcStatusInfo.features.hub_level === "off");
    return false;   // demo/simulated: illustrative data, never claims sensing is off
  }

  function render(){
    if(!haveFrame){
      headline.className = "qc-headline"; headline.textContent = WavrT("Loading…");
      sub.textContent = ""; namesEl.textContent = ""; anonEl.textContent = "";
      if(qcRoomsEl) qcRoomsEl.textContent = ""; note.hidden = true;
      return;
    }
    if(sensingOff()){
      // Honesty gate: never imply "empty home" when Wavr simply isn't watching —
      // say so plainly instead, and show nothing else (any roomOcc value here would
      // be stale, from before sensing was paused).
      headline.className = "qc-headline off";
      headline.textContent = WavrT("Sensing is off");
      sub.textContent = WavrT("System paused — nothing is sensing. Turn it back on from the sensing-level meter on Space or Settings.");
      namesEl.textContent = ""; anonEl.textContent = "";
      if(qcRoomsEl) qcRoomsEl.textContent = ""; note.hidden = true;
      return;
    }

    var roomKeys = Object.keys(roomOcc);
    var home = roomKeys.some(function(r){ return roomOcc[r]; });               // matches updateHouse()'s own definition (casa counts)
    var realOcc = roomKeys.filter(function(r){ return isRealRoom(r) && roomOcc[r]; });
    headline.className = "qc-headline " + (home ? "home" : "away");
    headline.textContent = home ? WavrT("Someone is here") : WavrT("Nobody here");             // exact reuse of heroLine1's own wording
    if(realOcc.length){
      var maxConf = Math.max.apply(null, realOcc.map(function(r){ return roomConf[r] || 0; }));
      sub.textContent = realOcc.join(", ") + " " + MIDDOT + " " + confWord(maxConf).toLowerCase()
        + " " + MIDDOT + " " + Math.round(maxConf * 100) + "%";                // exact reuse of heroLine2's own format
    } else if(home){
      // Not "house-level": a Space is as often a shop or a clinic, and what
      // this describes is the whole building either way.
      sub.textContent = WavrT("Space-level signal only · no specific room confirmed");
    } else {
      // The SAME sentence `house-indicator.js` renders for the same state, so
      // it is spelt out whole rather than concatenated from parts. Built from
      // fragments it was a second producer of one line, and the two had
      // already drifted: that one goes through the catalogue, this one went
      // straight to the DOM in English.
      sub.textContent = WavrT("no presence detected · sensors monitoring");
    }

    // Named + anonymous "home now" — live/central only (same gate/data as "Who's likely home").
    namesEl.textContent = ""; anonEl.textContent = "";
    var kp = window.__wavrKnownPresence;
    if(MODE !== "live"){
      note.hidden = false;
      note.textContent = WavrT("Who's here by name needs the hub — not available in this demo or on a view-only companion device.");
    // An EMPTY corroborator list belongs in THIS branch, not the one below. It
    // means nobody has assigned a device to a person yet — a missing setup
    // step, not a statement about who is in the building. Falling through said
    // "No one detected right now." on a fresh install: an absence of people
    // asserted from an absence of assignments. renderKnownPresence(), reading
    // this same payload, has always treated `[]` as "nothing honest to show"
    // and hidden itself — the two consumers disagreed about what empty meant.
    } else if(!kp || !Array.isArray(kp.corroborators) || !kp.corroborators.length){
      note.hidden = false;
      note.textContent = WavrT("No devices are registered to a person yet — use “assign person” on a row in the Network tab to see names here.");
    } else {
      note.hidden = true;
      var present = kp.corroborators.filter(function(c){ return c && c.present; });
      var named = present.filter(function(c){ return c.person; });
      var anon = present.filter(function(c){ return !c.person; });
      named.forEach(function(c){
        var chip = document.createElement("span");
        chip.className = "who-chip";
        chip.textContent = c.person;   // PII — textContent only, never innerHTML
        namesEl.appendChild(chip);
      });
      if(anon.length){
        var achip = document.createElement("span");
        achip.className = "qc-anon-chip";
        // The plural was an English `+ "s"`, which is not how most languages
        // build one. The catalogue's own plural pipe instead.
        achip.textContent = WavrT("+ {n} other device|+ {n} other devices",
                                  {n: anon.length});
        anonEl.appendChild(achip);
      }
      if(!named.length && !anon.length){
        var none = document.createElement("span");
        none.className = "qc-empty-note";
        // Reachable only when devices ARE registered and none of them is
        // present, so the sentence can name what it is based on. "No one
        // detected" alone reads as a claim about the house; this reads as a
        // claim about the devices, which is the one Wavr can actually make.
        none.textContent = WavrT("None of the registered devices is here right now.");
        namesEl.appendChild(none);
      }
    }

    // Compact per-room lines — mode-agnostic, the SAME roomOcc/roomConf every
    // room card reads. Absent on Space, where the room column already answers
    // this; kept working for any surface that mounts #qcRooms.
    if(!qcRoomsEl) return;
    qcRoomsEl.textContent = "";
    var allRooms = roomKeys.filter(isRealRoom).sort();
    if(!allRooms.length){
      var l0 = document.createElement("div"); l0.className = "qc-room-row dim";
      // Honest, scope-aware: a network/BLE-only install can have real house-level frames
      // (haveFrame is already true here) with no per-room sensor yet — never worded as
      // "no data at all", which the house-level headline above would then contradict.
      l0.textContent = roomKeys.length
        ? "Only house-level presence is available — no per-room sensor has reported yet."
        : "No room data yet.";
      qcRoomsEl.appendChild(l0);
    } else {
      allRooms.forEach(function(r){
        var row = document.createElement("div"); row.className = "qc-room-row";
        var n = document.createElement("span"); n.className = "qc-room-name"; n.textContent = r;   // room name is RoomState data — textContent only
        var s = document.createElement("span");
        s.className = "qc-room-state " + (roomOcc[r] ? "occ" : "");
        s.textContent = roomOcc[r] ? "occupied" : "empty";
        row.appendChild(n); row.appendChild(s);
        qcRoomsEl.appendChild(row);
      });
    }
  }

  var renderTimer = null;
  function scheduleRender(){
    if(renderTimer) return;
    renderTimer = setTimeout(function(){ renderTimer = null; render(); }, 250);
  }

  // ---------- hooks fed by the main script (same one-liner chain idiom as every other block) ----------
  var _rs = window.__wavrRS;
  window.__wavrRS = function(rs){
    try{ if(_rs) _rs(rs); }catch(e){}
    haveFrame = true;
    scheduleRender();
  };
  var _sys = window.__wavrSystem;
  window.__wavrSystem = function(s){
    try{ if(_sys) _sys(s); }catch(e){}
    qcSysInfo = (s && typeof s === "object") ? s : null;
    scheduleRender();
  };
  var _stat = window.__wavrStatus;
  window.__wavrStatus = function(s){
    try{ if(_stat) _stat(s); }catch(e){}
    // See features.js: `unavailable` means the poll got no answer, and an
    // empty payload would read as "nothing is on".
    qcStatusInfo = (s && typeof s === "object" && !s.unavailable) ? s : null;
    scheduleRender();
  };
  render();   // first paint: "Loading…" until the first RoomState frame lands
})();
