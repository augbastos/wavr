// ==========================================================================
// radar.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ---- Radar (top-down house map + live/demo targets; works in both modes) ----
const SVG_NS = "http://www.w3.org/2000/svg";
// The caption under a person's dot on the map. A FUNCTION, not a table, for the
// same two reasons `precisionWord()` below is one: a table of literals is
// invisible to anything that scans for `WavrT(` call sites, and it is built once
// — in whichever language happened to be active while this file was parsed —
// so it would keep saying "walking" after somebody switches to Portuguese.
// Unknown postures get no caption rather than a raw wire value.
function postureLabel(posture){
  switch(posture){
    case "walking":  return WavrT("walking");
    case "standing": return WavrT("standing");
    case "sitting":  return WavrT("sitting");
    case "lying":    return WavrT("lying");
    default:         return "";
  }
}
// Item 1: kill sensor jargon — every place a raw modality name would otherwise show
// (room-card "why" rows, source toggles, Status list) renders this friendly label instead,
// falling back to the raw name for anything not yet mapped.
// A FUNCTION, not a table, and for the reason `kinds()` and `connDesc()` are
// functions too: `WavrT` answers in whatever language is active when it is
// CALLED, and a table built once at parse time would still be in the old
// language after somebody switches. More importantly the literals live at a
// `WavrT(...)` call site here, where the catalogue check can see them — inside
// a table they were invisible, so all seven rendered English in every language.
// The KEYS are the wire's own modality names and never translate.
function modalityLabel(name){
  switch(name){
    case "wifi_csi": return WavrT("Wi-Fi");
    case "ruview":   return WavrT("Wi-Fi CSI");
    case "camera":   return WavrT("Camera");
    case "network":  return WavrT("Network");
    case "sim":      return WavrT("Simulated");
    case "mmwave":   return WavrT("Radar");
    case "ble":      return WavrT("Bluetooth");
    // `wavr.nodes.SENSOR_MODALITY` maps a PIR sensor node to modality "pir" and
    // an unrecognised/generic presence node to "node" — both real wire values
    // this switch never had a case for, so a PIR node's room-card "why" row
    // read the raw token "Pir" (capitalize CSS on an un-translated fallback).
    // Same word discoveries.js already uses for the identical sensor type
    // ("Motion sensor (PIR)"), and the same word nodes.js already falls back
    // to for an unnamed node (`WavrT("node")`, translated "sensor") — reusing
    // both rather than inventing a third spelling of either.
    case "pir":      return WavrT("Motion sensor (PIR)");
    case "node":     return WavrT("node");
    default:         return String(name || "");
  }
}
// One glyph per sensing modality, so a room's detection-methods breakdown reads at a glance
// (Camera / Bluetooth / Wi-Fi / Network / Radar) — reuses the icons already in the sprite sheet.
const MODALITY_ICON = {camera:"ic-camera", network:"ic-net", ble:"ic-bt", mmwave:"ic-radar",
                        wifi_csi:"ic-wifi", ruview:"ic-wifi", sim:"ic-net"};
