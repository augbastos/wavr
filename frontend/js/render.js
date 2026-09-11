// ==========================================================================
// render.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ---- Map liveness poll (fake-presence-on-disconnect fix) ----
// Keep the house map's tri-state honest by refreshing per-camera liveness from
// /api/cameras. Live-only (loopback), same-origin, read-only — reads name/room/liveness
// ONLY (never a frame, rtsp_url or credential; ADR-0002 / zero egress). A camera that
// stops sending frames latches 'offline' server-side after cam_unhealthy_secs; this poll
// surfaces that so its room stops rendering a frozen/decayed reading as confident
// presence. Runs once now + every 5s. No-op in demo/companion (no cameras, no applier).
if(MODE === "live"){
  const pollMapCams = async ()=>{
    let cams; try{ cams = await (await fetch(location.origin + "/api/cameras")).json(); }
    catch{ return; }   // LAN may blip; the next tick re-syncs
    applyMapLiveness?.(cams);
  };
  pollMapCams();
  setInterval(pollMapCams, 5000);
}
if(!window.WAVR_MOBILE) initCompanion();   // mobile: deferred to the post-ready boot below
                                           // (companion pairing screen OR read-only viewer chrome)

// The engine's "why" sentence, with the sensors called what the rest of the
// product calls them. See the long note at the drill renderer for what this is
// repairing; the short version is that `mmwave`/`pir` are wire values and a
// household should never meet one.
//
// Deliberately conservative: if the sentence is not the shape this expects, or
// there is no source list to rebuild the names from, the engine's own words go
// through untouched. A cosmetic repair that can swallow an explanation is a bad
// trade on the panel somebody opens when they do not believe the answer.
function explanationInWords(explanation, sources){
  const text = String(explanation || "");
  const cut = text.indexOf(" → ");           // everything after it is arithmetic
  if(cut === -1 || !(sources && sources.length)) return text;
  const named = sources.map(s =>
    (modalityLabel(s.modality) || s.modality) + ": " +
    (s.presence ? WavrT("present") : WavrT("empty"))).join(" · ");
  return named + text.slice(cut);
}

// ---- Rendering (RoomState) ----
const roomsEl = document.getElementById("rooms");
const timelineEl = document.getElementById("timeline");
const cards = {};

// Ring geometry: r=44 in a 100-unit viewBox -> circumference for the dasharray math.
const RING_C = 2 * Math.PI * 44;

/* A room nothing is watching must not silently stop existing.
 *
 * The list is built from RoomState, so a room that has never produced a
 * reading has no card — and a household that drew Kitchen, Bedroom and Garage
 * and has one camera in the kitchen sees ONE room and no hint that the other
 * two are unwatched. Absence read as "fine", which is the failure this whole
 * product is built against.
 *
 * `mapRoomNames()` is what the floor plan knows (published by house3d.js,
 * which loads before this file). Each room without a card gets a muted row
 * saying plainly that nothing covers it; the row disappears the moment real
 * evidence arrives, because `upsert` creates a card and this removes the
 * placeholder for it.
 */
function syncUnwatchedRooms(){
  if(typeof mapRoomNames !== "function") return;
  let known;
  try{ known = mapRoomNames() || []; }catch(e){ return; }
  const want = new Set(known.filter(isRealRoom));

  // Retire a placeholder whose room started reporting.
  roomsEl.querySelectorAll("[data-unwatched]").forEach(el => {
    if(cards[el.dataset.unwatched] || !want.has(el.dataset.unwatched)) el.remove();
  });

  want.forEach(room => {
    if(cards[room]) return;
    if(roomsEl.querySelector('[data-unwatched="' + CSS.escape(room) + '"]')) return;
    roomsEl.querySelector(".empty")?.remove();
    const el = document.createElement("div");
    el.className = "card card-unwatched";
    // Muted and dashed, set here rather than in the shell's stylesheet so this
    // module carries its own appearance. An uncovered room is a gap to fill,
    // not a fault — and it must not look like a covered room that happens to
    // be empty, which is the one confusion that would make this worse than
    // showing nothing.
    //
    // "Muted" was `opacity: 0.68` on the whole card, and opacity is the wrong
    // tool for it: it fades the TEXT along with the border. `--dim` on
    // `--elevated`, both faded to 68% over the page behind them, measures
    // 3.60:1 — below the 4.5:1 WCAG AA floor, and the least legible sentence on
    // the Space screen. It was also the one sentence on the card that has
    // something to DO in it.
    //
    // The dashed border already carries "not covered", so the quiet comes from
    // a colour token instead: `--dim-deep`, the palette's "no answer yet" grey,
    // documented in index.html as sized to clear AA on the tight background —
    // which is this card's. 4.54:1 for the note, 13.37:1 for the room name in
    // --text above it. `test_module_copy_is_marked.py` computes both from the
    // tokens rather than trusting these numbers.
    el.style.borderStyle = "dashed";
    el.dataset.unwatched = room;
    el.dataset.room = room;              // cross-highlight works here too
    const h = document.createElement("h2");
    h.textContent = room;                // the household's own word, never translated
    const note = document.createElement("p");
    note.className = "panel-note";
    note.style.color = "var(--dim-deep)";
    note.textContent = WavrT("No sensor covers this room. Add a camera or a sensor node to cover it.");
    el.appendChild(h);
    el.appendChild(note);
    roomsEl.appendChild(el);
  });
}

