// ==========================================================================
// housemap.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ---- House map (v2: floors -> room polygons + walls + features; meters throughout) ----
let HOUSE = { version: 2, units: "m", floors: [] };
let currentFloor = 0;
// Built-in default (mirrors backend housemap.DEFAULT_MAP exactly) — the public demo renders
// this client-side with NO backend call; it also doubles as the live/companion fetch fallback.
const DEMO_HOUSE = {
  version: 2, units: "m",
  floors: [{
    id: "f0", name: "Ground floor", level: 0,
    rooms: [
      {id:"r_sala",    name:"living room", polygon:[[0.0,0.0],[4.0,0.0],[4.0,3.0],[0.0,3.0]]},
      {id:"r_quarto",  name:"bedroom",     polygon:[[4.2,0.0],[7.7,0.0],[7.7,3.0],[4.2,3.0]]},
      {id:"r_quintal", name:"backyard",    polygon:[[0.0,3.2],[7.7,3.2],[7.7,5.7],[0.0,5.7]]},
    ],
    walls: [], features: [], backdrop: null,
  }],
};
// Whether the map on screen came from a Core, or from the sample below.
//
// The sample house is the right answer for the public demo, which never calls a
// backend at all. It is the wrong answer for somebody's home: it invents a
// living room, a bedroom and a backyard, and the dashboard draws them as that
// person's Space with "No sensor covers this room" under each -- advice about
// rooms that do not exist.
//
// That is what the first user was shown. The desktop window had been pointed at
// the wrong scheme, so every request from the page failed; the service worker
// answered the navigation out of its offline cache, so a full dashboard drew;
// and the line below filled the empty map with the sample. His Space has ONE
// room and is called "My Home". The screen said "Command Center", listed three
// rooms he does not have, and put a small "reconnecting" chip in the corner as
// the only clue that any of it was wrong.
//
// So the substitution is gated on whether a Core was ASKED and failed to
// answer, rather than on whether the map happens to be empty. Asked and silent
// leaves the map empty, and the surfaces that draw it say they have nothing --
// which is true, and is the one thing the sample house can never say.
let HOUSE_FROM_CORE = false;

async function loadHouse(){
  if(MODE === "live"){
    HOUSE_FROM_CORE = false;
    try{
      const r = await fetch(location.origin+"/api/house");
      if(r.ok){ HOUSE = await r.json(); HOUSE_FROM_CORE = true; }
    }catch{}
    if(!HOUSE_FROM_CORE){
      // Whatever an earlier successful load left behind is kept: a stale map is
      // still this person's map, and every card that draws it already carries
      // the "no reading has arrived for a while" warning. Never the sample.
      if(!HOUSE || !Array.isArray(HOUSE.floors) || !HOUSE.floors.length){
        HOUSE = { version: 2, units: "m", floors: [] };
      }
      currentFloor = HOUSE.floors.length ? HOUSE.floors[0].level : 0;
      return;
    }
  } else if(MODE === "companion" && companionToken()){        // companion viewer: real house map, Bearer-authed
    let respondeu = false;
    try{
      // Mobile: route to the stored central via the native pinned fetch so the Bearer token never
      // reaches the app's own https://localhost. Absent the hook, the original same-origin fetch runs.
      const r = await (window.WAVR_MOBILE
        ? window.WAVR_MOBILE.netFetch(window.WAVR_MOBILE.base+"/api/house", {headers:{"Authorization":"Bearer "+companionToken()}})
        : fetch(location.origin+"/api/house", {headers:{"Authorization":"Bearer "+companionToken()}}));
      if(r.status===401||r.status===403){ companionAuthFailed(); }
      else if(r.ok){ HOUSE = await r.json(); respondeu = true; HOUSE_FROM_CORE = true; }
    }catch{}
    if(!respondeu){
      // A PAIRED companion is looking at a real home too. Same rule.
      if(!HOUSE || !Array.isArray(HOUSE.floors) || !HOUSE.floors.length){
        HOUSE = { version: 2, units: "m", floors: [] };
      }
      currentFloor = HOUSE.floors.length ? HOUSE.floors[0].level : 0;
      return;
    }
  }
  // simulated (public demo) and unpaired companion: no backend call, ever — built-in default.
  if(!HOUSE || !Array.isArray(HOUSE.floors) || !HOUSE.floors.length) HOUSE = DEMO_HOUSE;
  currentFloor = HOUSE.floors[0].level;
}

// ---- Zones (client-side, persisted to localStorage; works in live AND demo) ----
// A zone is a named rectangle in radar user-space (metres, same coords as rooms/targets).
const ZONE_KEY = "wavr.zones.v1";
const isZone = (z) => z && typeof z.name==="string" &&
  ["x","y","w","h"].every(k => typeof z[k]==="number" && isFinite(z[k]));
