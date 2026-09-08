// ==========================================================================
// devices.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ==== Stage-2 Command Center: Devices — Detected / Catalog / Active + setup ladder ====
// Additive only. Consumes the SAME payloads the existing render*() functions already fetch,
// via four one-line hooks (__wavrSystem/__wavrCameras/__wavrInventory/__wavrRS). The device
// catalog is a same-origin static file (./vendor/device-catalog.json), fetched once on first
// entry into the Dispositivos tab and cached in memory — ZERO external requests. Every catalog
// or inventory string is rendered with createElement/textContent (never innerHTML).
(function(){
  "use strict";
  var M = (typeof MODE !== "undefined") ? MODE : "simulated";
  var $ = function(id){ return document.getElementById(id); };
  var SVGNS = "http://www.w3.org/2000/svg";

  // ---------- English label maps (1:1 with catalog fields — no invented data) ----------
  var CAT_EN = { ble:"Bluetooth (BLE)", "esp-diy":"ESP DIY", uwb:"UWB", network:"Network",
    camera:"Camera", contact:"Opening (door/window)", pir:"Motion (PIR)",
    environmental:"Environment", sound:"Sound", "lock-control":"Lock",
    "plug-control":"Smart plug", adapter:"Accessory", "hub-bridge":"Hub / bridge",
    mmwave:"mmWave radar", "wifi-csi":"Wi-Fi CSI", "safety-alarm":"Safety alarm" };
  var CAT_ICON = { ble:"ic-bt", "esp-diy":"ic-chip", uwb:"ic-radar", network:"ic-net",
    camera:"ic-camera", contact:"ic-door", pir:"ic-motion", environmental:"ic-thermo",
    sound:"ic-sound", "lock-control":"ic-lock", "plug-control":"ic-plug", adapter:"ic-usb",
    "hub-bridge":"ic-hub", mmwave:"ic-radar", "wifi-csi":"ic-wifi", "safety-alarm":"ic-alert" };
  var COV_EN = { point:"Point", room:"Room", zone:"Zone", perimeter:"Perimeter", "whole-home":"Whole Space" };
  var COV_LOW = { point:"a point", room:"a room", zone:"a zone", perimeter:"the perimeter", "whole-home":"the whole Space" };
  var COV_SAME = { point:"point", room:"room", zone:"zone", perimeter:"perimeter", "whole-home":"space" };
  var DET_EN = { presence:"presence", identity:"identity", motion:"motion", environment:"environment",
    person:"person", sound:"sound", contact:"contact", vitals:"vitals", fall:"fall", hazard:"hazard" };
  var CTRL_EN = { siren:"siren", lock:"lock", switch:"switch", plug:"plug",
    garage:"garage", valve:"valve", light:"light" };
  var CTRL_VERB = { siren:"trigger the siren", lock:"lock/unlock the door",
    switch:"turn the switch on/off", plug:"turn the plug on/off",
    garage:"open/close the garage", valve:"shut off the valve", light:"turn the light on/off" };
  var ST_EN = { "addable-now":"Ready to add", "via-home-assistant":"Via Home Assistant",
    "via-esp":"Via ESP", roadmap:"Coming soon" };
  var ST_CLS = { "addable-now":"st-now", "via-home-assistant":"st-ha", "via-esp":"st-esp", roadmap:"st-road" };
  var ST_ORDER = { "addable-now":0, "via-esp":1, "via-home-assistant":2, roadmap:3 };
  var HEALTH_EN = { fresh:"fresh signal", stale:"aging signal", dead:"no signal" };
  // `modalityLabel` is defined in format.js, which loads first.
  var MODLBL = function(n){ return modalityLabel(n); };
  function roadmapSentence(){
    return WavrT("There's no automatic setup path for this device yet — " +
      "once Wavr's setup agent exists, it will do this for you.");
  }

  // ---------- shared state (fed by the four one-line hooks in the main script) ----------
  var CAT = null, CATP = null;   // catalog array + memoized fetch promise (one fetch, ever)
  var stSel = new Set();         // status-filter chips (multi-toggle)
  var CAT_SHOW_ALL = false;      // one-shot "Browse all" reveal — a real query supersedes it
  var catCounts = null;          // per-category counts (built once in buildFilters, reused by the prompt)
  var invDevices = [];           // last /api/inventory devices (renderNetwork's 15s poll)
  var sysInfo = null;            // last /api/system payload (renderControls' 3s poll)
  var camsList = [];             // last /api/cameras payload (renderCameras)
  var healthMap = {};            // modality -> {health, age_s} from live RoomState frames
  var dsubCur = null;
  // ONVIF camera scan (detect -> connect). Results live only in JS; never persisted client-side.
  var onvifCams = [];            // probe.cameras[] from the last scan (masked rtsp only)
  var onvifErrs = [];            // probe.errors[] {host, reason}
  var scanState = "idle";        // idle | scanning | done | offoptin | error
  var scanNote = "";             // one-line honesty note under the header
  var scannedOnce = false;       // has a scan run this session
  var bannerDismissed = false;   // first-run "set up your cameras" strip, one-shot per session

  function norm(s){
    s = String(s == null ? "" : s).toLowerCase();
    try{ return s.normalize("NFD").replace(/[̀-ͯ]/g, ""); }catch(e){ return s; }
  }
  function shorten(s, n){ s = String(s == null ? "" : s); return s.length > n ? s.slice(0, n - 1) + "…" : s; }
  function elc(tag, cls, text){
    var e = document.createElement(tag);
    if(cls) e.className = cls;
    if(text != null) e.textContent = text;
    return e;
  }
  function icon(sym){
    var svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("aria-hidden", "true");
    var use = document.createElementNS(SVGNS, "use");
    use.setAttribute("href", "#" + sym);
    svg.appendChild(use);
    return svg;
  }
  function lbl(n){ return MODLBL(n) || n; }
  // Every label the maps above can yield, spelled out as a LITERAL inside WavrT(). tmap()
  // translates THROUGH here instead of calling WavrT() with a variable, because a
  // WavrT(variable) is invisible to the catalogue's completeness check — a label nothing
  // declares is a label nobody translates, and it stays English on a translated screen.
  // One case per label; the default returns the English unchanged.
  function enumLabel(en){
    switch(en){
      // category (CAT_EN)
      case "Bluetooth (BLE)":        return WavrT("Bluetooth (BLE)");
      case "ESP DIY":                return WavrT("ESP DIY");
      case "UWB":                    return WavrT("UWB");
      case "Network":                return WavrT("Network");
      case "Camera":                 return WavrT("Camera");
      case "Opening (door/window)":  return WavrT("Opening (door/window)");
      case "Motion (PIR)":           return WavrT("Motion (PIR)");
      case "Environment":            return WavrT("Environment");
      case "Sound":                  return WavrT("Sound");
      case "Lock":                   return WavrT("Lock");
      case "Smart plug":             return WavrT("Smart plug");
      case "Accessory":              return WavrT("Accessory");
      case "Hub / bridge":           return WavrT("Hub / bridge");
      case "mmWave radar":           return WavrT("mmWave radar");
      case "Wi-Fi CSI":              return WavrT("Wi-Fi CSI");
      case "Safety alarm":           return WavrT("Safety alarm");
      // coverage, three grammatical shapes (COV_EN / COV_LOW / COV_SAME)
      case "Point":                  return WavrT("Point");
      case "Room":                   return WavrT("Room");
      case "Zone":                   return WavrT("Zone");
      case "Perimeter":              return WavrT("Perimeter");
      case "Whole Space":            return WavrT("Whole Space");
      case "a point":                return WavrT("a point");
      case "a room":                 return WavrT("a room");
      case "a zone":                 return WavrT("a zone");
      case "the perimeter":          return WavrT("the perimeter");
      case "the whole Space":        return WavrT("the whole Space");
      case "the Space":              return WavrT("the Space");
      case "point":                  return WavrT("point");
      case "room":                   return WavrT("room");
      case "zone":                   return WavrT("zone");
      case "perimeter":              return WavrT("perimeter");
      case "space":                  return WavrT("space");
      // what it detects (DET_EN)
      case "presence":               return WavrT("presence");
      case "identity":               return WavrT("identity");
      case "motion":                 return WavrT("motion");
      case "environment":            return WavrT("environment");
      case "person":                 return WavrT("person");
      case "sound":                  return WavrT("sound");
      case "contact":                return WavrT("contact");
      case "vitals":                 return WavrT("vitals");
      case "fall":                   return WavrT("fall");
      case "hazard":                 return WavrT("hazard");
      // what it controls (CTRL_EN) and the verb for it (CTRL_VERB)
      case "siren":                  return WavrT("siren");
      case "lock":                   return WavrT("lock");
      case "switch":                 return WavrT("switch");
      case "plug":                   return WavrT("plug");
      case "garage":                 return WavrT("garage");
      case "valve":                  return WavrT("valve");
      case "light":                  return WavrT("light");
      case "trigger the siren":      return WavrT("trigger the siren");
      case "lock/unlock the door":   return WavrT("lock/unlock the door");
      case "turn the switch on/off": return WavrT("turn the switch on/off");
      case "turn the plug on/off":   return WavrT("turn the plug on/off");
      case "open/close the garage":  return WavrT("open/close the garage");
      case "shut off the valve":     return WavrT("shut off the valve");
      case "turn the light on/off":  return WavrT("turn the light on/off");
      // integration status (ST_EN) and the tooltip that explains it (ST_TIP)
      case "Ready to add":           return WavrT("Ready to add");
      case "Via Home Assistant":     return WavrT("Via Home Assistant");
      case "Via ESP":                return WavrT("Via ESP");
      case "Coming soon":            return WavrT("Coming soon");
      case "Wavr can use this device today":       return WavrT("Wavr can use this device today");
      case "Needs a Home Assistant bridge":        return WavrT("Needs a Home Assistant bridge");
      case "Needs a flashed ESP32/ESP8266 board":  return WavrT("Needs a flashed ESP32/ESP8266 board");
      case "Not supported yet":                    return WavrT("Not supported yet");
      // signal freshness (HEALTH_EN)
      case "fresh signal":           return WavrT("fresh signal");
      case "aging signal":           return WavrT("aging signal");
      case "no signal":              return WavrT("no signal");
      // last-resort noun for a category with no label
      case "device":                 return WavrT("device");
      default:                       return en;
    }
  }
  // The maps above turn a backend ENUM KEY into an English label, and the translation
  // catalogue is keyed on that English label — so the lookup happens HERE, at the render
  // site, and the backend goes on speaking its own enum. A key the map has never heard of
  // is data, not a sentence: it falls through untranslated instead of being recorded as a
  // missing translation and dragging a raw enum into the audit.
  function tmap(map, key, fallback){
    var en = map[key];
    if(en) return enumLabel(en);
    if(key) return key;
    return fallback ? enumLabel(fallback) : "";
  }
  function brandLbl(b){ return String(b || "—").replace(/^Gen\u00e9rico/, "Generic"); }

  // ---------- setup ladder: rung derived purely from status + wavr_ingress ----------
  // Rung 1 (AUTO) intentionally has no branch: no catalog device routes into it today
  // (backend self-announce doesn't exist) — no fake populated state, no fake empty state.
  var R3_ING = { mqtt:1, "serial-uart":1, "esp-uart":1, "esp-wifi":1, ble:1, "home-assistant":1 };
  function rungFor(c){
    if(c.status === "roadmap") return 4;                // informational, CTA becomes "Coming soon"
    if(c.wavr_ingress === "rtsp-onvif") return 2;       // quick guided (cameras)
    if(R3_ING[c.wavr_ingress]) return 3;                // manual guided (the bulk of the catalog)
    if(c.wavr_ingress === "network-scan") return "net"; // already watched by Network — no add button
    return "acc";                                       // hardware accessory (no ingestion)
  }

  // ---------- fusion-contribution sentence: template PER CATEGORY (not per device) ----------
  function fusionSentence(c){
    var det = Array.isArray(c.detects) ? c.detects : [];
    var s;
    if(c.category === "camera" && (det.indexOf("presence") >= 0 || det.indexOf("motion") >= 0 || det.indexOf("person") >= 0))
      s = WavrT("Confirms presence visually — reduces false positives from mmWave/PIR alone in the same {where}.",
                { where: tmap(COV_SAME, c.coverage, "space") });
    else if(c.category === "mmwave")
      s = WavrT("Detects presence even without motion (breathing/still) — covers PIR's blind spot.");
    else if((c.category === "ble" || c.category === "network") && det.indexOf("identity") >= 0)
      s = WavrT("Adds identity (who, not just whether someone's there) — improves fusion confidence when " +
                "combined with another sensor's presence.");
    else if(c.category === "safety-alarm")
      s = WavrT("Life-safety hazard alarm (smoke/leak) — surfaced as a high-priority alert, " +
                "not an occupancy signal; it never inflates or lowers presence confidence.");
    else if(c.category === "environmental")
      s = WavrT("Additional context (temperature/humidity/air), not presence — enriches the summary, " +
                "doesn't count toward occupancy confidence.");
    else
      s = WavrT("Extends detection coverage to {where} via {how}.",
                { where: tmap(COV_LOW, c.coverage, "the Space"),
                  how: shorten(c.modality || tmap(CAT_EN, c.category), 80) });
    if(c.control && c.control !== "none")
      s += " " + WavrT("Also lets Wavr act: {action} with confirmation.", { action: tmap(CTRL_VERB, c.control) });
    return s;
  }

  // ---------- privacy row: lock badge; catalog caveats in AMBER (honest warning, not an error) ----------
  function buildPrivacy(c){
    var note = String(c.privacy_note || "");
    if(!note) return null;
    var warn = /caveat/i.test(note);
    var idx = note.indexOf(". ");
    var first = (idx > 0 && idx < note.length - 2) ? note.slice(0, idx + 1) : note;
    var line = elc("span", "cap-priv");
    line.appendChild(icon(warn ? "ic-alert" : "ic-lock"));
    line.appendChild(elc("span", null, shorten(first, 120) + " "));
    if(first.length >= note.length){
      var d0 = elc("div", "cap-privd" + (warn ? " warn" : ""));
      d0.appendChild(line);
      return d0;
    }
    var det = elc("details", "cap-privd" + (warn ? " warn" : ""));
    var sum = document.createElement("summary");
    line.appendChild(elc("span", "pv-more", WavrT("see more")));
    sum.appendChild(line);
    det.appendChild(sum);
    det.appendChild(elc("p", "cap-priv-full", note));
    return det;
  }

  // ---------- CTA footer: the status decides the rung; roadmap NEVER becomes a button ----------
  // P4 fix 3: every actionable card gets TWO unambiguous-intent buttons — "View setup
  // guide" (instructions only) vs. the real add/confirm action (green accent).
  // The rung logic doesn't change: a camera (rung 2) adds via the form; config devices
  // (rung 3) only CONFIRM with the hub (a poll) — the labels say so.
  function buildCta(c, opts){
    opts = opts || {};
    var wrap = elc("div");
    var cta = elc("div", "cap-cta");
    var r = rungFor(c);
    if(r === 2){
      var g2 = elc("button", "ctl small", WavrT("View setup guide"));
      g2.type = "button";
      g2.onclick = function(ev){ openSetup(c, { ip: opts.ip || "", trigger: ev.currentTarget }); };
      cta.appendChild(g2);
      var b2 = elc("button", "ctl small primary", WavrT("Add camera"));
      b2.type = "button";
      b2.title = WavrT("opens the form that actually adds the camera");
      b2.onclick = function(ev){ openSetup(c, { ip: opts.ip || "", trigger: ev.currentTarget, focus: "form" }); };
      cta.appendChild(b2);
    } else if(r === 3){
      var g3 = elc("button", "ctl small", WavrT("View setup guide"));
      g3.type = "button";
      g3.onclick = function(ev){ openSetup(c, { trigger: ev.currentTarget }); };
      cta.appendChild(g3);
      var b3 = elc("button", "ctl small primary", WavrT("Mark as installed"));
      b3.type = "button";
      b3.title = WavrT("confirms with the hub whether the source has appeared — doesn't create anything on its own");
      b3.onclick = function(ev){ openSetup(c, { trigger: ev.currentTarget, focus: "verify" }); };
      cta.appendChild(b3);
    } else if(r === "net"){
      cta.appendChild(elc("span", "cap-tag", WavrT("Monitored by the Network tab — no setup needed in the app")));
    } else if(r === "acc"){
      cta.appendChild(elc("span", "cap-tag", WavrT("Hardware accessory — not a data source")));
    } else {
      cta.appendChild(elc("span", "cap-tag", WavrT("🔮 Coming soon")));
    }
    wrap.appendChild(cta);
    if(r === 4) wrap.appendChild(elc("p", "cap-road", roadmapSentence()));
    return wrap;
  }

  // ---------- Capability Explainer: ONE component, reused in Catalog/Detected/overlay ----------
  function buildCapCard(c, opts){
    opts = opts || {};
    var card = elc("article", "cap-card" + (c.status === "roadmap" ? " dim" : ""));
    var head = elc("div", "cap-head");
    var ic = elc("span", "cap-icon");
    ic.appendChild(icon(CAT_ICON[c.category] || "ic-grid"));
    var tw = elc("div");
    tw.appendChild(elc("div", "cap-title", c.name || WavrT("(unnamed)")));
    tw.appendChild(elc("div", "cap-brand", brandLbl(c.brand) + " · " + tmap(CAT_EN, c.category)));
    head.appendChild(ic); head.appendChild(tw);
    card.appendChild(head);
    if(opts.detMeta) card.appendChild(opts.detMeta);
    if(c.modality) card.appendChild(elc("p", "cap-mod", c.modality));
    var chips = elc("div", "cchips");
    chips.appendChild(elc("span", "cchip " + (ST_CLS[c.status] || ""), tmap(ST_EN, c.status)));
    if(c.coverage) chips.appendChild(elc("span", "cchip cov",
      WavrT("covers: {what}", { what: tmap(COV_EN, c.coverage) })));
    (Array.isArray(c.detects) ? c.detects : []).forEach(function(d){
      chips.appendChild(elc("span", "cchip det", tmap(DET_EN, d)));
    });
    if(c.control && c.control !== "none")
      chips.appendChild(elc("span", "cchip act", WavrT("controls: {what}", { what: tmap(CTRL_EN, c.control) })));
    else
      chips.appendChild(elc("span", "cchip", WavrT("no control action")));
    if(opts.confChip) chips.appendChild(opts.confChip);
    card.appendChild(chips);
    card.appendChild(elc("p", "cap-fusion", fusionSentence(c)));
    var priv = buildPrivacy(c);
    if(priv) card.appendChild(priv);
    if(c.notes) card.appendChild(elc("p", "cap-notes", c.notes));
    if(!opts.noCta) card.appendChild(buildCta(c, opts));
    return card;
  }

  // ---------- Catalog: single fetch + offline search and filters ----------
  function ensureCatalog(){
    if(CATP) return CATP;
    var msg = $("catMsg");
    if(msg){ msg.hidden = false; msg.textContent = WavrT("loading the catalog…"); }
    CATP = fetch("./vendor/device-catalog.json")   // same origin — nothing leaves home
      .then(function(r){ if(!r.ok) throw new Error("http " + r.status); return r.json(); })
      .then(function(d){
        CAT = (Array.isArray(d) ? d : []).filter(function(x){ return x && typeof x === "object"; });
        buildFilters();
        renderCatalog();
        renderDetectados();
      })
      .catch(function(){
        CATP = null;
        // The file path used to be on screen: "couldn't load the local
        // catalog (vendor/device-catalog.json)". A path is nothing a household
        // can act on, and this file ships WITH Wavr — if it cannot be read the
        // install is damaged, which is the sentence actually worth saying.
        if(msg){ msg.hidden = false; msg.textContent = WavrT("Wavr could not read its own device catalogue. Reinstall Wavr to put it back."); }
      });
    return CATP;
  }

  function optEl(v, label){ var o = document.createElement("option"); o.value = v; o.textContent = label; return o; }
  function buildFilters(){
    var chipBox = $("catStatusChips");
    if(!chipBox) return;
    chipBox.textContent = "";
    var stc = {};
    CAT.forEach(function(c){ stc[c.status] = (stc[c.status] || 0) + 1; });
    // v2 tooltips: what each integration-status chip actually means
    var ST_TIP = { "addable-now": "Wavr can use this device today",
                   "via-home-assistant": "Needs a Home Assistant bridge",
                   "via-esp": "Needs a flashed ESP32/ESP8266 board",
                   "roadmap": "Not supported yet" };
    ["addable-now", "via-home-assistant", "via-esp", "roadmap"].forEach(function(st){
      var b = elc("button", "fchip", WavrT("{label} ({n})", { label: tmap(ST_EN, st), n: stc[st] || 0 }));
      b.type = "button";
      b.setAttribute("aria-pressed", "false");
      b.setAttribute("data-tip", ST_TIP[st] ? enumLabel(ST_TIP[st]) : "");
      b.onclick = function(){
        if(stSel.has(st)) stSel.delete(st); else stSel.add(st);
        b.classList.toggle("on", stSel.has(st));
        b.setAttribute("aria-pressed", stSel.has(st) ? "true" : "false");
        renderCatalog();
      };
      chipBox.appendChild(b);
    });
    var catc = {};
    CAT.forEach(function(c){ catc[c.category] = (catc[c.category] || 0) + 1; });
    catCounts = catc;   // v2 search-first: the prompt's quick chips reuse these counts
    var sc = $("catFCategoria"); sc.textContent = ""; sc.appendChild(optEl("", WavrT("category (all)")));
    // Sorted on the label the reader actually sees, so the list reads alphabetically in
    // whatever language it is rendered in (identical order in the source language).
    Object.keys(catc).sort(function(a, b){ return norm(tmap(CAT_EN, a)).localeCompare(norm(tmap(CAT_EN, b))); })
      .forEach(function(k){ sc.appendChild(optEl(k, WavrT("{label} ({n})", { label: tmap(CAT_EN, k), n: catc[k] }))); });
    var so = $("catFCobertura"); so.textContent = ""; so.appendChild(optEl("", WavrT("coverage (all)")));
    ["point", "room", "zone", "perimeter", "whole-home"].forEach(function(k){
      if(CAT.some(function(c){ return c.coverage === k; })) so.appendChild(optEl(k, tmap(COV_EN, k)));
    });
    var sd = $("catFDetecta"); sd.textContent = ""; sd.appendChild(optEl("", WavrT("detects (all)")));
    var dets = {};
    CAT.forEach(function(c){ (Array.isArray(c.detects) ? c.detects : []).forEach(function(d){ dets[d] = 1; }); });
    Object.keys(dets).sort().forEach(function(d){ sd.appendChild(optEl(d, tmap(DET_EN, d))); });
    var sk = $("catFControle"); sk.textContent = ""; sk.appendChild(optEl("", WavrT("control (all)")));
    sk.appendChild(optEl("none", WavrT("no control action")));
    var ctrls = {};
    CAT.forEach(function(c){ if(c.control && c.control !== "none") ctrls[c.control] = 1; });
    Object.keys(ctrls).sort().forEach(function(k){ sk.appendChild(optEl(k, tmap(CTRL_EN, k))); });
    [sc, so, sd, sk].forEach(function(s){ s.onchange = renderCatalog; });
    var si = $("catSearch");
    if(si){
      // P5 fix 9: ~130ms debounce — each render rebuilds the full filtered card DOM, which
      // adds up on low-end phones. Only the free-text search debounces; the dropdown/chip
      // filters above stay immediate (one change per interaction, not per keystroke).
      var siT = null;
      si.oninput = function(){ clearTimeout(siT); siT = setTimeout(renderCatalog, 130); };
    }
  }

  // v2 search-first: the grid only renders when the user asked for something — a search
  // term, any active filter, or the explicit one-shot "Browse all" reveal.
  function catalogHasQuery(){
    var si = $("catSearch");
    if(si && norm(si.value).trim()) return true;
    if(stSel.size) return true;
    return ["catFCategoria", "catFCobertura", "catFDetecta", "catFControle"].some(function(id){
      var s = $(id); return !!(s && s.value);
    });
  }

  function quickChip(label, cat){
    var b = elc("button", "fchip", label);
    b.type = "button";
    b.onclick = function(){ var s = $("catFCategoria"); if(s) s.value = cat; renderCatalog(); };
    return b;
  }

  // "Detected on your network" shortcut chips — live/companion-with-token only, built from
  // real matchDevice() output over the last /api/inventory poll (never fabricated in demo).
  // Grouped by matched catalog category, max 4 chips; a tap filters the catalog to that category.
  function renderCatDetectedChips(){
    var wrap = $("catDetectedWrap"), chips = $("catDetectedChips");
    if(!wrap || !chips) return;
    var canDetect = (M === "live") ||
      (M === "companion" && typeof companionToken === "function" && !!companionToken());
    chips.textContent = "";
    if(!canDetect || !CAT || !invDevices.length){ wrap.hidden = true; return; }
    var usedIps = activeCameraIps();
    var byCat = {};
    invDevices.forEach(function(d){
      if(!d || typeof d !== "object") return;
      if(d.ip && usedIps.has(d.ip)) return;   // already a configured camera — an active source
      var m = matchDevice(d);
      if(m.conf === "none" || !m.cat) return;
      byCat[m.cat] = (byCat[m.cat] || 0) + 1;
    });
    var cats = Object.keys(byCat).sort(function(a, b){ return byCat[b] - byCat[a]; }).slice(0, 4);
    if(!cats.length){ wrap.hidden = true; return; }
    cats.forEach(function(k){
      chips.appendChild(quickChip(WavrT("{label} · {n} on your network", { label: tmap(CAT_EN, k), n: byCat[k] }), k));
    });
    wrap.hidden = false;
  }

  // v2 search-first: the default (no-query) state — a short prompt, one-tap category chips,
  // the detected-on-network shortcut, and a low-key "Browse all" escape hatch.
  function renderCatPrompt(){
    var box = $("catPrompt"), list = $("catList"), msg = $("catMsg");
    if(!box || !CAT) return;
    if(list) list.textContent = "";
    if(msg) msg.hidden = true;
    var cc = $("catCount");
    if(cc) cc.textContent = WavrT("{n} devices", { n: CAT.length });
    var h = $("catPromptH");
    if(h) h.textContent = WavrT("Search {n} sensors & devices to expand your coverage", { n: CAT.length });
    var chips = $("catQuickChips");
    if(chips){
      chips.textContent = "";
      var counts = catCounts || {};
      Object.keys(counts).sort(function(a, b){ return norm(tmap(CAT_EN, a)).localeCompare(norm(tmap(CAT_EN, b))); })
        .forEach(function(k){ chips.appendChild(quickChip(WavrT("{label} ({n})", { label: tmap(CAT_EN, k), n: counts[k] }), k)); });
    }
    renderCatDetectedChips();
    var all = $("catShowAll");
    if(all){
      all.textContent = WavrT("Browse all {n} devices", { n: CAT.length });
      all.onclick = function(){ CAT_SHOW_ALL = true; renderCatalog(); };
    }
    box.hidden = false;
  }

  function renderCatalog(){
    if(!CAT) return;
    var list = $("catList"), msg = $("catMsg");
    if(!list) return;
    // v2 search-first guard: no query and no Browse-all → prompt state instead of 203 cards.
    // A real query supersedes (and clears) the one-shot Browse-all flag, so clearing the
    // search returns to the prompt rather than silently staying in show-all.
    var hasQ = catalogHasQuery();
    if(hasQ) CAT_SHOW_ALL = false;
    if(!hasQ && !CAT_SHOW_ALL){ renderCatPrompt(); return; }
    var prompt = $("catPrompt");
    if(prompt) prompt.hidden = true;
    var q = norm($("catSearch").value),
        cat = $("catFCategoria").value, cov = $("catFCobertura").value,
        det = $("catFDetecta").value, ctl = $("catFControle").value;
    var out = CAT.filter(function(c){
      if(q && norm(c.name).indexOf(q) < 0 && norm(c.brand).indexOf(q) < 0) return false;
      if(stSel.size && !stSel.has(c.status)) return false;
      if(cat && c.category !== cat) return false;
      if(cov && c.coverage !== cov) return false;
      if(det && (Array.isArray(c.detects) ? c.detects : []).indexOf(det) < 0) return false;
      if(ctl && c.control !== ctl) return false;
      return true;
    });
    // default order: addable-now first, roadmap last (and dimmed on the card)
    out.sort(function(a, b){
      var d = (ST_ORDER[a.status] ?? 9) - (ST_ORDER[b.status] ?? 9);
      return d !== 0 ? d : norm(a.name).localeCompare(norm(b.name));
    });
    var cc = $("catCount");
    if(cc) cc.textContent = WavrT("{shown} of {total} devices", { shown: out.length, total: CAT.length });
    list.textContent = "";
    var frag = document.createDocumentFragment();
    out.forEach(function(c){ frag.appendChild(buildCapCard(c, {})); });
    list.appendChild(frag);
    if(msg){
      msg.hidden = out.length > 0;
      if(!out.length) msg.textContent = WavrT("No matches for those filters — try a different word or clear a filter.");
    }
  }

  // ---------- Detected: client-side match cascade over /api/inventory ----------
  // Honesty (spec §2): the offline OUI table covers ~20 vendors — Espressif and routers
  // are the strong signal; most cameras resolve vendor="unknown" and fall to medium/fallback.
  // We never force "high"/a brand guess the data can't support.
  var BRAND_STOP = { open:1, source:1, generic:1, generico:1, community:1, foundation:1,
    formerly:1, chipset:1, boards:1, etc:1, and:1, the:1, via:1, "class":1, compatible:1, life360:1 };
  function tokens(s){
    return norm(s).split(/[^a-z0-9]+/).filter(function(t){ return t.length > 2 && !BRAND_STOP[t]; });
  }
  var IP_ING = { "rtsp-onvif":1, "network-scan":1, "esp-wifi":1, mqtt:1 };
  function ipVisible(c){ return IP_ING[c.wavr_ingress] === 1 || c.category === "hub-bridge"; }
  var PHONE_VENDORS = { apple:1, samsung:1, xiaomi:1, google:1, huawei:1, lg:1 };
  // v2 remap: keys are the backend's FIXED 18-type taxonomy (the old slash-strings are gone
  // from the wire). Only types with a sensible catalog-category home are listed — the rest
  // fall through to the brand-token cascade, which stays honest about what it can't narrow.
  var DTYPE_CATS = { camera:["camera"], router:["network"], gateway:["hub-bridge"],
    esp_dev:["esp-diy"], speaker:["hub-bridge"], smart_plug:["plug-control"],
    iot_sensor:["environmental", "contact", "pir", "sound"] };

  function matchDevice(d){
    var vendor = String(d.vendor || "").trim();
    var vkey = norm(vendor);
    var dtype = norm(d.device_type || "");
    if(!vkey || vkey === "unknown"){
      // v2 taxonomy: the classifier's randomized-MAC heuristic now lands on phone/low
      return { conf: "none",
        hint: dtype === "phone" ? WavrT("Randomized MAC with no OUI — probably a phone with network privacy turned on.") : null };
    }
    // known-vendor phone: presence via ARP (the network-known-phone-arp entry)
    if(PHONE_VENDORS[vkey] && (dtype === "phone" || dtype === "tablet")){
      var phone = CAT.find(function(c){ return c.id === "network-known-phone-arp"; });
      if(phone) return { conf: "media", cat: "network", cands: [phone] };
    }
    // Espressif = silicon, not a product: partial signal → category (medium), never a SKU (high)
    if(vkey.indexOf("espressif") >= 0){
      var pool0 = CAT.filter(ipVisible);
      var esp = pool0.filter(function(c){ return c.category === "esp-diy"; });
      if(!esp.length) esp = CAT.filter(function(c){ return c.category === "esp-diy" && c.status !== "roadmap"; });
      return { conf: "media", cat: "esp-diy", cands: esp.slice(0, 3),
        hint: WavrT("ESP32/ESP8266 board — could be running any firmware (ESPresense, ESPHome, DIY).") };
    }
    var vtok = tokens(vendor);
    var pool = CAT.filter(ipVisible);   // BLE/serial/Zigbee-behind-a-hub never show up in an ARP scan
    var cands = pool.filter(function(c){
      var bt = tokens(c.brand);
      return bt.some(function(t){ return vtok.indexOf(t) >= 0; }) ||
             (vkey.length > 3 && norm(c.brand).indexOf(vkey) >= 0);
    });
    if(!cands.length) return { conf: "none" };
    var wanted = DTYPE_CATS[dtype];
    var narrowed = wanted ? cands.filter(function(c){ return wanted.indexOf(c.category) >= 0; }) : [];
    if(narrowed.length === 1) return { conf: "alta", cat: narrowed[0].category, cands: narrowed };
    var use = narrowed.length ? narrowed : cands;
    var freq = {};
    use.forEach(function(c){ freq[c.category] = (freq[c.category] || 0) + 1; });
    var best = Object.keys(freq).sort(function(a, b){ return freq[b] - freq[a]; })[0];
    return { conf: "media", cat: best,
             cands: use.filter(function(c){ return c.category === best; }).slice(0, 3) };
  }

  function activeCameraIps(){
    // best-effort exclusion: if a detected IP is already on a configured camera, it's an active source
    var s = new Set();
    camsList.forEach(function(c){
      var m = /(\d{1,3}\.){3}\d{1,3}/.exec(String((c && c.rtsp_url) || ""));
      if(m) s.add(m[0]);
    });
    return s;
  }

  function detMetaLine(d){
    var bits = [];
    // Resolved identity (a user-set name, else the device's own self-announced/PTR
    // hostname, CLEANED via display_name) leads the line when known — same priority
    // as the Network tab's rows.
    if(d.name) bits.push(d.name);
    else if(d.display_name) bits.push(d.display_name);
    else if(d.hostname) bits.push(d.hostname);
    if(d.ip) bits.push(d.ip);
    if(typeof maskMac === "function" && d.mac) bits.push(maskMac(d.mac));
    if(d.vendor && d.vendor !== "unknown") bits.push(d.vendor);
    if(d.device_type && d.device_type !== "unknown") bits.push(dtypeLabel(d.device_type));
    if(typeof fmtRelative === "function"){ var rel = fmtRelative(d.last_seen); if(rel) bits.push(WavrT("seen: {when}", { when: rel })); }
    return elc("div", "det-meta", WavrT("Seen on the network: {what}", { what: bits.join(" · ") }));
  }

  function goToRede(mac){
    if(typeof window.switchTab === "function") window.switchTab("rede");
    setTimeout(function(){
      if(typeof maskMac !== "function" || !mac) return;
      var want = maskMac(mac);
      var rows = document.querySelectorAll("#devList .dev-row");
      for(var i = 0; i < rows.length; i++){
        var mel = rows[i].querySelector(".mac");
        if(mel && mel.textContent === want){
          rows[i].scrollIntoView({ block: "center" });
          rows[i].classList.add("flash");
          (function(row){ setTimeout(function(){ row.classList.remove("flash"); }, 2200); })(rows[i]);
          break;
        }
      }
    }, 120);
  }

  function goToCatalog(opts){
    opts = opts || {};
    setDsub("catalogo");
    var apply = function(){
      if(!CAT) return;
      if(opts.category !== undefined){ var s = $("catFCategoria"); if(s) s.value = opts.category || ""; }
      if(opts.search !== undefined){ var i = $("catSearch"); if(i) i.value = opts.search || ""; }
      renderCatalog();
    };
    var p = ensureCatalog();
    if(p && p.then) p.then(apply); else apply();
  }

  function detActions(d, opts){
    var row = elc("div", "cap-cta");
    var b1 = elc("button", "linklike", WavrT("view in Network"));
    b1.type = "button";
    b1.onclick = function(){ goToRede(d.mac); };
    row.appendChild(b1);
    var b2 = elc("button", "linklike", WavrT("view in Catalog"));
    b2.type = "button";
    b2.onclick = function(){ goToCatalog({ category: (opts && opts.category) || "", search: "" }); };
    row.appendChild(b2);
    return row;
  }

  function buildDetCard(d, m){
    if(m.conf === "alta"){
      var sku = m.cands[0];
      var chip = elc("span", "cchip conf-alta", WavrT("high confidence"));
      var card = buildCapCard(sku, { ip: d.ip || "", detMeta: detMetaLine(d), confChip: chip });
      card.appendChild(detActions(d, { category: sku.category }));
      return card;
    }
    // medium: suggest the CATEGORY (not a model) — short OUI table, honesty > guessing
    var card2 = elc("article", "cap-card");
    var head = elc("div", "cap-head");
    var ic = elc("span", "cap-icon");
    ic.appendChild(icon(CAT_ICON[m.cat] || "ic-grid"));
    var tw = elc("div");
    tw.appendChild(elc("div", "cap-title", WavrT("Likely: {what}", { what: tmap(CAT_EN, m.cat) })));
    var brand2 = elc("div", "cap-brand",
      (d.vendor && d.vendor !== "unknown" ? d.vendor : WavrT("unknown vendor")) + " · ");
    brand2.appendChild(dtypeLabelEl(d.device_type, d.type_confidence, d.known === false));
    tw.appendChild(brand2);
    head.appendChild(ic); head.appendChild(tw);
    card2.appendChild(head);
    card2.appendChild(detMetaLine(d));
    var chips = elc("div", "cchips");
    chips.appendChild(elc("span", "cchip conf-media", WavrT("medium confidence")));
    card2.appendChild(chips);
    card2.appendChild(elc("p", "cap-mod", m.hint ||
      WavrT("The vendor ({vendor}) matches the catalog's {category} category, but the exact model can't be confirmed.",
            { vendor: d.vendor || "?", category: tmap(CAT_EN, m.cat) })));
    if(m.cands && m.cands.length){
      var row = elc("div", "cap-cta");
      row.appendChild(elc("span", "cap-tag", WavrT("could be:")));
      m.cands.forEach(function(c){
        var b = elc("button", "ctl small", shorten(c.name, 42));
        b.type = "button";
        b.onclick = function(ev){ openSetup(c, { ip: d.ip || "", trigger: ev.currentTarget }); };
        row.appendChild(b);
      });
      card2.appendChild(row);
    }
    card2.appendChild(detActions(d, { category: m.cat }));
    return card2;
  }

  function buildUnknownCard(d, m){
    var card = elc("article", "cap-card");
    var head = elc("div", "cap-head");
    var ic = elc("span", "cap-icon");
    ic.appendChild(icon(dtypeIcon(d.device_type)));   // v2: taxonomy icon (ic-qmark fallback)
    var tw = elc("div");
    var known = d.vendor && d.vendor !== "unknown";
    // Resolved identity (name, else the device's own self-announced/PTR hostname, CLEANED
    // via display_name) leads the title when known — same priority as the Network tab's
    // rows; only a truly unresolved device (no name, no hostname, no vendor) keeps the
    // honest "Unknown device" title.
    tw.appendChild(elc("div", "cap-title",
      d.name || d.display_name || d.hostname || (known ? d.vendor : WavrT("Unknown device"))));
    var brandU = elc("div", "cap-brand");
    brandU.appendChild(dtypeLabelEl(d.device_type, d.type_confidence, d.known === false));
    tw.appendChild(brandU);
    head.appendChild(ic); head.appendChild(tw);
    card.appendChild(head);
    card.appendChild(detMetaLine(d));
    card.appendChild(elc("p", "cap-mod", (m && m.hint) ||
      WavrT("Doesn't match any catalog entry — it could be a computer, a phone, or a device Wavr doesn't use as a sensor.")));
    var row = elc("div", "cap-cta");
    var b1 = elc("button", "linklike", WavrT("view in Network"));
    b1.type = "button";
    b1.onclick = function(){ goToRede(d.mac); };
    var b2 = elc("button", "linklike", WavrT("search in Catalog"));
    b2.type = "button";
    b2.onclick = function(){ goToCatalog({ search: known ? d.vendor : "", category: "" }); };
    row.appendChild(b1); row.appendChild(b2);
    card.appendChild(row);
    return card;
  }

  function setDetCount(n){ var e = $("detCount"); if(e) e.textContent = n ? " (" + n + ")" : ""; }
  function setAtvCount(){
    var e = $("atvCount");
    if(!e) return;
    if(M !== "live" || !sysInfo || !Array.isArray(sysInfo.sources)){ e.textContent = ""; return; }
    var on = sysInfo.sources.filter(function(s){ return s && s.enabled; }).length;
    e.textContent = " (" + on + "/" + sysInfo.sources.length + ")";
  }

  // P4 fix 6: illustrative card for the Detected empty state (demo/no hub) — 100% static
  // data, visibly labeled as an example; built with textContent only.
  function exampleDetCard(){
    var card = elc("article", "cap-card cap-example");
    var head = elc("div", "cap-head");
    var ic = elc("span", "cap-icon");
    ic.appendChild(icon("ic-camera"));
    var tw = elc("div");
    tw.appendChild(elc("div", "cap-title", WavrT("Example: a camera found on your Wi-Fi")));
    tw.appendChild(elc("div", "cap-brand", WavrT("illustrative example — not a real device")));
    head.appendChild(ic); head.appendChild(tw);
    card.appendChild(head);
    // The address and the vendor are made-up DATA; only the words around them are a sentence.
    card.appendChild(elc("div", "det-meta", WavrT("Seen on the network: {what}",
      { what: "192.168.1.42 · TP-Link · " + WavrT("Camera") + " · " + WavrT("seen: now") })));
    var chips = elc("div", "cchips");
    chips.appendChild(elc("span", "cchip ex-badge", WavrT("example")));
    chips.appendChild(elc("span", "cchip conf-alta", WavrT("high confidence")));
    card.appendChild(chips);
    card.appendChild(elc("p", "cap-mod",
      WavrT("With a Wavr hub on your network, the Wi-Fi devices that match the catalog show up " +
            "here like this, ready to become sensors with one add button.")));
    return card;
  }

  // ---------- ONVIF camera scan: detect -> connect (POST /api/onvif/probe, opt-in) ----------
  function canDetectNow(){
    return (M === "live") ||
      (M === "companion" && typeof companionToken === "function" && !!companionToken());
  }
  function onvifTitle(cam){
    var mk = String((cam && cam.make) || "").trim();
    var md = String((cam && cam.model) || "").trim();
    var nm = String((cam && cam.name) || "").trim();
    if(nm && nm !== mk) return nm;
    var mm = (mk + " " + md).trim();
    return mm || WavrT("Camera at {host}", { host: (cam && cam.ip) || WavrT("unknown") });
  }
  // The backend masks ONLY the password (rtsp://user:***@host). A masked-password URL can't
  // be POSTed as-is -> the user must supply creds (Configure). No masked password -> the URL
  // is directly connectable (Connect now).
  function onvifNeedsCreds(cam){ return /:\*{2,}@/.test(String((cam && cam.rtsp_url) || "")); }
  function onvifBranch(cam){
    var url = String((cam && cam.rtsp_url) || "");
    var profs = (cam && Array.isArray(cam.profiles)) ? cam.profiles.length : 0;
    if(url && profs && !onvifNeedsCreds(cam)) return 1;                 // CONNECT NOW
    if(url || profs || (cam && (cam.make || cam.model || cam.name))) return 2;  // CONFIGURE
    return 3;                                                          // GUIDED
  }
  function parseRtsp(url){
    var out = { ip: "", port: "554", path: "" };
    var m = /^rtsps?:\/\/(?:[^@/]*@)?([^:/]+)(?::(\d+))?(\/[^\s]*)?/i.exec(String(url || ""));
    if(m){ out.ip = m[1] || ""; if(m[2]) out.port = m[2]; if(m[3]) out.path = m[3]; }
    return out;
  }
  function onvifCatalogEntry(cam){
    return { id: "onvif-" + ((cam && cam.ip) || "cam"), name: onvifTitle(cam),
      brand: String((cam && cam.make) || "IP camera"), category: "camera",
      modality: "RTSP / ONVIF IP camera", coverage: "room",
      detects: ["presence", "motion", "person"], control: "none",
      status: "addable-now", wavr_ingress: "rtsp-onvif",
      // Written HERE, unlike every other privacy_note (those are catalogue data), so this
      // one is a sentence the shell owns and can translate.
      privacy_note: WavrT("Camera frames are processed in memory and never leave this Space.") };
  }
  function startsOffLine(){
    var p = elc("p", "starts-off");
    // ONE key for the whole sentence, with the emphasised word as a SLOT: marking the bold
    // word on its own would leave the two halves to be reassembled in English clause order.
    // WavrT with no vars returns the entry verbatim, slot intact, so it can be split here.
    var full = WavrT("Adds the camera turned {off}. Turn it on in Devices → Active when you're ready — Wavr never enables a camera for you.");
    var parts = full.split("{off}");
    p.appendChild(document.createTextNode(parts[0]));
    if(parts.length > 1){
      p.appendChild(elc("b", null, WavrT("off")));
      p.appendChild(document.createTextNode(parts.slice(1).join("{off}")));
    }
    return p;
  }

  function onvifConnectRow(cam, holder, primaryBtn){
    if(holder.firstChild) return;   // already open
    var wrap = elc("div", "onvif-connect");
    function fld(labelText, val, ph){
      var l = elc("label");
      l.appendChild(elc("span", null, labelText));
      var i = document.createElement("input");
      i.type = "text"; if(val) i.value = val; if(ph) i.placeholder = ph;
      l.appendChild(i); wrap.appendChild(l);
      return i;
    }
    var defName = cam.ip ? "cam_" + String(cam.ip).replace(/\./g, "_") : "cam";
    var iName = fld(WavrT("name"), defName, WavrT("e.g. cam_living_room"));
    var iRoom = fld(WavrT("room"), suggestRoom(), WavrT("e.g. living room"));
    var actions = elc("div", "oc-actions");
    var add = elc("button", "ctl small primary", WavrT("Add camera (off)"));
    add.type = "button";
    var fb = elc("span", "action-fb"); fb.setAttribute("aria-live", "polite");
    actions.appendChild(add); actions.appendChild(fb);
    wrap.appendChild(actions);
    add.onclick = async function(){
      var nm = iName.value.trim(), rm = iRoom.value.trim();
      if(!nm || !rm){ actionFeedback(fb, false, WavrT("name and room are required")); return; }
      add.disabled = true;
      var body = { name: nm, room: rm, rtsp_url: cam.rtsp_url, confidence: 0.4 };
      var r = null;
      try{
        r = await WavrAPI.fetch("/api/cameras", {method: "POST", json: body});
      }catch(e){}
      if(r && r.ok){
        actionFeedback(fb, true, null, WavrT("✓ camera added — starts off"));
        if(window.__wavrCamerasRefresh) window.__wavrCamerasRefresh();
        setTimeout(function(){ setDsub("ativos"); }, 900);
      } else {
        add.disabled = false;
        var msg = WavrT("couldn't add the camera");
        if(r && r.status === 409) msg = WavrT("a camera with that name already exists");
        actionFeedback(fb, false, msg);
      }
    };
    holder.appendChild(wrap);
    if(primaryBtn) primaryBtn.disabled = true;
    iName.focus();
  }

  function onvifGuidedSteps(host){
    var s = [
      { h: WavrT("Enable the camera account / RTSP"),
        p: WavrT("In the vendor app (e.g. Tapo → Advanced Settings → Camera Account) create a username + password. This is what Wavr uses to read the stream.") }];
    s.push({ h: WavrT("(Optional) give it a fixed address"),
      p: WavrT("So Wavr keeps finding it, set a static IP or a DHCP reservation in your router for {host}.",
               { host: host || WavrT("this camera") }),
      copy: host || "" });
    return s;
  }
  function buildOnvifGuidedBody(card, host){
    var steps = onvifGuidedSteps(host);
    var wrap = elc("div", "ladder-steps");
    steps.forEach(function(s, i){
      var st = elc("div", "ladder-step");
      st.appendChild(elc("span", "ladder-num", String(i + 1)));
      var body = elc("div");
      body.appendChild(elc("h4", null, s.h));
      body.appendChild(elc("p", null, s.p));
      if(s.copy){
        var row = elc("div", "copyrow");
        row.appendChild(elc("code", "copyval", s.copy));
        var cb = elc("button", "ctl small", WavrT("Copy")); cb.type = "button";
        cb.onclick = function(){
          try{ if(navigator.clipboard) navigator.clipboard.writeText(s.copy).catch(function(){}); }catch(e){}
          cb.textContent = WavrT("Copied"); setTimeout(function(){ cb.textContent = WavrT("Copy"); }, 1500);
        };
        row.appendChild(cb);
        body.appendChild(row);
      }
      st.appendChild(body); wrap.appendChild(st);
    });
    var st3 = elc("div", "ladder-step");
    st3.appendChild(elc("span", "ladder-num", String(steps.length + 1)));
    var b3 = elc("div");
    b3.appendChild(elc("h4", null, WavrT("Scan again")));
    b3.appendChild(elc("p", null, WavrT("Once RTSP / the camera account is on, re-scan this camera — it will then offer Connect or Configure.")));
    var sb = elc("button", "ctl small primary", WavrT("Scan again")); sb.type = "button";
    sb.onclick = function(){ scanCameras(host ? [host] : undefined); };
    b3.appendChild(sb); st3.appendChild(b3); wrap.appendChild(st3);
    card.appendChild(wrap);
    card.appendChild(elc("p", "starts-off",
      WavrT("Wavr can't flip these switches on the camera for you — that stays your decision on the device. Once RTSP is on, scan again.")));
  }

  function buildOnvifCard(cam){
    var branch = onvifBranch(cam);
    var card = elc("article", "cap-card onvif-found");
    var head = elc("div", "cap-head");
    var ic = elc("span", "cap-icon"); ic.appendChild(icon("ic-camera"));
    var tw = elc("div");
    tw.appendChild(elc("div", "cap-title", onvifTitle(cam)));
    var brand = [String(cam.make || "").trim(), String(cam.model || "").trim()].filter(Boolean).join(" ");
    tw.appendChild(elc("div", "cap-brand", (brand || WavrT("IP camera")) + " · " + WavrT("Camera")));
    head.appendChild(ic); head.appendChild(tw);
    card.appendChild(head);
    var meta = []; if(cam.ip) meta.push(cam.ip); if(brand) meta.push(brand);
    card.appendChild(elc("div", "det-meta", WavrT("Seen on the network: {what}",
      { what: meta.join(" · ") || WavrT("(unknown)") })));
    var chips = elc("div", "cchips");
    chips.appendChild(elc("span", "cchip conf-alta", WavrT("camera found")));
    card.appendChild(chips);
    var priv = buildPrivacy(onvifCatalogEntry(cam)); if(priv) card.appendChild(priv);
    if(branch === 1){
      card.appendChild(elc("p", "onvif-stream", WavrT("Stream: {url}", { url: String(cam.rtsp_url || "") })));
      card.appendChild(startsOffLine());
      var cta1 = elc("div", "cap-cta");
      var connect = elc("button", "ctl small primary", WavrT("Connect now")); connect.type = "button";
      var holder = elc("div");
      connect.onclick = function(){ onvifConnectRow(cam, holder, connect); };
      cta1.appendChild(connect); card.appendChild(cta1); card.appendChild(holder);
    } else if(branch === 2){
      card.appendChild(elc("p", "cap-mod",
        WavrT("This camera answered, but needs the camera-account login before Wavr can read its stream.")));
      card.appendChild(startsOffLine());
      var cta2 = elc("div", "cap-cta");
      var cfg = elc("button", "ctl small primary", WavrT("Configure")); cfg.type = "button";
      cfg.onclick = function(ev){ openSetup(onvifCatalogEntry(cam),
        { ip: cam.ip || "", focus: "form", onvif: cam, trigger: ev.currentTarget }); };
      cta2.appendChild(cfg); card.appendChild(cta2);
    } else {
      buildOnvifGuidedBody(card, cam.ip || "");
    }
    return card;
  }
  function buildOnvifGuidedCard(host, reason){
    var card = elc("article", "cap-card");
    var head = elc("div", "cap-head");
    var ic = elc("span", "cap-icon"); ic.appendChild(icon("ic-camera"));
    var tw = elc("div");
    tw.appendChild(elc("div", "cap-title", WavrT("Camera at {host}", { host: host || WavrT("unknown") })));
    tw.appendChild(elc("div", "cap-brand", WavrT("answered ONVIF · no readable stream yet")));
    head.appendChild(ic); head.appendChild(tw);
    card.appendChild(head);
    if(host) card.appendChild(elc("div", "det-meta", WavrT("Seen on the network: {what}", { what: host })));
    card.appendChild(elc("p", "cap-mod",
      WavrT("A camera responded here but didn't expose a usable RTSP stream — the camera account / RTSP is probably still turned off.")));
    buildOnvifGuidedBody(card, host);
    return card;
  }

  function renderScanUI(){
    var btn = $("detScanCams"), note = $("detScanNote"), credRow = $("detScanCredRow");
    if(!btn) return;
    var can = canDetectNow();
    btn.hidden = !can;
    if(credRow) credRow.hidden = !can;
    if(!can){
      if(note){ note.hidden = true; note.textContent = ""; }
      var cbx = $("detScanCreds"); if(cbx) cbx.hidden = true;
      return;
    }
    btn.disabled = (scanState === "scanning");
    if(scanState === "scanning"){
      btn.setAttribute("aria-busy", "true");
      btn.textContent = "";
      btn.appendChild(elc("span", "scan-spin"));
      btn.appendChild(document.createTextNode(WavrT("Scanning…")));
    } else {
      btn.removeAttribute("aria-busy");
      btn.textContent = scannedOnce ? WavrT("Scan again") : WavrT("Scan for cameras");
    }
    if(note){
      var txt = scanNote;
      if(scanState === "idle" && !txt) txt = WavrT("Wavr can look for ONVIF/RTSP cameras on your local network.");
      if(scanState === "scanning") txt = WavrT("Looking for cameras on your network… this takes a few seconds.");
      note.textContent = txt;
      note.className = "det-scan-note" + ((scanState === "offoptin" || scanState === "error") ? " warn" : "");
      note.hidden = !txt;
    }
  }

  function renderBanner(){
    var box = $("detBanner");
    if(!box) return;
    box.textContent = "";
    var show = canDetectNow() && !bannerDismissed && !scannedOnce && !(camsList && camsList.length);
    // Shown on phones too: the non-technical owner is most likely on mobile, and this is
    // the only proactive camera-setup nudge. The .det-banner is flex-wrap/responsive and
    // only OFFERS a local scan (no false "camera found" claim), so it stays honest.
    if(!show){ box.hidden = true; return; }
    var b = elc("div", "det-banner");
    var t = elc("div", "db-text");
    // The bold is the LEAD of the line, not a word inside a clause, so the two halves are
    // whole units on their own and each gets its own entry.
    t.appendChild(elc("b", null, WavrT("Set up your cameras")));
    t.appendChild(document.createTextNode(WavrT(" — Wavr can look for IP cameras on your network.")));
    b.appendChild(t);
    var cta = elc("div", "db-cta");
    var scan = elc("button", "ctl small primary", WavrT("Scan for cameras")); scan.type = "button";
    scan.onclick = function(){ scanCameras(); };
    var no = elc("button", "linklike", WavrT("Not now")); no.type = "button";
    no.onclick = function(){ bannerDismissed = true; renderBanner(); };
    cta.appendChild(scan); cta.appendChild(no);
    b.appendChild(cta); box.appendChild(b); box.hidden = false;
  }

  async function scanCameras(targets){
    if(scanState === "scanning") return;                 // guard double-submit
    if(!canDetectNow()) return;
    bannerDismissed = true;                               // the banner's job is done once a scan starts
    scanState = "scanning"; scanNote = ""; renderScanUI(); renderBanner();
    var body = {};
    if(targets && targets.length) body.targets = targets;
    var u = $("detScanUser"), p = $("detScanPass");
    if(u && u.value.trim()) body.username = u.value.trim();
    if(p && p.value) body.password = p.value;
    var r = null, data = null;
    try{
      r = await WavrAPI.fetch("/api/onvif/probe", {method: "POST", json: body});
    }catch(e){ r = null; }
    if(p) p.value = "";                                   // never keep the scan password around
    if(!r){ scanState = "error"; scanNote = WavrT("Couldn't reach the hub — is it running?"); renderScanUI(); return; }
    if(r.status === 403 || r.status === 503){
      scanState = "offoptin";
      scanNote = WavrT("Camera scanning is off by default. Turn on WAVR_ONVIF_PROBE on the hub to let Wavr " +
        "probe your LAN for cameras (local-only, nothing leaves your network).");
      renderScanUI(); return;
    }
    if(!r.ok){ scanState = "error"; scanNote = WavrT("Couldn't reach the hub — is it running?"); renderScanUI(); return; }
    try{ data = await r.json(); }catch(e){ data = null; }
    if(!data || typeof data !== "object"){
      scanState = "error"; scanNote = WavrT("The hub returned an unexpected response."); renderScanUI(); return;
    }
    onvifCams = Array.isArray(data.cameras) ? data.cameras.filter(function(c){ return c && typeof c === "object"; }) : [];
    onvifErrs = Array.isArray(data.errors) ? data.errors.filter(function(e){ return e && typeof e === "object"; }) : [];
    scannedOnce = true; scanState = "done";
    var n = onvifCams.length;
    scanNote = n ? WavrT("Found {n} camera.|Found {n} cameras.", { n: n })
                 : WavrT("No cameras answered. If yours is plugged in, it may need RTSP enabled first — " +
                   "see “How to enable”, then scan again.");
    renderScanUI(); renderDetectados();
  }

  function renderDetectados(){
    var list = $("detList"), empty = $("detEmpty");
    if(!list || !empty) return;
    renderScanUI(); renderBanner();
    var canDetect = (M === "live") ||
      (M === "companion" && typeof companionToken === "function" && !!companionToken());
    if(!canDetect){
      // demo / companion without a token: HONEST empty state — nothing is scanned from here;
      // the labeled example card shows what detection would do with a real hub.
      list.textContent = "";
      list.appendChild(exampleDetCard());
      empty.hidden = false;
      // `canDetect` is false for TWO different situations, and they were given
      // one sentence. A companion is a real install on somebody's phone; it is
      // reading this because it has not been paired yet, and calling it a demo
      // is false twice over — it also hides the one thing that would fix it.
      // The read-only banner three hundred lines below already splits these.
      empty.textContent = (M === "companion")
        ? WavrT("Detection happens on the hub. Pair this device with your Wavr hub to see what it finds.")
        : WavrT("Detection needs a Wavr instance running on your local network — this demo " +
          "doesn't access any network. Browse the Catalog to see the devices Wavr understands.");
      setDetCount(0);
      return;
    }
    if(!CAT){
      list.textContent = "";
      empty.hidden = false;
      empty.textContent = WavrT("loading the catalog to match against your network…");
      return;
    }
    var usedIps = activeCameraIps();
    // ONVIF cameras occupy the ACTIVE layer; their IPs are excluded from the passive layer below.
    var onvifIps = new Set(onvifCams.map(function(c){ return c && c.ip; }).filter(Boolean));
    var matched = [], unmatched = [];
    invDevices.forEach(function(d){
      if(!d || typeof d !== "object") return;
      if(d.ip && usedIps.has(d.ip)) return;    // already a configured camera — an active source
      if(d.ip && onvifIps.has(d.ip)) return;   // already surfaced above as an ONVIF camera card
      var m = matchDevice(d);
      if(m.conf === "none") unmatched.push({ d: d, m: m });
      else matched.push({ d: d, m: m });
    });
    matched.sort(function(a, b){
      var r = (a.m.conf === "alta" ? 0 : 1) - (b.m.conf === "alta" ? 0 : 1);
      if(r) return r;
      return String(b.d.last_seen || "").localeCompare(String(a.d.last_seen || ""));
    });
    // guided cards = probe errors where a camera answered ONVIF but has no usable RTSP profile
    // (account/RTSP likely off). Every other error is a dim "couldn't reach" line, not a card.
    var guidedErrs = onvifErrs.filter(function(e){
      var host = e && (e.host || e.ip);
      return host && !usedIps.has(host) && !onvifIps.has(host) && /no usable rtsp/i.test(String(e.reason || ""));
    });
    var reachErrs = onvifErrs.filter(function(e){ return guidedErrs.indexOf(e) < 0 && e && (e.host || e.ip); });
    setDetCount(onvifCams.length + matched.length);
    list.textContent = "";
    var frag = document.createDocumentFragment();
    onvifCams.forEach(function(cam){ frag.appendChild(buildOnvifCard(cam)); });
    guidedErrs.forEach(function(e){ frag.appendChild(buildOnvifGuidedCard(e.host || e.ip, e.reason)); });
    matched.forEach(function(x){ frag.appendChild(buildDetCard(x.d, x.m)); });
    if(unmatched.length){
      frag.appendChild(elc("h4", "det-sub", WavrT("No catalog match ({n})", { n: unmatched.length })));
      // Scale: building a card per unmatched device froze the tab at airport scale
      // (5000 devices = 5000 cards). Cap the render; the header count stays the TRUE
      // total, and the Network tab's searchable list reaches every device.
      var DET_UNMATCHED_CAP = 300;
      var unmShown = unmatched.slice(0, DET_UNMATCHED_CAP);
      unmShown.forEach(function(x){ frag.appendChild(buildUnknownCard(x.d, x.m)); });
      if(unmatched.length > unmShown.length){
        frag.appendChild(elc("p", "det-scan-err",
          WavrT("Showing {shown} of {total} unmatched devices — use the Network tab to search the full list.",
                { shown: unmShown.length, total: unmatched.length })));
      }
    }
    if(reachErrs.length){
      frag.appendChild(elc("p", "det-scan-err",
        WavrT("Couldn't reach: {hosts}. These are usually non-camera devices — safe to ignore.",
              { hosts: reachErrs.map(function(e){ return String(e.host || e.ip); }).join(", ") })));
    }
    list.appendChild(frag);
    var none = !onvifCams.length && !guidedErrs.length && !matched.length && !unmatched.length;
    empty.hidden = !none;
    if(none) empty.textContent = WavrT("Nothing detected yet — the hub's network inventory needs to be active " +
      "(see the Network tab). Reminder: BLE, serial, or Zigbee sensors (behind a hub) don't have their own IP and " +
      "never appear here — use the Catalog to set them up.");
  }

  // ---------- setup overlay (rungs 2 and 3; net/accessory = informational) ----------
  var setupOpen = false, setupTrigger = null;
  var setupOverlay = $("setupOverlay"), setupContent = $("setupContent"), setupTitle = $("setup-h");

  function closeSetup(){
    if(!setupOpen) return;
    setupOpen = false;
    setupOverlay.hidden = true;
    // P5 fix 5: un-inert the app BEFORE restoring focus to the in-app trigger
    if(window.__wavrSetAppInert) window.__wavrSetAppInert(false);
    if(setupTrigger && setupTrigger.focus) setupTrigger.focus();
    setupTrigger = null;
  }
  var backBtn = $("setupBack");
  if(backBtn) backBtn.addEventListener("click", closeSetup);
  // P5 fix 5: same focus trap the gear overlay uses (helper defined in the Stage-1 shell)
  if(setupOverlay && window.__wavrTrapFocus) window.__wavrTrapFocus(setupOverlay);
  document.addEventListener("keydown", function(e){ if(e.key === "Escape" && setupOpen) closeSetup(); });

  function suggestRoom(){
    try{
      if(typeof HOUSE !== "undefined" && HOUSE && Array.isArray(HOUSE.floors) && HOUSE.floors[0] &&
         Array.isArray(HOUSE.floors[0].rooms) && HOUSE.floors[0].rooms[0] && HOUSE.floors[0].rooms[0].name)
        return HOUSE.floors[0].rooms[0].name;
    }catch(e){}
    return "";
  }

  function openSetup(c, opts){
    opts = opts || {};
    if(!setupOverlay || !setupContent) return;
    setupTrigger = opts.trigger || null;
    setupContent.textContent = "";
    var r = rungFor(c);
    setupTitle.textContent = r === 2 ? WavrT("Add camera") : r === 3 ? WavrT("Setup guide") : WavrT("Device");
    var top = elc("div", "tile");
    top.appendChild(buildCapCard(c, { noCta: true }));
    setupContent.appendChild(top);
    if(r === 2) setupContent.appendChild(buildRung2(c, opts));
    else if(r === 3) setupContent.appendChild(buildRung3(c, opts));
    else if(r === "net") setupContent.appendChild(infoTile(WavrT("Monitored by the network"),
      WavrT("This kind of device is watched by Wavr's network scan (Network tab) — there's nothing to set up " +
      "in the app. If it's on your LAN, it shows up there automatically.")));
    else if(r === "acc") setupContent.appendChild(infoTile(WavrT("Hardware accessory"),
      WavrT("Helps you mount or install sensors, but it isn't a Wavr data source — there's nothing to set up here.")));
    else if(r === 4) setupContent.appendChild(infoTile(WavrT("Coming soon"), roadmapSentence()));
    setupOverlay.hidden = false;
    setupOpen = true;
    if(window.__wavrSetAppInert) window.__wavrSetAppInert(true);   // P5 fix 5
    // P4 fix 3: when the click came from the real action ("Add camera"/"Mark as
    // installed"), land straight on the form/confirm step instead of the top of the guide.
    if(opts.focus === "verify"){
      var vb = setupContent.querySelector("[data-setup-verify]");
      if(vb && !vb.disabled){ vb.scrollIntoView({ block: "center" }); vb.focus(); return; }
    } else if(opts.focus === "form"){
      var fi = setupContent.querySelector("form.setup-form input");
      if(fi){ fi.scrollIntoView({ block: "center" }); fi.focus(); return; }
    }
    if(backBtn) backBtn.focus();
  }

  function infoTile(h, p){
    var tile = elc("div", "tile");
    var th = elc("div", "tile-head");
    th.appendChild(elc("h2", null, h));
    tile.appendChild(th);
    tile.appendChild(elc("p", "panel-note", p));
    return tile;
  }

  // Rung 2 — Quick guided (RTSP cameras): only the IP comes pre-filled; without an ONVIF
  // probe we don't pretend to have found the stream. Credentials masked, never leave the LAN.
  function buildRung2(c, opts){
    var tile = elc("div", "tile");
    var th = elc("div", "tile-head");
    var onv = opts.onvif;
    var parsed = onv ? parseRtsp(onv.rtsp_url) : null;
    th.appendChild(elc("h2", null, WavrT("Quick setup — RTSP camera")));
    tile.appendChild(th);
    if(onv){
      tile.appendChild(elc("p", "setup-note",
        WavrT("Wavr found this camera on your network and pre-filled its address. Add the camera-account " +
        "username and password below — they stay on this device.")));
    } else {
      tile.appendChild(elc("p", "setup-note",
        WavrT("Wavr can't discover the stream address on its own yet (no ONVIF probe). " +
        "Only the IP comes pre-filled — you get the RTSP username, password, and path from the camera's app or manual.")));
    }
    if(M !== "live"){
      tile.appendChild(elc("p", "panel-note",
        WavrT("Adding cameras happens on the Wavr hub's panel (local access).") + " " +
        (M === "companion" ? WavrT("This viewer is read-only.") : WavrT("This demo has no hub."))));
      return tile;
    }
    var form = document.createElement("form");
    form.className = "setup-form";
    function field(name, lb, opt){
      opt = opt || {};
      var lab = elc("label", opt.wide ? "setup-wide" : null);
      lab.appendChild(elc("span", null, lb));
      var inp = document.createElement("input");
      inp.name = name;
      if(opt.type) inp.type = opt.type;
      if(opt.value) inp.value = opt.value;
      if(opt.ph) inp.placeholder = opt.ph;
      if(opt.req) inp.required = true;
      lab.appendChild(inp);
      if(opt.helpEl) lab.appendChild(opt.helpEl);
      form.appendChild(lab);
      return inp;
    }
    var iName = field("name", WavrT("name"), { ph: WavrT("e.g. cam_living_room"), req: 1 });
    var iRoom = field("room", WavrT("room"), { value: suggestRoom(), ph: WavrT("e.g. living room"), req: 1 });
    var ipVal = opts.ip || (parsed && parsed.ip) || "";
    var iIp   = field("ip", ipVal ? WavrT("Camera IP (detected)") : WavrT("Camera IP"), { value: ipVal, ph: "192.168.1.20", req: 1 });
    var iPort = field("port", WavrT("port"), { value: (parsed && parsed.port) || "554", type: "number" });
    iPort.min = "1"; iPort.max = "65535";
    var iUser = field("user", WavrT("RTSP username"), { ph: WavrT("e.g. wavr") });
    var cred = elc("p", "cred-note");
    cred.appendChild(icon("ic-lock"));
    cred.appendChild(document.createTextNode(WavrT("Stays on this device only — never leaves your network.")));
    var iPass = field("pass", WavrT("RTSP password"), { type: "password", helpEl: cred });
    if(onv){
      form.appendChild(elc("p", "starts-off setup-wide",
        WavrT("Use the camera account you created in the vendor app (e.g. Tapo → Advanced Settings → Camera Account), not your Tapo/vendor login.")));
    }
    var iPath = field("path", WavrT("stream path"), { value: (parsed && parsed.path) || "", ph: WavrT("e.g. /stream1 (see the camera's app/manual)"), wide: 1 });
    var iConf = field("confidence", WavrT("minimum confidence (0–1)"), { value: "0.4", type: "number" });
    iConf.step = "0.05"; iConf.min = "0"; iConf.max = "1";
    var prev = elc("p", "setup-preview setup-wide", "");
    form.appendChild(prev);
    function composed(mask){
      var user = iUser.value.trim(), pass = iPass.value, ip = iIp.value.trim(),
          port = (iPort.value || "554").trim(), path = iPath.value.trim();
      if(path && path[0] !== "/") path = "/" + path;
      var credPart = user ? encodeURIComponent(user) + ":" + (mask ? "•••" : encodeURIComponent(pass)) + "@" : "";
      return "rtsp://" + credPart + ip + ":" + port + path;
    }
    function updPrev(){ prev.textContent = iIp.value.trim() ? WavrT("Composed URL: {url}", { url: composed(true) }) : ""; }
    form.addEventListener("input", updPrev);
    updPrev();
    var subWrap = elc("div", "setup-wide");
    var sub = elc("button", "ctl", WavrT("Add camera"));
    sub.type = "submit";
    var fb = elc("span", "action-fb");
    fb.setAttribute("aria-live", "polite");
    subWrap.appendChild(sub);
    subWrap.appendChild(fb);
    form.appendChild(subWrap);
    if(onv){ var off = startsOffLine(); off.className = "starts-off setup-wide"; form.appendChild(off); }
    form.onsubmit = async function(e){
      e.preventDefault();
      if(sub.disabled) return;              // guard against rapid double-submit
      sub.disabled = true;
      var body = { name: iName.value.trim(), room: iRoom.value.trim(),
                   rtsp_url: composed(false), confidence: parseFloat(iConf.value) || 0.4 };
      var r = null;
      try{
        r = await WavrAPI.fetch("/api/cameras", {method: "POST", json: body});
      }catch(err){}
      if(r && r.ok){
        if(typeof actionFeedback === "function") actionFeedback(fb, true, null, WavrT("✓ camera added"));
        window.__wavrCamerasRefresh?.();   // re-syncs the list in Active
        setTimeout(function(){ closeSetup(); setDsub("ativos"); }, 900);
      } else {
        var msg = WavrT("couldn't add the camera");
        if(r && r.status === 409) msg = WavrT("a camera with that name already exists");
        if(typeof actionFeedback === "function") actionFeedback(fb, false, msg);
        else fb.textContent = msg;
        sub.disabled = false;               // restore so the owner can retry
      }
    };
    tile.appendChild(form);
    return tile;
  }

  // Rung 3 — Manual guided (mqtt/serial/esp/ble/home-assistant): checklist with copy-values.
  // HONEST: doesn't create the source from the app (no endpoint exists) — the final step only
  // CONFIRMS, polling GET /api/system (fallback /api/status).
  function r3steps(c){
    var ing = c.wavr_ingress;
    var slug = String(c.id || "new-source").replace(/[^a-z0-9_-]/gi, "").toLowerCase() || "new-source";
    var regStep = { h: WavrT("Register the source in the hub's configuration"),
      p: WavrT("Add the source to the Wavr hub's config file / environment variables — " +
         "sources can't be created from here yet."),
      copy: slug, cl: WavrT("suggested name for the source") };
    if(ing === "home-assistant") return [
      { h: WavrT("Integrate the device into Home Assistant"),
        p: WavrT("Add it to HA as usual (vendor integration, Zigbee, Z-Wave, etc.) and confirm the entity shows up there.") },
      { h: WavrT("Copy the entity_id in HA"),
        p: WavrT("In Settings → Devices & Services → Entities."),
        copy: "sensor.YOUR_ENTITY", cl: WavrT("entity_id format — replace with the real name") },
      regStep];
    if(ing === "serial-uart" || ing === "esp-uart") return [
      { h: WavrT("Connect the device to the hub over USB/serial"),
        p: WavrT("Use the cable or adapter listed in the sensor's manual.") },
      { h: WavrT("Identify the serial port and baud rate"),
        p: WavrT("On Linux it's usually /dev/ttyUSB0 or /dev/ttyACM0; the right baud rate is in the vendor's manual."),
        copy: "/dev/ttyUSB0", cl: WavrT("typical port — confirm on the hub") },
      regStep];
    // mqtt / ble / esp-wifi: the node publishes to a local MQTT broker
    var pre = ing === "ble"
      ? { h: WavrT("Prepare a BLE listening node"),
          p: WavrT("Bluetooth signals reach Wavr through an ESP32 node (e.g. ESPresense) that listens for BLE and publishes over MQTT.") }
      : { h: WavrT("Prepare the device"),
          p: WavrT("Install/set up the firmware or app the vendor recommends (see the device note above).") };
    return [pre,
      { h: WavrT("Point it at the local MQTT broker"),
        p: WavrT("The device publishes its readings to an MQTT broker on your network (e.g. Mosquitto running on the hub)."),
        copy: "mqtt://HUB-IP:1883", cl: WavrT("broker address — replace HUB-IP with the hub's real IP") },
      regStep];
  }

  async function fetchSources(){
    // confirmation poll: only READS state — never creates anything
    try{
      var r = await fetch(location.origin + "/api/system");
      if(r.ok){ var s = await r.json(); return new Set((s.sources || []).map(function(x){ return x.name; })); }
    }catch(e){}
    try{
      var r2 = await fetch(location.origin + "/api/status");
      if(r2.ok){ var s2 = await r2.json(); return new Set((s2.sources || []).map(function(x){ return x.name; })); }
    }catch(e){}
    return null;
  }

  function buildRung3(c, opts){
    var tile = elc("div", "tile");
    var th = elc("div", "tile-head");
    th.appendChild(elc("h2", null, WavrT("Manual setup — step by step")));
    tile.appendChild(th);
    tile.appendChild(elc("p", "setup-note",
      WavrT("This guide doesn't create the source from the app — today, Wavr sources are defined in the hub's " +
      "configuration (config file / environment variables). The final step only confirms it has appeared.")));
    var steps = r3steps(c);
    var wrap = elc("div", "ladder-steps");
    steps.forEach(function(s, i){
      var st = elc("div", "ladder-step");
      st.appendChild(elc("span", "ladder-num", String(i + 1)));
      var body = elc("div");
      body.appendChild(elc("h4", null, s.h));
      body.appendChild(elc("p", null, s.p));
      if(s.copy){
        var row = elc("div", "copyrow");
        row.appendChild(elc("code", "copyval", s.copy));
        var cb = elc("button", "ctl small", WavrT("Copy"));
        cb.type = "button";
        cb.onclick = function(){
          try{ if(navigator.clipboard) navigator.clipboard.writeText(s.copy).catch(function(){}); }catch(e){}
          cb.textContent = WavrT("Copied");
          setTimeout(function(){ cb.textContent = WavrT("Copy"); }, 1500);
        };
        row.appendChild(cb);
        if(s.cl) row.appendChild(elc("span", "copylabel", s.cl));
        body.appendChild(row);
      }
      st.appendChild(body);
      wrap.appendChild(st);
    });
    // final step: verify the connection (a poll, not creation)
    var vst = elc("div", "ladder-step");
    vst.appendChild(elc("span", "ladder-num", String(steps.length + 1)));
    var vbody = elc("div");
    vbody.appendChild(elc("h4", null, WavrT("Mark as installed — verify connection")));
    vbody.appendChild(elc("p", null, WavrT("After setting this up outside the app, tap here to confirm the source has appeared on the hub.")));
    var vres = elc("p", "verify-res", "");
    vres.setAttribute("aria-live", "polite");
    // Saliency fix (18 screen): the one action that completes onboarding is now the one
    // accent-primary control in the ladder (was plain "ctl small" — same weight-class as the
    // step "Copy" buttons, so it never won the peak against the corner trust badge).
    var vbtn = elc("button", "ctl small primary", WavrT("Verify connection"));
    vbtn.type = "button";
    vbtn.setAttribute("data-setup-verify", "1");   // P4 fix 3: target of the "Mark as installed" button
    if(M !== "live"){
      vbtn.disabled = true;
      vres.textContent = WavrT("Verification checks with the Wavr hub — available only on the local panel.");
    } else {
      var baseline = null;
      fetchSources().then(function(names){ baseline = names; });
      vbtn.onclick = async function(){
        vbtn.disabled = true;
        vres.textContent = WavrT("checking with the hub…");
        var names = await fetchSources();
        if(names === null){
          vres.textContent = WavrT("couldn't reach the hub — is it running?");
        } else {
          var base = baseline || new Set();
          var fresh = [];
          names.forEach(function(n){ if(!base.has(n)) fresh.push(n); });
          if(fresh.length){
            vres.textContent = WavrT("✓ new source registered: {names} — see it in Devices → Active.",
                                     { names: fresh.map(lbl).join(", ") });
          } else if(names.size){
            vres.textContent = WavrT("no new source yet ({n} registered: {names}). Apply the configuration on the hub and " +
              "restart the service, then verify again.",
              { n: names.size, names: Array.from(names).map(lbl).join(", ") });
          } else {
            vres.textContent = WavrT("no source registered on the hub yet — apply the configuration and restart the service.");
          }
          // PR4: end-of-setup discovery check. If nothing new showed up, a pathological network
          // (isolation / second net / silent multicast) is a likely reason — surface that verdict
          // HERE, before the user concludes, instead of leaving them guessing. Best-effort: a
          // missing/healthy verdict simply adds nothing.
          if(!fresh.length && typeof discoveryVerdictCallout === "function"){
            var dd = await fetchDiscoveryVerdict();
            if(dd && Array.isArray(dd.checks)){
              var callout = discoveryVerdictCallout(dd.checks);
              if(callout){
                vbody.appendChild(elc("p", "panel-note",
                  WavrT("The network diagnosis may explain why the device didn't show up:")));
                vbody.appendChild(callout);
              }
            }
          }
        }
        vbtn.disabled = false;
      };
    }
    vbody.appendChild(vbtn);
    vbody.appendChild(vres);
    vst.appendChild(vbody);
    wrap.appendChild(vst);
    tile.appendChild(wrap);
    return tile;
  }

  // ---------- Active: per-modality health via RoomState ----------
  // Panel-review finding #16: the "Data sources" tile (#srcToggles/#srcHealth) moved
  // into System card B (#controls, unhidden by renderControls itself) — the #ativosFontes
  // wrapper it used to live in is gone, so there is nothing left to unhide here.

  function renderHealth(){
    var box = $("srcHealth");
    if(!box) return;
    var keys = Object.keys(healthMap);
    box.hidden = !keys.length;
    box.textContent = "";
    keys.sort().forEach(function(k){
      var h = healthMap[k] || {};
      var hc = (h.health === "fresh" || h.health === "stale" || h.health === "dead") ? " hdot-" + h.health : "";
      var row = elc("div", "sh-row");
      row.appendChild(elc("i", "hdot" + hc));
      row.appendChild(elc("span", "m", lbl(k)));
      var txt = tmap(HEALTH_EN, h.health) || "—";
      if(h.age_s != null && h.health && h.health !== "fresh") txt += " (" + h.age_s + "s)";
      row.appendChild(elc("span", null, "— " + txt));
      box.appendChild(row);
    });
  }

  // ---------- hooks fed by the main script (one-liners added there in Stage 2) ----------
  var healthTimer = null;
  window.__wavrRS = function(rs){
    try{
      ((rs && rs.sources) || []).forEach(function(s){
        if(s && s.modality) healthMap[s.modality] = { health: s.health || "", age_s: s.age_s };
      });
    }catch(e){}
    if(M !== "live") return;   // the Sources tile is live-only
    if(healthTimer) return;
    healthTimer = setTimeout(function(){ healthTimer = null; renderHealth(); }, 1000);
  };
  window.__wavrSystem = function(s){
    sysInfo = (s && typeof s === "object") ? s : null;
    setAtvCount();
  };
  window.__wavrCameras = function(cams){
    camsList = Array.isArray(cams) ? cams : [];
    if(CAT){ renderDetectados(); renderCatDetectedChips(); }   // exclusions may have changed
  };
  window.__wavrInventory = function(devs){
    invDevices = Array.isArray(devs) ? devs : [];
    if(CAT){ renderDetectados(); renderCatDetectedChips(); }   // prompt's detected chips too
  };

  // ---------- wire the ONVIF camera-scan controls (button + optional-creds toggle) ----------
  (function wireScan(){
    var btn = $("detScanCams");
    if(btn) btn.addEventListener("click", function(){ scanCameras(); });
    var tgl = $("detScanCredsToggle"), box = $("detScanCreds");
    if(tgl && box) tgl.addEventListener("click", function(){
      var open = box.hidden;
      box.hidden = !open;
      tgl.setAttribute("aria-expanded", open ? "true" : "false");
    });
    renderScanUI();
  })();

  // ---------- System: collapses the source dot-list into a summary line linking to Active ----------
  (function(){
    var ss = $("statusSources"), sum = $("statusSourcesSummary");
    if(!ss || !sum) return;
    function upd(){
      // P5 fix 11: same shared counter the topbar pill mirror uses (Stage-1 shell)
      var c = window.__wavrCountActiveSources ? window.__wavrCountActiveSources() : null;
      if(!c) return;
      sum.textContent = "";
      // ONE key for the whole clause, with the ratio as a SLOT: WavrT with no vars returns
      // the entry verbatim, slot intact, so the <b> can be dropped where the language wants it.
      var full = WavrT("{count} sources active · ");
      var parts = full.split("{count}");
      sum.appendChild(document.createTextNode(parts[0]));
      var b = document.createElement("b");
      b.textContent = c.act + "/" + c.total;
      sum.appendChild(b);
      sum.appendChild(document.createTextNode(parts.slice(1).join("{count}")));
      var link = elc("button", "linklike", WavrT("view in Devices"));
      link.type = "button";
      link.onclick = function(){
        if(typeof window.switchTab === "function") window.switchTab("dispositivos");
        setDsub("ativos");
      };
      sum.appendChild(link);
    }
    new MutationObserver(upd).observe(ss, { childList: true, subtree: true });
    upd();
  })();

  // ---------- sub-tab shell: Detected (n) · Catalog · Active (n) ----------
  var DSUB_KEY = "wavr.dsub.v1";
  var dsubBtns = { detectados: $("dsubBtnDetectados"), catalogo: $("dsubBtnCatalogo"), ativos: $("dsubBtnAtivos") };
  var dsubViews = { detectados: $("dsubViewDetectados"), catalogo: $("dsubViewCatalogo"), ativos: $("dsubViewAtivos") };
  function setDsub(name, opts){
    opts = opts || {};
    if(!dsubViews[name]) name = "catalogo";
    dsubCur = name;
    // devices-tab-simple: any EXPLICIT navigation to a sub-tab (a cross-link from System/
    // Network, or a click on the Advanced tab bar itself, which is only reachable once
    // already revealed) also reveals the "Advanced" technical view it lives in — never on
    // the silent bootstrap call below, so a page load never force-opens it.
    if(!opts.silent){
      var advWrap = $("devAdvancedWrap"), advToggle = $("devAdvancedToggle");
      if(advWrap && advWrap.hidden){
        advWrap.hidden = false;
        if(advToggle){ advToggle.setAttribute("aria-expanded", "true"); advToggle.textContent = WavrT("Hide the full device list"); }
      }
    }
    Object.keys(dsubBtns).forEach(function(k){
      var on = k === name;
      if(dsubBtns[k]){
        dsubBtns[k].classList.toggle("on", on);
        dsubBtns[k].setAttribute("aria-selected", on ? "true" : "false");
        dsubBtns[k].tabIndex = on ? 0 : -1;
      }
      if(dsubViews[k]) dsubViews[k].hidden = !on;
    });
    try{ localStorage.setItem(DSUB_KEY, name); }catch(e){}
    if(name === "catalogo" || name === "detectados") ensureCatalog();
    if(name === "detectados") renderDetectados();
    if(name === "catalogo") focusCatSearch();
  }
  // v2 search-first: autofocus the search box on desktop only — never steal focus
  // (and pop the keyboard) on touch. No-op while the Devices panel is display:none.
  function focusCatSearch(){
    try{ if(!window.matchMedia("(pointer: fine)").matches) return; }catch(e){ return; }
    var si = $("catSearch");
    if(si) si.focus({ preventScroll: true });
  }
  window.__wavrSetDsub = setDsub;   // Stage-3b hook: Network drill-down cross-links to Detected
  Object.keys(dsubBtns).forEach(function(k){
    if(dsubBtns[k]) dsubBtns[k].addEventListener("click", function(){ setDsub(k); });
  });
  var addBtn = $("dsubAdd");
  if(addBtn) addBtn.addEventListener("click", function(){ setDsub("catalogo"); });
  // Audit H1 (WCAG 2.1.1-A): setDsub's roving tabIndex had no keydown path, so a keyboard
  // user could never leave the active sub-tab. Mirrors the .nav-tabs handler: arrows cycle,
  // Home/End jump, and focus follows the newly active tab (this focus() runs after setDsub's
  // desktop focusCatSearch(), so keyboard navigation stays on the tablist).
  var DSUB_ORDER = ["detectados", "catalogo", "ativos"];
  var dsubList = document.querySelector(".viewToggle.dsub");
  if(dsubList) dsubList.addEventListener("keydown", function(e){
    if(["ArrowRight","ArrowDown","ArrowLeft","ArrowUp","Home","End"].indexOf(e.key) === -1) return;
    e.preventDefault();
    var i = DSUB_ORDER.indexOf(dsubCur);
    if(e.key === "ArrowRight" || e.key === "ArrowDown") i = (i + 1) % DSUB_ORDER.length;
    else if(e.key === "ArrowLeft" || e.key === "ArrowUp") i = (i - 1 + DSUB_ORDER.length) % DSUB_ORDER.length;
    else if(e.key === "Home") i = 0;
    else i = DSUB_ORDER.length - 1;
    setDsub(DSUB_ORDER[i]);
    if(dsubBtns[DSUB_ORDER[i]]) dsubBtns[DSUB_ORDER[i]].focus();
  });
  var dsubInit = null;
  try{ dsubInit = localStorage.getItem(DSUB_KEY); }catch(e){}
  if(!dsubViews[dsubInit]) dsubInit = M === "simulated" ? "catalogo" : M === "companion" ? "detectados" : "ativos";
  setDsub(dsubInit, { silent: true });   // wires the tab buttons/views without revealing Advanced

  // the catalog loads on first entry into the Devices tab (single fetch, cached) — still
  // preloads with Advanced collapsed, since the Simple wizard's matchDevice() cascade needs it.
  (function(){
    var p = $("panel-dispositivos");
    if(!p) return;
    new MutationObserver(function(){
      if(!p.classList.contains("active")) return;
      ensureCatalog();
      if(dsubCur === "catalogo") focusCatSearch();   // v2 search-first (desktop only)
    }).observe(p, { attributes: true, attributeFilter: ["class"] });
    if(p.classList.contains("active")) ensureCatalog();
  })();

  // ---------- devices-tab-simple: "Add a device" wizard (default landing) ----------
  // Reuses matchDevice()/activeCameraIps()/onvifCams (ONVIF scan) + openSetup()/goToCatalog()
  // exactly as the technical Detected view above does — same data, same add actions, only a
  // simpler "is this yours?" presentation. Dismissals are session-only (never persisted),
  // matching the rest of this file's ephemeral-UI-state convention (e.g. bannerDismissed).
  var simpleDismissed = {};
  function simpleKeyOnvif(cam){ return "onvif:" + ((cam && cam.ip) || ""); }
  function simpleKeyNet(d){ return "net:" + ((d && (d.mac || d.ip)) || Math.random()); }

  function buildSimpleCard(opts){
    var card = elc("article", "cap-card");
    var head = elc("div", "cap-head");
    var ic = elc("span", "cap-icon");
    ic.appendChild(icon(opts.icon || "ic-grid"));
    var tw = elc("div");
    tw.appendChild(elc("div", "cap-title", opts.title));
    tw.appendChild(elc("div", "cap-brand", opts.sub));
    head.appendChild(ic); head.appendChild(tw);
    card.appendChild(head);
    card.appendChild(elc("p", "dev-simple-q", WavrT("Is this yours?")));
    var row = elc("div", "cap-cta");   // inherits the shared .cap-cta .ctl.primary green rule
    var yes = elc("button", "ctl small primary", WavrT("Yes, that's mine"));
    yes.type = "button";
    yes.onclick = opts.onYes;
    var no = elc("button", "ctl small off", WavrT("Not mine"));
    no.type = "button";
    no.onclick = function(){ simpleDismissed[opts.key] = true; renderDevSimpleResults(); };
    row.appendChild(yes); row.appendChild(no);
    card.appendChild(row);
    return card;
  }

  function renderDevSimpleResults(){
    var list = $("devSimpleList"), h = $("devSimpleResultsH"), rescan = $("devSimpleRescan");
    if(!list || !h) return;
    var frag = document.createDocumentFragment();
    var count = 0;
    onvifCams.forEach(function(cam){
      var key = simpleKeyOnvif(cam);
      if(simpleDismissed[key]) return;
      count++;
      frag.appendChild(buildSimpleCard({
        key: key, icon: "ic-camera", title: onvifTitle(cam),
        sub: WavrT("Camera found on your Wi-Fi") + (cam.ip ? " · " + cam.ip : ""),
        onYes: function(ev){ openSetup(onvifCatalogEntry(cam),
          { ip: cam.ip || "", onvif: cam, focus: "form", trigger: ev.currentTarget }); }
      }));
    });
    if(CAT && invDevices.length){
      var usedIps = activeCameraIps();
      var onvifIps = {};
      onvifCams.forEach(function(c){ if(c && c.ip) onvifIps[c.ip] = 1; });
      invDevices.forEach(function(d){
        if(!d || typeof d !== "object") return;
        if(d.ip && (usedIps.has(d.ip) || onvifIps[d.ip])) return;
        var m = matchDevice(d);
        if(m.conf !== "alta" && m.conf !== "media") return;
        var key = simpleKeyNet(d);
        if(simpleDismissed[key]) return;
        count++;
        var sku = (m.cands && m.cands[0]) || null;
        var catLbl = tmap(CAT_EN, m.cat, "device");
        frag.appendChild(buildSimpleCard({
          key: key, icon: CAT_ICON[m.cat] || "ic-grid",
          title: m.conf === "alta" ? (sku ? sku.name : catLbl) : WavrT("Likely a {what}", { what: catLbl }),
          sub: (d.vendor && d.vendor !== "unknown" ? d.vendor + " · " : "") + WavrT("Seen on your Wi-Fi"),
          onYes: function(ev){
            if(sku) openSetup(sku, { ip: d.ip || "", focus: rungFor(sku) === 2 ? "form" : "verify", trigger: ev.currentTarget });
            else goToCatalog({ category: m.cat || "", search: "" });
          }
        }));
      });
    }
    list.textContent = "";
    list.appendChild(frag);
    list.hidden = !count;
    var can = canDetectNow();
    if(count){
      h.textContent = WavrT("Found {n} device — tap the one that's yours.|Found {n} devices — tap the one that's yours.",
                            { n: count });
    } else if(!can){
      // Same split as `renderDetectados` above: an unpaired companion is not a
      // demo, and what it needs is the pairing step, not a hub it already has.
      h.textContent = (M === "companion")
        ? WavrT("Pair this device with your Wavr hub to see the devices it finds.")
        : WavrT("This demo can't scan your network — with a Wavr hub connected, found devices " +
          "would show up here. “Advanced” below shows exactly how, with an example.");
    } else {
      h.textContent = WavrT("Nothing new found yet. Make sure the device is powered on and connected, then " +
        "look again — or browse the full list below.");
    }
    if(rescan) rescan.hidden = false;
  }

  function runSimpleScan(){
    var intro = $("devSimpleIntro"), scanBox = $("devSimpleScan"), res = $("devSimpleResults");
    if(!canDetectNow()){
      // Honest: nothing actually scans off-hub — skip the fake "looking…" delay entirely.
      if(intro) intro.hidden = true;
      if(scanBox) scanBox.hidden = true;
      if(res) res.hidden = false;
      renderDevSimpleResults();
      return;
    }
    if(intro) intro.hidden = true;
    if(res) res.hidden = true;
    if(scanBox) scanBox.hidden = false;
    var finish = function(){
      if(scanBox) scanBox.hidden = true;
      if(res) res.hidden = false;
      renderDevSimpleResults();
    };
    Promise.resolve(ensureCatalog()).then(function(){ return scanCameras(); }).then(finish).catch(finish);
  }
  var simpleStartBtn = $("devSimpleStart");
  if(simpleStartBtn) simpleStartBtn.addEventListener("click", runSimpleScan);
  var simpleRescanBtn = $("devSimpleRescan");
  if(simpleRescanBtn) simpleRescanBtn.addEventListener("click", runSimpleScan);

  // Approve-on-Core integration: route "pairing a phone/tablet" to the ALREADY-BUILT flow
  // (the #pairApprove banner if a request is pending, else Settings -> Devices -> pairing
  // code) instead of duplicating it inside this wizard.
  function irParaOPareamento(){
    var pa = document.getElementById("pairApprove");
    if(pa && !pa.hidden){ pa.scrollIntoView({ block: "center" }); return; }
    var gearBtn = document.getElementById("gearTopBtn") || document.getElementById("gearNavBtn");
    if(gearBtn) gearBtn.click();
    setTimeout(function(){
      // Whichever of the pair is actually on screen. With LAN access off the
      // pairing panel is hidden and `#pairingOff` stands in its place, and
      // scrolling to a hidden element does nothing at all -- the overlay would
      // open at the top and the person would be left looking for a panel that
      // is not there, one screen deeper than where they started.
      var alvo = document.getElementById("pairing");
      if(!alvo || alvo.hidden) alvo = document.getElementById("pairingOff");
      if(alvo && !alvo.hidden) alvo.scrollIntoView({ block: "start" });
    }, 60);
  }

  // Two buttons, one behaviour: the tile at the top of this screen and the
  // footnote inside the sensor wizard. Both delegate to the pairing flow that
  // already exists rather than growing a second one.
  ["devSimplePairLink", "phoneTilePair"].forEach(function(id){
    var b = $(id);
    if(b) b.addEventListener("click", irParaOPareamento);
  });

  var advToggleBtn = $("devAdvancedToggle"), advWrapBox = $("devAdvancedWrap");
  if(advToggleBtn && advWrapBox) advToggleBtn.addEventListener("click", function(){
    var show = advWrapBox.hidden;
    advWrapBox.hidden = !show;
    advToggleBtn.setAttribute("aria-expanded", show ? "true" : "false");
    advToggleBtn.textContent = show ? WavrT("Hide the full device list") : WavrT("Advanced: browse the full device list");
    if(show) setDsub(dsubCur);
  });
})();