function upsert(rs){
  // The whole-building placeholder is not a room, and this list is headed
  // "Status by room".
  //
  // `events.py` reports presence under `HOUSE_ROOM` whenever the evidence
  // cannot localise, which the network scan and BLE always do — and that is
  // the DEFAULT sensing level. So a fresh install's Space tab showed exactly
  // one card, titled with an internal Portuguese token for a room nobody has,
  // and none of the household's actual rooms. Three other modules already
  // filtered it out and this one did not; the constant lives in `shared.js`
  // now so a fifth reader cannot forget it.
  //
  // House-level presence is not lost by this: `whoshome.js` renders it as
  // "house-level signal only · no specific room confirmed", which is the
  // honest sentence for evidence that cannot say where.
  if(!isRealRoom(rs.room)) return;
  let c = cards[rs.room];
  if(!c){
    roomsEl.querySelector(".empty")?.remove();
    const el = document.createElement("div"); el.className="card";
    // Room name comes from RoomState (attacker-controllable via the camera "room" field) —
    // never interpolate it into innerHTML. The skeleton below is 100% static markup; every
    // dynamic string (room name, source labels, values) is set via textContent afterwards.
    el.dataset.room = rs.room;      // §6 cross-highlight anchor
    el.tabIndex = 0;                // keyboard: focus highlights, Enter selects (see below)
    el.insertAdjacentHTML("beforeend", `<div class="card-top">
        <div class="ring" role="img">
          <svg viewBox="0 0 100 100" aria-hidden="true" focusable="false">
            <circle class="ring-bg" cx="50" cy="50" r="44"></circle>
            <circle class="ring-fg" cx="50" cy="50" r="44"></circle>
          </svg>
          <span class="ring-pct"><b>—</b><small>confidence</small></span>
        </div>
        <div class="card-info">
          <h2></h2>
          <span class="pill"></span>
          <span class="pcount" hidden></span>
          <div class="bar"><i></i></div>
          <div class="precision" hidden role="img">
            <div class="precision-steps" aria-hidden="true">
              <i data-rung="house"></i><i data-rung="room"></i><i data-rung="count"></i><i data-rung="position"></i>
            </div>
            <span class="precision-label"><b class="precision-rung"></b><span class="precision-detail"></span></span>
            <span class="precision-next" hidden></span>
          </div>
        </div>
      </div>
      <!-- A contradiction between this room's sensors, said out loud.
           A confidence ring is a summary, and a summary of a disagreement is
           the one place a summary lies: "68%" over a radar saying somebody is
           there and a camera saying the room is empty hides the interesting
           part. Static markup; every string is set with textContent. -->
      <div class="room-dissent" hidden role="note">
        <span class="room-dissent-head"></span>
        <span class="room-dissent-who"></span>
      </div>
      <div class="srcs"></div>
      <details class="why"><summary>Why?</summary>
        <div class="drill"></div><div class="vit"></div>
      </details>`);
    el.querySelector("h2").textContent = rs.room;
    el.querySelector(".why > summary").textContent = WavrT("Why?");
    const fg0 = el.querySelector(".ring-fg");
    fg0.style.strokeDasharray = String(RING_C);
    fg0.style.strokeDashoffset = String(RING_C);   // starts empty -> first update draws in
    roomsEl.appendChild(el); c = cards[rs.room] = el;
    // A real room just landed, so the Space is no longer un-mapped. Cleared
    // here rather than on a timer: the stage must never say "no rooms yet"
    // across a room it is drawing at that moment.
    window.__wavrSpaceEmpty?.(false);
    syncUnwatchedRooms();     // this room's placeholder, if it had one, goes
    // Draw-in on first paint: force one layout so the transition below animates from empty.
    void el.offsetWidth;
  }
  // ---- Confidence ring (§6): write styles ONLY when the rounded % actually changed, so
  // the eased draw-in re-fires on real value changes and never on idle WS re-renders. ----
  const pct = Math.round(rs.confidence * 100);
  if(+c.dataset.pct !== pct){
    c.dataset.pct = pct;
    const fg = c.querySelector(".ring-fg");
    fg.style.strokeDashoffset = String(RING_C * (1 - pct/100));
    fg.style.stroke = colorFor(rs.confidence);              // consumer 1 of colorFor()
    c.querySelector(".ring-pct b").textContent = pct + "%";
    const bi = c.querySelector(".bar>i");
    bi.style.transform = `scaleX(${rs.confidence})`;
    bi.style.background = colorFor(rs.confidence);          // consumer 3: room-rail bar
  }
  // P4 layer 1: plain-language word under the % ("Confident"/"Likely"/"Uncertain";
  // empty rooms just read "empty"). textContent only — XSS-safe by construction.
  // `data-conf` stays the ENGLISH word: index.html selects on it
  // (`.ring-pct small[data-conf="Confident"]`), so only the visible text is translated.
  const ringKey = rs.occupied ? confWord(rs.confidence) : "empty";
  const ringWord = WavrT(ringKey);
  const ringWordEl = c.querySelector(".ring-pct small");
  if(ringWordEl.textContent !== ringWord){ ringWordEl.textContent = ringWord; ringWordEl.dataset.conf = ringKey; }
  c.querySelector(".ring").setAttribute("aria-label",
    WavrT("{word} — {pct}% confidence in {room}", {word: ringWord, pct: pct, room: rs.room}));
  const pill = c.querySelector(".pill");
  pill.textContent = rs.occupied ? WavrT("occupied") : WavrT("empty");
  pill.className = "pill " + (rs.occupied ? "on" : "off");
  c.classList.toggle("quiet", !rs.occupied);   // quiet state: unoccupied ring reads unlit
  // Person count (additive, honest): show "N people" only when a counting-capable
  // source (camera/mmwave) vouches for a number AND the room is occupied; otherwise
  // stay hidden so the room reads as just the presence dot -- never fabricate a 0.
  const pc = c.querySelector(".pcount");
  const pcn = rs.person_count;
  if(rs.occupied && typeof pcn === "number" && pcn > 0){
    pc.textContent = WavrT("{n} person|{n} people", {n: pcn});
    pc.hidden = false;
  } else {
    pc.textContent = "";
    pc.hidden = true;
  }
  // ---- Precision ladder (DISTINCT from confidence — PRECISION_RANK above, and
  // precisionWord()/precisionNextCopy() for the words it puts on screen). Pure
  // renderer of rs.precision_* — never re-derives a rung client-side, never claims a
  // precision the backend didn't emit. An older backend (fields absent) or a vacant room
  // (backend always emits "none" when !occupied) hides this: the three web modes stay
  // byte-identical pre-ladder. Gate the whole block on real value change (same discipline
  // as the confidence ring's strokeDashoffset) so idle WS re-renders don't rewrite the DOM.
  const prec = c.querySelector(".precision");
  const rung = rs.precision_level;
  const precKey = rung + "|" + rs.precision_pct + "|" + rs.precision_next;
  if(c.dataset.precKey !== precKey){
    c.dataset.precKey = precKey;
    if(!rung || rung === "none" || !PRECISION_RANK.hasOwnProperty(rung)){
      prec.hidden = true;
    } else {
      prec.hidden = false;
      const rank = PRECISION_RANK[rung];
      prec.querySelectorAll("[data-rung]").forEach(seg => {
        const segRank = PRECISION_RANK[seg.dataset.rung];
        seg.dataset.state = segRank < rank ? "done" : segRank === rank ? "active" : "locked";
      });
      const n = typeof pcn === "number" ? pcn : null;
      let label;
      if(rank === PRECISION_RANK.position){
        // Top rung: no new dots — this just names the ceiling the map's existing target
        // dots already reached (isPositioned/isEstimated, same POS_EST_MAX the map uses).
        const hasExact = (rs.targets || []).some(t => isPositioned(t) && !isEstimated(t));
        const plural = n !== 1;
        const who = (n != null) ? WavrT("{n} person|{n} people", {n: n}) : WavrT("People here");
        label = WavrT(hasExact
          ? (plural ? "{who}, exact positions shown on the map" : "{who}, exact position shown on the map")
          : (plural ? "{who}, approximate positions shown on the map" : "{who}, approximate position shown on the map"),
          {who: who});
      } else if(rank === PRECISION_RANK.count){
        label = n != null ? WavrT("{n} person|{n} people", {n: n}) : WavrT("Someone's here");
      } else if(rank === PRECISION_RANK.room){
        label = WavrT("Someone's here");
      } else {   // house
        label = WavrT("Someone is probably here");
      }
      const rungWord = precisionWord(rung);
      const rungWordEl = prec.querySelector(".precision-rung");
      rungWordEl.textContent = rungWord;
      prec.querySelector(".precision-detail").textContent = " · " + label;
      const nextEl = prec.querySelector(".precision-next");
      const nextCopy = precisionNextCopy(rs.precision_next);
      nextEl.textContent = nextCopy;
      nextEl.hidden = !nextCopy;
      prec.setAttribute("aria-label", WavrT("{word} detail — {label} in {room}",
        {word: rungWord, label: label, room: rs.room}));
    }
  }
  // ---- 3-state sensor-consensus rows (§6). States, driven from RoomState sources[]:
  //   absent  — modality seen in the house but not in THIS room's array: "no sensor here"
  //   disagree — present but voting against the fused verdict: warn-amber, NOT danger-red
  //   agree   — present and voting with the verdict: accent-green check
  // All rows textContent-built (XSS-safe); text labels always present (never icon-only).
  (rs.sources || []).forEach(s => { if(s && s.modality) SEEN_MODS.add(s.modality); });
  const present = new Map((rs.sources || []).map(s => [s.modality, s]));
  const absent = [...SEEN_MODS].filter(m => !present.has(m))
    .sort((a, b) => modalityLabel(a).localeCompare(modalityLabel(b)));
  // ---- Disagreement, above the source rows because it changes how they read ----
  //
  // The BACKEND decides whether these sensors contradict each other
  // (`wavr/disagreement.py`), and the same function answers for an AI agent over
  // MCP. A second implementation here would eventually differ from it, in front
  // of somebody with no way to tell which is right — and for a while the agent
  // was told and the household was not, which is the wrong way round.
  const dis = rs.disagreement;
  const disEl = c.querySelector(".room-dissent");
  if (disEl) {
    if (dis && dis.disagree) {
      disEl.hidden = false;
      // Says WHAT disagrees, not just that something does. "Sensors disagree"
      // alone is an anxiety with no action attached to it.
      //
      // Labelled by modality where that is enough to tell them apart — "radar
      // vs camera" is the sentence a person can act on. When two sensors share
      // a modality the label collapses to "node: occupied · node: empty", which
      // is worse than useless, so those fall back to the sensor's own name.
      // This is the household's own dashboard: their equipment names are the
      // right thing to show here, unlike the experience layer, which must never
      // see them.
      const mods = (dis.sensors || []).map(x => x.modality);
      const ambiguous = new Set(mods.filter((m, i) => mods.indexOf(m) !== i));
      const who = (dis.sensors || []).map(x => {
        const named = ambiguous.has(x.modality)
          ? (x.sensor_id || x.modality)
          : (modalityLabel(x.modality) || x.modality || x.sensor_id);
        return named + ": " + (x.says === "occupied" ? WavrT("occupied") : WavrT("empty"));
      });
      disEl.querySelector(".room-dissent-head").textContent =
        dis.counts_disagree ? WavrT("Sensors disagree, including on how many")
                            : WavrT("Sensors disagree");
      disEl.querySelector(".room-dissent-who").textContent = who.join(" · ");
      // The reason the room still has an answer travels with it, or the answer
      // reads as a mistake somebody should correct.
      disEl.title = dis.note ? WavrT(dis.note) : "";
    } else {
      disEl.hidden = true;
    }
  }

  const srcs = c.querySelector(".srcs");
  srcs.textContent = "";
  const mkRow = (modality, s) => {
    const row = document.createElement("div");
    const ic = document.createElement("span"); ic.className = "sic"; ic.setAttribute("aria-hidden", "true");
    const m = document.createElement("span"); m.className = "m";
    const bar = document.createElement("span"); bar.className = "sbar";
    const fill = document.createElement("i"); bar.appendChild(fill);
    const val = document.createElement("span"); val.className = "sval";
    const mIco = MODALITY_ICON[modality];   // from a controlled map (not network data)
    if(mIco){
      const mi = document.createElement("span"); mi.className = "msvg"; mi.setAttribute("aria-hidden", "true");
      mi.innerHTML = '<svg width="13" height="13"><use href="#' + mIco + '"/></svg>';
      m.appendChild(mi);
    }
    m.appendChild(document.createTextNode(modalityLabel(modality) || modality));
    if(!s){                                    // state 1: no coverage here
      row.className = "src absent";
      ic.textContent = "—";
      val.textContent = WavrT("no sensor here");
    } else {
      const unhealthy = s.health && s.health !== "fresh";
      if(unhealthy){                           // freshness dot, kept from the old rows
        const d = document.createElement("i"); d.className = "h " + s.health;
        d.title = s.health === "dead" ? WavrT("no signal") : WavrT("aging signal");
        m.prepend(d);
      }
      const agrees = !!s.presence === !!rs.occupied;
      fill.style.width = Math.round((s.confidence ?? 0) * 100) + "%";
      const age = (unhealthy && s.age_s != null) ? " · " + s.age_s + "s" : "";
      if(agrees){                              // state 3: agrees — green check
        row.className = "src agree" + (unhealthy ? " " + s.health : "");
        ic.innerHTML = '<svg width="12" height="12" aria-hidden="true"><use href="#ic-check"/></svg>';
        // P6 fix 5: the plain confidence word from the ring title rides along with the
        // exact per-sensor % — same reassurance at the detail level. textContent only.
        val.textContent = (s.presence ? WavrT("present") : WavrT("empty")) + " · " + Math.round((s.confidence ?? 0)*100) + "% (" +
          WavrT(confWord(s.confidence ?? 0)).toLowerCase() + ")" + age;
        fill.style.background = colorFor(s.confidence ?? 0);
      } else {                                 // state 2: active but disagreeing — warn amber
        row.className = "src disagree" + (unhealthy ? " " + s.health : "");
        ic.textContent = "—";
        val.textContent = WavrT("active, doesn't agree") + age;
      }
    }
    row.appendChild(ic); row.appendChild(m); row.appendChild(bar); row.appendChild(val);
    return row;
  };
  (rs.sources || []).forEach(s => srcs.appendChild(mkRow(s.modality, s)));
  absent.forEach(m => srcs.appendChild(mkRow(m, null)));
  // ---- "Why?" drill: fused-math explanation + one weight-disclosure line per source
  // ("Camera · weight 43% · agrees") — answers "why 92% and not 100%" (§6). Weight share
  // mirrors backend fusion.py DEFAULT_WEIGHTS over this room's present sources.
  //
  // `fusion.py` composes the explanation as
  //     mmwave: present · pir: empty → 63% occupied
  // naming each sensor by its WIRE value. Those wire values reached the
  // household verbatim, so one sensor stood on one card under three spellings
  // at once: `pir` on this line, "Motion sensor (PIR)" in the disagreement
  // banner above it, and "Motion Sensor (PIR)" in its own row between them.
  // Somebody reading that has to work out on their own that all three are the
  // same thing, on the panel whose entire job is to explain.
  //
  // The arithmetic stays the engine's: the fused percentage and the exit
  // countdown are its answers and nothing here recomputes them. Only the NAMES
  // are rebuilt, from the same `sources` array whose rows are drawn directly
  // below this line, through the same `modalityLabel` those rows call. That is
  // what makes it impossible for the line and the rows to disagree again — not
  // a second table that happens to match today.
  const drill = c.querySelector(".drill");
  drill.textContent = "";
  if(rs.explanation){
    const ex = document.createElement("div"); ex.className = "drill-expl";
    ex.textContent = explanationInWords(rs.explanation, rs.sources);
    drill.appendChild(ex);
  }
  (rs.sources || []).forEach(s => {
    const line = document.createElement("div"); line.className = "drill-line";
    const b = document.createElement("b"); b.textContent = modalityLabel(s.modality) || s.modality;
    line.appendChild(b);
    const verdict = (!!s.presence === !!rs.occupied) ? WavrT("agrees") : WavrT("doesn't agree");
    const healthNote = s.health === "dead" ? WavrT(" · no signal") : s.health === "stale" ? WavrT(" · signal aging") : "";
    // P6 fix 5: per-source confidence gets the same plain word as the title, beside the
    // exact % (weight % stays untouched — it is a share of the vote, not a confidence).
    const confNote = s.confidence != null
      ? " · " + WavrT(confWord(s.confidence)).toLowerCase() + " (" + Math.round(s.confidence * 100) + "%)"
      : "";
    // `share` is what this source actually contributed, decay and reliability
    // included. Absent on an older Core, and then it is left out rather than
    // guessed — a wrong percentage here is worse than no percentage, because
    // this is the panel somebody opens to check the arithmetic.
    const shareNote = typeof s.share === "number"
      ? WavrT(" · {pct}% of the vote", {pct: Math.round(s.share * 100)})
      : "";
    // Said when reliability actually moved the number. The backend publishes
    // it only then, for the same reason: a factor of 1.0 on every row trains a
    // reader to stop looking.
    const relNote = s.reliability != null
      ? WavrT(" · reliability {value}",
              {value: WavrFmt.number(s.reliability, {maximumFractionDigits: 2})})
        + (s.reliability_reason ? " (" + WavrT(s.reliability_reason) + ")" : "")
      : "";
    line.appendChild(document.createTextNode(
      shareNote + " · " + verdict + confNote + healthNote + relNote));
    drill.appendChild(line);
  });
  // Vitals and the reliability factor above are the numbers on this card that
  // actually carry a fraction, so they are the ones a raw `String(n)` renders
  // with an English decimal POINT inside a Portuguese sentence. Through WavrFmt
  // they get the reader's own separator, and the digit count stops depending on
  // whatever float the Core happened to send.
  const v = rs.vitals || {};
  const vit = [];
  if(v.breathing_bpm!=null) vit.push(WavrT("Breathing {bpm}/min",
    {bpm: WavrFmt.number(v.breathing_bpm, {maximumFractionDigits: 1})}));
  if(v.heart_bpm!=null) vit.push(WavrT("Heart rate {bpm} bpm",
    {bpm: WavrFmt.number(v.heart_bpm, {maximumFractionDigits: 0})}));
  c.querySelector(".vit").textContent = vit.join(" · ");
  c.dataset.occupied = rs.occupied ? "1" : "0";
  c.dataset.confidence = rs.confidence;
  reorderCards();
}