function loadZones(){
  try{ const z = JSON.parse(localStorage.getItem(ZONE_KEY)); return Array.isArray(z) ? z.filter(isZone) : []; }
  catch{ return []; }
}
function saveZones(zones){ try{ localStorage.setItem(ZONE_KEY, JSON.stringify(zones)); }catch{} }
let zones = loadZones();

// A camera Target carries a POSITION-quality confidence when it is positioned (x/y set):
// ~0.85 for an accurate 4-point homography, ~0.45 for the approximate monocular prior
// (backend localize.Q_HOMOGRAPHY / Q_MONOCULAR). This threshold splits "calibrated" from
// "estimated" so the map can render an approximate spot honestly (hazy). An UNpositioned
// target (x/y null -> room-centred) is neither: its confidence is a detection score, not a
// position score, so it is never styled as estimated here.
const POS_EST_MAX = 0.6;
const isPositioned = (t) => t && t.x != null && t.y != null;
const isEstimated  = (t) => isPositioned(t) && (t.confidence ?? 0) < POS_EST_MAX;

function dot(room, t){
  const g = document.createElementNS(SVG_NS, "g");
  g.setAttribute("class", "radar-target");
  const c = document.createElementNS(SVG_NS, "circle");
  c.setAttribute("class", "radar-dot");
  c.setAttribute("cx", 0); c.setAttribute("cy", 0); c.setAttribute("r", 0.12);
  c.appendChild(document.createElementNS(SVG_NS, "title"));
  const txt = document.createElementNS(SVG_NS, "text");
  txt.setAttribute("class", "radar-posture");
  g.appendChild(c); g.appendChild(txt);
  placeDot(g, room, t);   // position set before insertion -> no fly-in on first paint
  return g;
}

function placeDot(g, room, t){
  const r = 0.12;
  // An UNPOSITIONED target has no x/y. It used to be drawn at the room's centre,
  // as a crisp dot indistinguishable from one placed by a four-point homography
  // -- the code's own comment said "crisp = calibrated or room-centred". That is
  // a fabricated position: the honest statement is "someone is in this room",
  // and the middle of the room is not where they are.
  //
  // The centre is still where the marker is anchored, because there is nowhere
  // else to anchor it. What changes is that it stops claiming to be a point: the
  // class below styles it as a diffuse region covering the room.
  const unpositioned = !isPositioned(t);
  g.classList.toggle("unpositioned", unpositioned);
  let x = t.x != null ? room.x + t.x : room.x + room.w/2;
  let y = t.y != null ? room.y + t.y : room.y + room.h/2;
  x = Math.min(Math.max(x, room.x + r), room.x + room.w - r);
  y = Math.min(Math.max(y, room.y + r), room.y + room.h - r);
  // CSS px == SVG user units here; style.transform (not the attribute) so the
  // .radar-target transition animates movement between updates.
  g.style.transform = `translate(${x}px, ${y}px)`;
  // Hazy marker = an ESTIMATED (monocular) spot; crisp = calibrated or room-centred.
  const est = isEstimated(t);
  g.classList.toggle("est", est);
  // The map's hover text. Two WHOLE sentences rather than one plus an optional
  // English tail: a template literal is invisible to anything scanning for
  // `WavrT(`, and gluing a translated fragment onto the end of another one is
  // how a Portuguese sentence ends up disagreeing with itself. The percentage
  // goes through WavrFmt so the reader's own conventions apply to it.
  const conf = WavrFmt.number(t.confidence ?? 0, {style: "percent", maximumFractionDigits: 0});
  g.querySelector("title").textContent = est
    ? WavrT("Person {id} · confidence {pct} · estimated position", {id: t.id ?? "?", pct: conf})
    : WavrT("Person {id} · confidence {pct}", {id: t.id ?? "?", pct: conf});
  const txt = g.querySelector("text");
  // Compensate the viewBox's user-space scaling so the caption reads at a real ~10px
  // regardless of room/viewport size (see radarPxPerM comment above).
  txt.style.fontSize = (10 / radarPxPerM) + "px";
  const label = postureLabel(t.posture);
  if(label){
    // Flip the caption above the dot when it sits in the lower half of the room, so the
    // baseline never lands on (or past) the viewBox's bottom edge and clips descenders.
    const ly = (y - room.y) > room.h/2 ? -0.24 : 0.32;
    // text-anchor is "middle"; clamp the label's x independently of the dot's so a wide
    // word (e.g. "walking") can't overflow the room/viewBox on either side near a corner.
    const half = label.length*0.085 + 0.05;
    const lo = room.x + half, hi = room.x + room.w - half;
    const lx = lo <= hi ? Math.min(Math.max(x, lo), hi) : room.x + room.w/2;
    txt.setAttribute("x", lx - x); txt.setAttribute("y", ly);   // coords relative to the g
    txt.textContent = label;
  } else {
    txt.textContent = "";
  }
}