// ---- Stage 3a (§6): THE one grey→green confidence scale. Three consumers — the room
// card's confidence ring, render3D()'s floor tint, and the room-card confidence bar —
// all call THIS function, so the gradient can never drift between them.
function colorFor(pct){
  const t = Math.min(Math.max(+pct || 0, 0), 1);
  const a = [120, 130, 138], b = [61, 181, 74];   // unlit grey (≈--dim, darkened) → --accent
  return "rgb(" + a.map((v, i) => Math.round(v + (b[i] - v) * t)).join(",") + ")";
}
// Trust weights per modality — mirrors backend fusion.py DEFAULT_WEIGHTS exactly (same
// table SimulatorProvider already mirrors). Feeds the per-source weight disclosure line
// ("Camera · weight 43% · agrees"); 0.5 is the backend's fallback for unknown modalities.
// `pir`/`node` (sensor-node modalities, 2026-07-11) added to keep this mirror exact.
// The per-source share of the vote now arrives IN the source row (`share`,
// fusion.py). This file used to keep a copy of `DEFAULT_WEIGHTS` and divide it
// out — which knew nothing about the freshness decay or the per-sensor
// reliability factor that also go into the real mass, so the evidence panel
// showed an authoritative percentage that was wrong for exactly the two sensors
// somebody opens that panel about: the aging one and the measured-unreliable
// one. One producer; the copy is gone.
// ---- Precision ladder (DISTINCT axis from confidence — fusion.py RESOLUTION_SCOPE/_fuse).
// confidence = how SURE someone is present; precision = how DETAILED an answer the fused
// evidence can honestly support (house -> room -> count -> position). Mirrors the backend
// enum client-side once — the level/pct/next are
// computed ONLY server-side and read verbatim here; the frontend never re-derives a rung.
// The RANK is arithmetic — it orders the rungs, and no reader ever sees it — so
// it stays a table. The WORDS are not: they were two tables of English literals, and a
// table of literals is wrong here twice over. Nothing that scans for `WavrT(`
// call sites can see inside a data table, so an entire named feature rendered
// English in a translated product without any completeness check noticing; and a
// table is evaluated ONCE, at parse time, so even marked-up literals would have
// frozen whichever language was active when the file loaded.
//
// Both are functions now, which is the shape `kinds()` (wizard.js) and
// `connDesc()` (connectors.js) already use for exactly this: the literals sit
// inside `WavrT(...)` where a scanner finds them, and the answer is rebuilt in
// the language that is active when the ladder is drawn.
const PRECISION_RANK = {none:0, house:1, room:2, count:3, position:4};
function precisionWord(rung){
  switch(rung){
    case "house":    return WavrT("Home");
    case "room":     return WavrT("Room");
    case "count":    return WavrT("People");
    case "position": return WavrT("Positions");
    default:         return "—";     // "none", and anything a newer Core invents
  }
}
function precisionNextCopy(next){
  switch(next){
    case "add_room_sensor":
      return WavrT("Add a room sensor (Bluetooth/radar) to locate the room");
    case "add_counting_sensor":
      return WavrT("Turn on a camera or radar to count people here");
    case "calibrate_camera_position":
      return WavrT("Calibrate a camera for exact positions");
    default:
      return "";
  }
}
// Every modality seen anywhere in the house — a modality present here but absent from a
// room's sources[] renders as that room's "no sensor here" (no-coverage) row (§6).
const SEEN_MODS = new Set();
const REDUCED_MOTION = matchMedia("(prefers-reduced-motion: reduce)");
let radarUpdate = null;
let highlightRoom3D = null;   // set inside renderRadar() once the 3D pipeline exists (§6)
// Map liveness applier (fake-presence-on-disconnect fix): set inside renderRadar(); the
// live-only /api/cameras poll below feeds it the per-camera liveness tri-state so the
// house map can render an OFFLINE camera's room distinctly from a sensor-confirmed-empty
// or a no-coverage one. Null (no-op) in demo/companion, where there are no cameras.
let applyMapLiveness = null;
// The map's own verdict, published for the ONE other place that must not
// contradict it: the off-screen summary a screen reader gets. That summary used
// to derive its own answer from `roomOcc` alone and announced amber and slate
// rooms as "Empty" — the map is not allowed to be more certain in words than it
// is in colour, so it now asks rather than re-derives.
let mapStateOf = null;      // (room) -> one of the documented states
let mapRoomNames = null;    // () -> every room the map knows about
// SVG font-size scales with the viewBox transform like any other geometric length, so a
// fixed user-space font-size shrinks/grows with room/viewport size. rebuildRoomsForFloor()
// recomputes this (rendered CSS px per 1 viewBox metre) on every layout so room labels and
// posture captions (set from this) can be expressed in REAL px and stay legible on a phone.
let radarPxPerM = 40;