// Occupied rooms first (highest confidence first), empty rooms last — pure CSS `order`
// on the existing grid/flex .rooms, so cards animate into place via their transitions
// instead of the DOM being reshuffled.
function reorderCards(){
  Object.keys(cards)
    .map(room => {
      const el = cards[room];
      return {room, el, occ: el.dataset.occupied === "1", conf: parseFloat(el.dataset.confidence) || 0};
    })
    .sort((a, b) => {
      if(a.occ !== b.occ) return a.occ ? -1 : 1;
      if(a.occ) return b.conf - a.conf;
      return a.room.localeCompare(b.room);
    })
    .forEach((entry, i) => { entry.el.style.order = i; });
}

function pushTimeline(rs){
  timelineEl.querySelector(".empty")?.remove();
  const row = document.createElement("div"); row.className="row";
  // `rs.room` is HOUSE_ROOM ("casa") whenever the evidence is house-wide (the network
  // scan and BLE always report this way, and that is the DEFAULT sensing level) — every
  // OTHER per-room reader in this file already refuses to draw HOUSE_ROOM as a room
  // (see upsert() above), and History forgot to, so a fresh install's log read "casa ·
  // occupied (40%)" on every single line: the internal token, in Portuguese, on an
  // English screen. `dataset.room` stays unset for these rows (no real room to cross-
  // highlight or to add to the Room filter dropdown as a raw token); the visible room
  // cell gets the same honest "Whole Space" wording `space-admin.js`'s house_wide rows
  // and whoshome.js's sub already use for identical evidence.
  const realRoom = isRealRoom(rs.room);
  if(realRoom) row.dataset.room = rs.room;   // §6: hovering a timeline row highlights its room everywhere
  // Room name is attacker-controllable RoomState data — build every field with
  // createElement + textContent instead of innerHTML so it can never inject markup.
  //
  // `rs.ts` is ISO-8601 UTC (events.py). Slicing characters 11..19 out of it
  // printed the UTC wall clock with no offset applied and no way to tell —
  // somebody in Dublin in summer read every history row an hour early, and the
  // 24-hour clock was not theirs to choose either. WavrFmt parses the instant
  // and renders it in the reader's own zone and conventions.
  const t = document.createElement("span"); t.className="t";
  t.textContent = WavrFmt.time(rs.ts, {hour: "2-digit", minute: "2-digit", second: "2-digit"});
  const r = document.createElement("span"); r.className="r";
  r.textContent = realRoom ? rs.room : WavrT("Whole Space");
  const status = document.createElement("span");
  // TWO whole sentences, not one built from an English word and a bracket. The
  // adjective agrees with the room in Portuguese, so "occupied"/"empty" cannot
  // be swapped into a shared frame; and the percentage goes through WavrFmt.
  const tlPct = WavrFmt.number(rs.confidence, {style: "percent", maximumFractionDigits: 0});
  status.textContent = rs.occupied
    ? WavrT("occupied ({pct})", {pct: tlPct})
    : WavrT("empty ({pct})", {pct: tlPct});
  row.appendChild(t); row.appendChild(r); row.appendChild(status);
  // Stage 3b (§7#4): tag the row for the History client-side filters — the room rides
  // data-room (set above); the kind derives from the frame's modalities (network-only = "rede").
  const kmods = (rs.sources || []).map(s => s && s.modality).filter(Boolean);
  row.dataset.kind = (kmods.length && kmods.every(m => m === "network")) ? "rede" : "presenca";
  timelineEl.prepend(row);
  window.__wavrTlRow?.(row);   // Stage-3b hook: apply the active filter to the new row
  while(timelineEl.children.length>60) timelineEl.lastChild.remove();
}

// ---- Cross-highlighting (§6): hover/focus/select a room anywhere -> highlight it
// everywhere. `.linked` toggles on every DOM node sharing data-room (room cards, timeline
// rows, 2D map rects) AND highlightRoom3D() boosts the three.js room mesh + pulses its
// person marker. Hover is transient; click/Enter is a sticky selection (click again or
// select another room to clear).
let hoverRoomName = null, selectedRoomName = null;
function linkRoom(name, on){
  if(!name) return;
  document.querySelectorAll("[data-room]").forEach(n => {
    if(n.dataset.room === name) n.classList.toggle("linked", on);
  });
  try{ highlightRoom3D?.(name, on); }catch{}
}
function setHoverRoom(name){
  if(name === hoverRoomName) return;
  if(hoverRoomName && hoverRoomName !== selectedRoomName) linkRoom(hoverRoomName, false);
  hoverRoomName = name;
  if(hoverRoomName) linkRoom(hoverRoomName, true);
}
function setSelectedRoom(name){
  if(selectedRoomName){
    const prev = selectedRoomName;
    selectedRoomName = null;
    if(prev !== hoverRoomName) linkRoom(prev, false);
    if(prev === name) return;   // clicking the selected room again = deselect
  }
  selectedRoomName = name || null;
  if(selectedRoomName) linkRoom(selectedRoomName, true);
  dockSelectedRoom();
  // The structured spatial state carries which room is open, and selection
  // happens between frames — so it is marked now rather than waiting for the
  // next RoomState to rebuild the list.
  window.__wavrMarkSpatialSelection?.(selectedRoomName);
}

// SELECTING A ROOM SHOULD NOT COST YOU THE SPACE.
//
// The room card already carries the whole evidence view — per-sensor agreement,
// confidence, freshness, the "Why?" disclosure — and it is good. What it does
// badly is where it happens: the strip sits under the Space, so selecting a
// room scrolled the map off the top of the screen, and the column beside the
// map, which exists to qualify the Space, sat empty below the sensing control.
//
// So the card MOVES into that column. The node itself, not a copy: every
// re-render still writes to the same element wherever it currently lives, so
// there is still exactly one producer of a room's state and no second view to
// drift from the first. Its place in the strip is remembered and restored on
// deselect, so the strip's order survives the round trip.
let dockedCard = null, dockedAfter = null;
function dockSelectedRoom(){
  const side = document.querySelector(".space-stage .stage-side");
  if(!side) return;                    // another surface's composition: leave it alone
  if(dockedCard && dockedCard !== cards[selectedRoomName]){
    // Back where it came from, at the position it held.
    if(dockedAfter && dockedAfter.parentNode === roomsEl) roomsEl.insertBefore(dockedCard, dockedAfter);
    else roomsEl.appendChild(dockedCard);
    dockedCard.classList.remove("room-docked");
    dockedCard = dockedAfter = null;
  }
  const card = selectedRoomName ? cards[selectedRoomName] : null;
  if(!card || card === dockedCard) return;
  dockedAfter = card.nextElementSibling;
  dockedCard = card;
  card.classList.add("room-docked");
  side.prepend(card);   // first: it is the thing that was just asked for
}
// Hover (mouse) + focus (keyboard) delegation over every [data-room] node — including the
// 2D SVG rects (Element.closest/dataset work on SVG). Moving onto a non-room node clears.
document.addEventListener("mouseover", (e) => {
  const n = e.target && e.target.closest ? e.target.closest("[data-room]") : null;
  setHoverRoom(n ? n.dataset.room : null);
});
document.addEventListener("focusin", (e) => {
  const n = e.target && e.target.closest ? e.target.closest("[data-room]") : null;
  if(n) setHoverRoom(n.dataset.room);
});
document.addEventListener("focusout", (e) => {
  if(!(e.relatedTarget && e.relatedTarget.closest && e.relatedTarget.closest("[data-room]")))
    setHoverRoom(null);
});
// Room-rail card click/Enter -> sticky select + cross-highlight + focus its detail (§6).
// Clicks inside the "Why?" drill keep their native toggle behavior.
// Fix 2 (Core panel): on the panel form factor a room tap OPENS the collapsed map first, then
// focuses that room on it — otherwise the tap would select a display:none canvas (reads broken).
// Desktop keeps the plain sticky-select (the map is always visible there).
// "Am I the wall panel?" — asked the way the stylesheet now asks it.
//
// This was the same form-factor media query the CSS used, and it had the same
// bug for the same reason: it describes a SHAPE, and a maximised 1366x768
// laptop viewport (1366x628, ratio 2.18) and a phone held sideways (2.16) both
// have it. So a room tap in an ordinary browser window took the panel branch —
// expanding a card instead of selecting a room — while the surrounding CSS,
// once gated, was drawing the desktop composition around it. Two different
// answers to one question is worse than either answer.
//
// `data-core` is set in the head from `?core` / `window.WAVR_CORE`, which is
// what the launcher actually sends. The shape stays as a second condition for
// the same reason it does in the stylesheet: a panel that is somehow tall
// should not get a composition designed for a short one.
const PANEL_SHAPE_MQ = window.matchMedia("(orientation:landscape) and (max-height:820px) and (min-aspect-ratio:2/1)");
const HOME_PANEL_MQ = {
  get matches(){
    return document.documentElement.hasAttribute("data-core") && PANEL_SHAPE_MQ.matches;
  },
  addEventListener: (...a) => PANEL_SHAPE_MQ.addEventListener(...a),
  removeEventListener: (...a) => PANEL_SHAPE_MQ.removeEventListener(...a),
  addListener: (...a) => PANEL_SHAPE_MQ.addListener?.(...a),
};
function selectRoomFromRail(room){
  if(HOME_PANEL_MQ.matches){
    // On the panel a room tap EXPANDS that room's detection-methods breakdown (which sensors
    // are seeing it — camera / Bluetooth / Wi-Fi / network) inline, so the "why" of the
    // precision is clear right there. The house map stays reachable via its own "View house
    // map" bar; it no longer hijacks a room tap.
    const card = cards[room];
    if(card){
      const open = card.classList.toggle("srcs-open");
      card.setAttribute("aria-expanded", open ? "true" : "false");
      if(open) card.scrollIntoView({ behavior: REDUCED_MOTION.matches ? "auto" : "smooth", block: "nearest" });
    }
    setSelectedRoom(room);              // still cross-highlight the selection (no map auto-open)
    return;
  }
  setSelectedRoom(room);
}
roomsEl.addEventListener("click", (e) => {
  const card = e.target.closest ? e.target.closest(".card") : null;
  if(!card || e.target.closest("details")) return;
  selectRoomFromRail(card.dataset.room);
});
roomsEl.addEventListener("keydown", (e) => {
  if(e.key !== "Enter" && e.key !== " ") return;
  const card = e.target.closest ? e.target.closest(".card") : null;
  if(!card || e.target !== card) return;   // Enter inside <summary> keeps its own behavior
  e.preventDefault();
  selectRoomFromRail(card.dataset.room);
});
// 3D canvas -> DOM: the raycaster in ensure3D() calls this on a room-floor click/tap.
// Also scroll+focus the room's detail card so the selection lands somewhere actionable.
window.__wavrSelectRoom = (name) => {
  setSelectedRoom(name);
  const card = cards[name];
  if(card && selectedRoomName === name){
    card.scrollIntoView({ behavior: REDUCED_MOTION.matches ? "auto" : "smooth",
                          block: "nearest", inline: "nearest" });
    card.focus({ preventScroll: true });
  }
};

// Item 5: honest first-run empty state for live installs with no sensors reporting yet —
// replaces the forever-"Loading…"/"waiting for data" wording with something actionable,
// but only if no RoomState has actually arrived by then (real data always wins).
if(MODE==="live"){
  setTimeout(()=>{
    // Guard on what the list actually SHOWS, not on what arrived.
    //
    // `roomOcc` is keyed by every room a RoomState mentioned, and on a fresh
    // install the only sensor reports on the whole-building pseudo-room, which
    // `upsert` correctly refuses to draw as a card. So data "arrived", this
    // returned, and the panel sat on "Waiting for the Core's first reading…"
    // for ever — with the honest sentence right here, unreachable, on the
    // first screen of every new install.
    //
    // `cards` holds only rooms that are really drawn, and a room the floor plan
    // knows but nothing watches gets a `[data-unwatched]` row, which is also a
    // real answer. Either one means there is something to read.
    if(Object.keys(cards).length) return;
    if(roomsEl.querySelector("[data-unwatched]")) return;
    if(heroLine1El){ heroLine1El.textContent = WavrT("No sensor is sending data yet"); heroLine1El.className = "hero-line1"; }
    if(heroLine2El){ heroLine2El.textContent = WavrT("Add a camera in the Devices tab or run a network scan."); }
    const emptyEl = roomsEl.querySelector(".empty");
    if(emptyEl) emptyEl.textContent = WavrT("No sensor is sending data yet — add a camera in the Devices tab or run a network scan.");
    // And the Space itself says so, because the Space is what the reader is
    // looking at. Same decision, same moment, ONE producer: this only reveals a
    // composition already in the markup, and the guards above are the careful
    // part — they wait for a card that is really drawn, not for data to merely
    // have arrived.
    showSpaceEmpty(true);
  }, 6000);
}

// Drawn-nothing is a state of the Space, so it is drawn on the Space. Hidden
// again the moment a real room lands, from `upsert` — a stage that keeps saying
// "no rooms yet" over a room it is currently rendering would be the same class
// of lie this file's other guards exist to prevent.
function showSpaceEmpty(on){
  const el = document.getElementById("spaceEmpty");
  if(el) el.hidden = !on;
}
window.__wavrSpaceEmpty = showSpaceEmpty;

// The one action on that composition drives the navigation that already
// exists, by clicking the destination's own button. No second router, and no
// second opinion about which tab this is: if the destination moves or is
// renamed, this follows it, and if it is not in this build the button is
// simply not wired rather than throwing on a click.
document.getElementById("spaceEmptyGo")?.addEventListener("click", () => {
  document.querySelector('[data-tab="dispositivos"]')?.click();
});

const handle = (rs)=>{ setReconnecting(false); upsert(rs); pushTimeline(rs); updateHouse(rs); radarUpdate?.(rs); window.__wavrRS?.(rs); };

// The floor plan arrives asynchronously and rooms can be drawn at any time, so
// this runs on its own slow tick rather than once at boot. Cheap: a set
// comparison and, almost always, nothing to do.
setTimeout(syncUnwatchedRooms, 2500);
setInterval(syncUnwatchedRooms, 15000);
(async ()=>{
  if(window.WAVR_MOBILE){
    // BOOT GATE: token/base/house are read synchronously but the shim loads them from async
    // Keystore-backed storage. Wait for the caches, THEN build the provider and run the init we
    // deferred above (mode label, renderNetwork, renderRadar, initCompanion) in document order.
    // Race the gate against an 8s timeout so the page can NEVER hang even if a future shim breaks
    // the ready contract. In the happy path `ready` resolves in ms, so the timeout never fires.
    // This whole block is inside `if(window.WAVR_MOBILE)` — when the hook is ABSENT (loopback/
    // companion/demo web) not a byte of it executes, so the three web modes stay byte-identical.
    await Promise.race([window.WAVR_MOBILE.ready, new Promise(r=>setTimeout(r,8000))]);
    __wavrMakeProvider();
    // Fix F7: #mode removed (see the parse-time site above) -- nothing to re-run here now.
    renderNetwork(); renderRadar(); initCompanion();
    renderStatus();   // item 8: re-run now the token cache is populated (the parse-time call no-op'd in companion)
    renderControls(); // Fix D: re-run now companionToken() is populated -- the parse-time call short-circuited
                       // on MODE!=="live" before the Keystore cache landed, so companionIsCentral()'s GET
                       // /api/devices/me check never fired; this call is what actually kicks it off for a
                       // native-mobile admin companion (a browser companion already had the token synchronously
                       // at the first parse-time call, so this is a no-op re-check there).
    renderMobileBonded();   // item 7: admin device's bonded-Bluetooth import (no-op unless role central + plugin)
  }
  (await provider.history()).forEach(handle); provider.start(handle);
})();
