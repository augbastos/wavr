// ==========================================================================
// network.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ---- Wavr Net panel (Plano A / live only): device inventory + rogue alerts ----
const MIDDOT = "·";   // shared separator glyph
function maskMac(mac){
  // Show the vendor-OUI half, mask the device-specific half (privacy).
  if(typeof mac!=="string" || !mac) return "—";
  const p = mac.split(":");
  if(p.length===6) return p.slice(0,3).join(":")+":··:··:··";
  const keep = Math.max(2, Math.ceil(mac.length/2));
  return mac.slice(0,keep) + "·".repeat(Math.max(2, mac.length-keep));
}
// ---- v2 device auto-ID: fixed 18-type taxonomy → original icon + English label ----
// Honesty rule: only type_confidence === "high" renders as fact; medium/low get the
// dashed/dim "guessed" treatment and low adds an explicit "?" affordance. These are
// top-level lexical bindings — readable from the later script blocks (same pattern as MODE).
const DTYPE_ICON = { router:"ic-net", gateway:"ic-hub", phone:"ic-phone", tablet:"ic-tablet",
  laptop:"ic-laptop", desktop:"ic-desktop", tv:"ic-tv", streaming_stick:"ic-stick",
  speaker:"ic-sound", camera:"ic-camera", printer:"ic-printer", nas:"ic-nas",
  console:"ic-console", iot_sensor:"ic-thermo", esp_dev:"ic-chip", smart_plug:"ic-plug",
  wearable:"ic-watch", unknown:"ic-qmark" };
const DTYPE_LABEL = { router:"Router", gateway:"Hub / gateway", phone:"Phone", tablet:"Tablet",
  laptop:"Laptop", desktop:"Desktop", tv:"TV", streaming_stick:"Streaming stick",
  speaker:"Speaker", camera:"Camera", printer:"Printer", nas:"NAS", console:"Game console",
  iot_sensor:"IoT sensor", esp_dev:"ESP / DIY", smart_plug:"Smart plug", wearable:"Wearable",
  unknown:"Unknown device" };
function dtypeIcon(t){ return DTYPE_ICON[t] || "ic-qmark"; }
// DTYPE_LABEL holds the English for anything that needs the source word (features.js builds
// its type-pin <select> from it). Reading a screen goes through dtypeLabel(), which spells
// every label out as a LITERAL inside WavrT(): a WavrT(variable) is invisible to the
// catalogue's completeness check, and an undeclared label never gets translated at all.
// A legacy type the taxonomy has never heard of passes through — data, not a sentence.
function dtypeLabel(t){
  switch(t){
    case "router":          return WavrT("Router");
    case "gateway":         return WavrT("Hub / gateway");
    case "phone":           return WavrT("Phone");
    case "tablet":          return WavrT("Tablet");
    case "laptop":          return WavrT("Laptop");
    case "desktop":         return WavrT("Desktop");
    case "tv":              return WavrT("TV");
    case "streaming_stick": return WavrT("Streaming stick");
    case "speaker":         return WavrT("Speaker");
    case "camera":          return WavrT("Camera");
    case "printer":         return WavrT("Printer");
    case "nas":             return WavrT("NAS");
    case "console":         return WavrT("Game console");
    case "iot_sensor":      return WavrT("IoT sensor");
    case "esp_dev":         return WavrT("ESP / DIY");
    case "smart_plug":      return WavrT("Smart plug");
    case "wearable":        return WavrT("Wearable");
    case "unknown":         return WavrT("Unknown device");
    default:                return t ? String(t) : WavrT("Unknown device");
  }
}
// The severity ladder the backend stamps on every alert (info < note < watch < alert <
// critical). Same rule as above: the badge shows a word a person reads, so each one is a
// literal here; the CSS class keeps using the raw enum.
function sevLabel(sev){
  switch(sev){
    case "info":     return WavrT("info");
    case "note":     return WavrT("note");
    case "watch":    return WavrT("watch");
    case "alert":    return WavrT("alert");
    case "critical": return WavrT("critical");
    default:         return sev;
  }
}
function dtypeIconEl(t, conf, small, rogue){
  const wrap = document.createElement("span");
  // Audit M1 (frontend half): a rogue/unknown device (known === false) must NEVER get the
  // solid "confirmed fact" treatment — its identity signals (OUI, hostname) are attacker-chosen.
  wrap.className = "dev-ico" + (conf === "high" && !rogue ? "" : " guessed") + (small ? " sm" : "");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "#" + dtypeIcon(t));
  svg.appendChild(use); wrap.appendChild(svg);
  return wrap;
}
function dtypeLabelEl(t, conf, rogue){
  // Audit M1 (frontend half): cap the rendered confidence of rogue/unknown devices at
  // "probable" — a spoofed OUI must never paint a solid "Camera (high confidence)" during
  // rogue triage, whatever the backend reports. (Backend caps OUI-alone at medium too.)
  if(rogue && conf === "high") conf = "medium";
  const s = document.createElement("span");
  s.className = "dtype-lbl" + (conf === "high" ? "" : " guess");
  s.textContent = dtypeLabel(t);               // textContent only — device data is untrusted
  if(conf === "medium"){                        // UX (#11): title-only tooltips are invisible on
    s.title = WavrT("probable type — not confirmed");   // touch — give medium the same visible "?" cue low has
    const q = document.createElement("span");
    q.className = "dtype-q"; q.setAttribute("aria-hidden", "true"); q.textContent = "?";
    s.appendChild(q);
    const sr = document.createElement("span");
    sr.className = "sr-only"; sr.textContent = WavrT(" (probable, not confirmed)");
    s.appendChild(sr);
  } else if(conf !== "high"){                  // low (or missing) → explicit guess affordance
    s.title = WavrT("guessed from weak signals");
    const q = document.createElement("span");
    q.className = "dtype-q"; q.setAttribute("aria-hidden", "true"); q.textContent = "?";
    s.appendChild(q);
    const sr = document.createElement("span");
    sr.className = "sr-only"; sr.textContent = WavrT(" (guessed)");
    s.appendChild(sr);
  }
  return s;
}
// ---- Alert explain/dismiss: shared by the Network tab's own panel (initNetAlertsPop, all
// modes) and, when running in ?core kiosk mode, the Core Panel's ambient badge (openAlertsPop
// further down reuses alertExplain()/buildAlertItem instead of keeping its own copy). Plain
// top-level declarations so both script blocks share them via window, no window.* export needed.
// Each explanation is a WavrT LITERAL rather than a table the render site looks up with a
// variable — that is the only shape the catalogue's completeness check can see, and it is
// evaluated per render, so a language change repaints these too.
function alertExplain(kind){
  switch(kind){
    case "gateway_identity":
      return WavrT("Your router answered from a different hardware address. Usually a reboot or a swapped router — but it can also mean someone is impersonating your gateway.");
    case "rogue_dhcp":
      return WavrT("Another device is handing out network addresses like a router. Usually a second router or a misconfiguration; occasionally an attack.");
    case "rogue_device":
      return WavrT("A device we haven't seen on this network before just showed up.");
    case "fall_suspected":
      return WavrT("Someone may have been lying down outside a bed or rest area for a while. This is a research demonstration, not a certified medical or fall-detection device (ADR-0003) -- treat it as a prompt to check in, never a diagnosis.");
    case "intrusion":
      return WavrT("Watch counted more people in a room, or in the Space overall, than it knows are here. It never reveals who or exactly where in the room -- only that someone unaccounted-for is present.");
    default:
      return WavrT("A network alert.");
  }
}
function alertKeyOf(a){
  var kind = (a && a.kind) || "rogue_device";
  return kind + "|" + ((a && a.ts) || "") + "|" +
    ((a && (a.observed_mac || a.extra_server || a.mac || a.ip || a.gateway_ip ||
     (typeof a.room === "string" ? a.room : ""))) || "");
}
// Client-only dismissal (no backend endpoint for this): persisted in localStorage so an
// acknowledged alert instance (same kind+ts+identity key) is filtered out of every alert
// surface until the backend produces a genuinely new occurrence (a different ts -> a new key).
var ALERT_DISMISS_KEY = "wavr.alerts.dismissed.v1", ALERT_DISMISS_MAX = 300;
function loadDismissedAlerts(){
  try{ var v = JSON.parse(localStorage.getItem(ALERT_DISMISS_KEY) || "[]"); return Array.isArray(v) ? v : []; }
  catch(e){ return []; }
}
function isAlertDismissed(key){ return !!key && loadDismissedAlerts().indexOf(key) !== -1; }
function dismissAlertKey(key){
  if(!key) return;
  var cur = loadDismissedAlerts();
  if(cur.indexOf(key) === -1) cur.push(key);
  if(cur.length > ALERT_DISMISS_MAX) cur = cur.slice(cur.length - ALERT_DISMISS_MAX);
  try{ localStorage.setItem(ALERT_DISMISS_KEY, JSON.stringify(cur)); }catch(e){}
}
// One #alertList row -> one explain+dismiss item, shared by both panels above.
// opts.onOpen(row) fires when the item body is tapped (each caller defines what "open" means);
// opts.onDismissed(key) fires after a successful dismiss so the caller can re-render/re-count.
function buildAlertItem(row, opts){
  var kind = row.dataset.kind || "rogue_device";
  var key  = row.dataset.alertKey || "";
  var sev  = (row.className.match(/sev-(\w+)/) || [])[1] || "note";
  var titleEl = row.querySelector(".at");
  var titleTxt = titleEl ? titleEl.textContent : WavrT("Alert");
  var item = document.createElement("div");
  item.className = "core-alert-item sev-" + sev;
  var openBtn = document.createElement("button");
  openBtn.type = "button"; openBtn.className = "core-alert-open";
  var dot = document.createElement("span"); dot.className = "core-alert-dot"; openBtn.appendChild(dot);
  var txt = document.createElement("span"); txt.className = "core-alert-txt";
  var t = document.createElement("span"); t.className = "core-alert-title";
  t.textContent = titleTxt;   // textContent — untrusted network data
  var w = document.createElement("span"); w.className = "core-alert-why";
  w.textContent = alertExplain(kind);
  txt.appendChild(t); txt.appendChild(w); openBtn.appendChild(txt);
  var go = document.createElement("span"); go.className = "core-alert-go"; go.textContent = "›"; openBtn.appendChild(go);
  openBtn.addEventListener("click", function(){ if(opts && opts.onOpen) opts.onOpen(row); });
  item.appendChild(openBtn);
  var actions = document.createElement("div"); actions.className = "core-alert-actions";
  var ackBtn = document.createElement("button");
  ackBtn.type = "button"; ackBtn.className = "core-alert-dismiss";
  ackBtn.textContent = WavrT("Dismiss");
  ackBtn.setAttribute("aria-label", WavrT("Dismiss alert: {title}", {title: titleTxt}));
  ackBtn.addEventListener("click", function(ev){
    ev.stopPropagation();
    dismissAlertKey(key);
    if(row.parentNode) row.parentNode.removeChild(row);
    item.remove();
    if(opts && opts.onDismissed) opts.onDismissed(key);
  });
  actions.appendChild(ackBtn);
  item.appendChild(actions);
  return item;
}

function renderNetwork(){
  // live (central) OR companion viewer (token) — both read the same local backend, read-only.
  if(MODE!=="live" && MODE!=="companion") return;     // never in Plano B (demo)
  if(MODE==="companion" && !companionToken()) return; // companion pairing state: no token yet
  const auth = MODE==="companion" ? {"Authorization":"Bearer "+companionToken()} : null;  // companion-only
  document.getElementById("network").hidden = false;
  const devList = document.getElementById("devList");
  const alertList = document.getElementById("alertList");
  const hint = document.getElementById("netHint");
  const devFb = document.getElementById("devFb");
  const trustAllBtn = document.getElementById("trustAllBtn");
  const trustAllFb = document.getElementById("trustAllFb");
  // Device-rename edit state (item: device naming) — kept outside refresh() so the 15s poll
  // doesn't wipe an in-progress edit; only one device can be renamed at a time.
  let editingMac = null, editingValue = "", editingJustOpened = false;
  // Assign-person edit state (known-device-ui, 2026-07-11) — same one-row-at-a-time pattern
  // as the rename state above; the identity registry it writes is the SAME consent registry
  // wavr.known_presence composes house-level "who's home" over (see renderKnownPresence()).
  let assigningMac = null, assigningValue = "";
  // Scale (airport-grade LANs): cache the last inventory so the search box can
  // re-filter + re-render from memory with NO re-fetch, and CAP how many device
  // rows we ever build into the DOM. Rendering every device was ~17 nodes each --
  // at 5000 devices that is ~85k DOM nodes torn down and rebuilt every 15s, which
  // froze the tab. Now we build at most DEV_RENDER_CAP rows and let search reach
  // the rest. Covers every real home unchanged (homes rarely exceed ~150 devices).
  let _lastDevices = [], _lastRogue = [], _lastNetId = new Map();
  let _netMisses = 0;
  const NET_TIMEOUT_MS = 8000;

  /* A fetch init with a deadline on it.
   *
   * These two reads go through `__nf` rather than `WavrAPI.fetch`, because on
   * the Capacitor build they must use the shim's pinned fetch — so the
   * deadline is attached here instead of being inherited.
   */
  function withDeadline(init){
    var out = Object.assign({}, init || {});
    try{
      var ctl = new AbortController();
      out.signal = ctl.signal;
      setTimeout(function(){ ctl.abort(); }, NET_TIMEOUT_MS);
    }catch(e){ /* no AbortController: the miss counter is still the backstop */ }
    return out;
  }

  /* What the Network tab says when it could not read the Core.
   *
   * It EMPTIES the lists rather than leaving them, and marks the alert list so
   * the Core Panel — which derives its wave and its alert count from this DOM
   * — can tell "nothing is wrong" from "nobody answered". Keeping the last
   * view is what made a wall panel show a calm green wave and a frozen alert
   * count over a Core that had stopped minutes earlier.
   */
  function netCannotTell(){
    // The SAME sentence the house-status tile says for the same situation.
    // Two wordings for one condition is how a product ends up sounding like
    // two products.
    var note = WavrT("Wavr is not answering, so this cannot be checked. "
                     + "Check that the Core is running.");
    var alerts = document.getElementById("alertList");
    if(alerts){
      alerts.dataset.unknown = "1";
      alerts.textContent = "";
      var a = document.createElement("div");
      a.className = "empty"; a.textContent = note;
      alerts.appendChild(a);
    }
    var devs = document.getElementById("devList");
    if(devs){
      devs.textContent = "";
      var d = document.createElement("div");
      d.className = "empty"; d.textContent = note;
      devs.appendChild(d);
    }
  }

  const DEV_RENDER_CAP = 500;
  const devCountNote = document.getElementById("devCountNote");
  function devSearchText(d){
    return [d.name, d.display_name, d.hostname, d.vendor, d.device_type, d.ip, d.mac]
      .filter(Boolean).join(" ").toLowerCase();
  }
  async function refresh(){
    let inv, al, identityDevices = [];
    // Mobile: route the central reads through the native pinned fetch (base = stored central) so the
    // Bearer token never reaches the app's own https://localhost; the `auth` header logic is unchanged.
    // Absent the hook, __nf is exactly fetch(location.origin+path, opt) — original behavior verbatim.
    const __nf = (path, opt)=> window.WAVR_MOBILE
      ? window.WAVR_MOBILE.netFetch(window.WAVR_MOBILE.base + path, opt)
      : fetch(location.origin + path, opt);
    try{
      // A DEADLINE, because a Core that goes dark never settles a fetch: it
      // does not refuse, it says nothing, and this poll then never reaches its
      // render. What it used to do next was "keep last view" — and the wall
      // panel reads this very list out of the DOM to choose its wave, so a
      // frozen `#alertList` was painted calm green over a Core that had been
      // gone for minutes.
      const ri = await __nf("/api/inventory", withDeadline(auth?{headers:auth}:undefined));
      const ra = await __nf("/api/alerts", withDeadline(auth?{headers:auth}:undefined));
      if(auth && (ri.status===401||ri.status===403||ra.status===401||ra.status===403)){ companionAuthFailed(); return; }
      inv = await ri.json();
      al  = await ra.json();
      _netMisses = 0;
      var _al = document.getElementById("alertList");
      if(_al) delete _al.dataset.unknown;
    }catch{
      // Two misses, then say so. One dropped request is not a dead Core, and
      // flapping a wall panel on every hiccup is how a real warning stops
      // being read — the same two-miss rule the house-status tile uses.
      _netMisses += 1;
      if(_netMisses >= 2) netCannotTell();
      return;
    }
    // Consent-first identity registry (known-device-ui, 2026-07-11) — live/central only, so
    // each network row can show "registered to <person>" instead of the generic assign
    // control. A 403 (non-central peer/companion) or thrown fetch just leaves every row
    // unregistered-looking — never a hard failure of the inventory poll above.
    if(MODE==="live"){
      try{
        const rIdentity = await WavrAPI.fetch("/api/identity/devices");
        if(rIdentity.ok){
          const idJson = await rIdentity.json();
          identityDevices = Array.isArray(idJson.devices) ? idJson.devices : [];
        }
      }catch{}
    }
    const netIdentityByMac = new Map();
    identityDevices.forEach(d => { if(d && d.source==="network" && d.address) netIdentityByMac.set(String(d.address).toLowerCase(), d); });
    const devices = Array.isArray(inv && inv.devices) ? inv.devices : [];
    // Drop alerts the operator already dismissed (client-persisted) BEFORE they ever become
    // rows -- keeps #alertList, the "(n)" badges and the "no recent" empty state all honest
    // without needing a separate hide-pass; a genuinely new occurrence gets a new key and
    // reappears normally.
    const rogue   = (Array.isArray(al && al.alerts) ? al.alerts : []).filter(a => !isAlertDismissed(alertKeyOf(a)));
    _lastDevices = devices; _lastRogue = rogue; _lastNetId = netIdentityByMac;
    repaint();
  }
  // Render the cached inventory + alerts. Called by refresh() after a fetch AND by the
  // search box (window.__wavrRepaintDev) to re-filter from memory with NO re-fetch --
  // so typing in search never re-pulls the (potentially multi-MB) inventory payload.
  function repaint(){
    const devices = _lastDevices, rogue = _lastRogue, netIdentityByMac = _lastNetId;
    // E-front: bulk-trust — only counts kind==='rogue_device' sightings (a real MAC each),
    // same restriction as the per-row "this is mine" button below; gateway_identity/rogue_dhcp
    // are network-level events with no single device to allowlist. Live-only for now, same
    // X-Wavr-Local CSRF gate as the per-row button (both call the identical backend name_deps).
    const rogueDeviceN = rogue.filter(a => (a.kind || "rogue_device") === "rogue_device").length;
    if(trustAllBtn){
      if(MODE==="live" && rogueDeviceN > 0){
        trustAllBtn.hidden = false;
        trustAllBtn.textContent = WavrT("Trust all {n} device shown|Trust all {n} devices shown", {n: rogueDeviceN});
        trustAllBtn.setAttribute("aria-label", WavrT("mark all {n} unknown devices as known", {n: rogueDeviceN}));
        trustAllBtn.onclick = async ()=>{
          trustAllBtn.disabled = true;
          let r;
          try{
            r = await WavrAPI.fetch("/api/inventory/known/bulk", {method: "POST"});
          }catch{}
          if(r && r.ok){
            let marked = 0;
            try{ marked = (await r.json()).marked ?? 0; }catch{}
            actionFeedback(trustAllFb, true, null, WavrT("✓ marked {n} known", {n: marked}));
            refresh();
          } else {
            trustAllBtn.disabled = false;
            actionFeedback(trustAllFb, false, WavrT("update failed"));
          }
        };
      } else {
        trustAllBtn.hidden = true;
        trustAllBtn.onclick = null;
      }
    }
    window.__wavrInventory?.(devices);   // Stage-2 hook: Detected matches this SAME payload against the catalog
    // Inventory is opt-in (WAVR_NET_INVENTORY=1); an empty list means it isn't collecting.
    hint.hidden = devices.length > 0;
    // Filter (from the search box, in memory) then CAP, then build into ONE
    // DocumentFragment appended in a single reflow -- never 5000 live appendChilds.
    const _q = (window.__wavrDevQ || "");
    const _matches = _q ? devices.filter(d => devSearchText(d).indexOf(_q) >= 0) : devices;
    const _shown = _matches.slice(0, DEV_RENDER_CAP);
    devList.textContent = "";
    const _frag = document.createDocumentFragment();
    _shown.forEach(d => {
      const row = document.createElement("div"); row.className = "dev-row";
      // Stage 3b (§7#8/#13): row addressable for the profile drill-down + search. The full
      // MAC rides a data attribute only — the display stays masked, reveal is on-demand.
      if(d.mac){
        row.dataset.mac = d.mac; row.tabIndex = 0;
        // Audit M3: the row is an expandable-profile disclosure — role + initial state at
        // CREATION (the Stage-3b drill-down block only re-stamps aria-expanded on toggle).
        row.setAttribute("role", "button");
        row.setAttribute("aria-expanded", "false");
      }
      const firstSeenShort = fmtShortDate(d.first_seen);
      if(firstSeenShort) row.title = WavrT("first seen: {when}", {when: firstSeenShort});   // tooltip keeps the row compact
      const left = document.createElement("div"); left.className = "dev-left";
      // Name line: custom name shown prominently (bold) with vendor/type demoted to a
      // secondary line; falls back to vendor/type as the main text when no name is set.
      const nameLine = document.createElement("div"); nameLine.className = "dev-name-line";
      const main = document.createElement("span");
      // v2: device_type is the backend's fixed taxonomy → English label + confidence-honest cue
      const vendorType = (d.vendor || WavrT("unknown")) + " · " + dtypeLabel(d.device_type);  // plain-text form (aria)
      // Resolved-identity priority: a user-set name wins; otherwise the device's own
      // self-announced/PTR-resolved hostname, CLEANED for display (display_name strips the
      // router's DHCP search-domain suffix, e.g. ".lan.gateway", and prettifies
      // separators server-side -- wavr.data.deviceclass.display_hostname; falls back to the
      // raw hostname if display_name is ever absent, still a real identity, just not
      // user-confirmed); only when NONE of the three is known does the row fall back to the
      // honest vendor+type guess.
      const resolvedIdentity = d.name || d.display_name || d.hostname || null;
      if(resolvedIdentity){
        main.className = "dev-main named"; main.textContent = resolvedIdentity;   // textContent only — XSS-safe
        const sub = document.createElement("span"); sub.className = "dev-sub";
        sub.textContent = " · " + (d.vendor || WavrT("unknown")) + " · ";
        sub.appendChild(dtypeLabelEl(d.device_type, d.type_confidence, d.known === false));
        nameLine.appendChild(main); nameLine.appendChild(sub);
      } else {
        main.className = "dev-main"; main.textContent = (d.vendor || WavrT("unknown")) + " · ";
        main.appendChild(dtypeLabelEl(d.device_type, d.type_confidence, d.known === false));
        nameLine.appendChild(main);
      }
      left.appendChild(nameLine);
      // IP/MAC + last-seen line (compact, inline; first-seen lives in the row tooltip above).
      const metaLine = document.createElement("div"); metaLine.className = "dev-meta-line";
      const meta = document.createElement("span"); meta.className = "dev-meta";
      meta.textContent = (d.ip || WavrT("no IP")) + " · ";
      const mac = document.createElement("span"); mac.className = "mac";
      mac.textContent = maskMac(d.mac);      // textContent only — never innerHTML with device data
      metaLine.appendChild(meta); metaLine.appendChild(mac);
      const lastRel = fmtRelative(d.last_seen);
      if(lastRel){
        const seen = document.createElement("span"); seen.className = "dev-seen";
        seen.textContent = " · " + WavrT("seen: {when}", {when: lastRel});
        metaLine.appendChild(seen);
      }
      left.appendChild(metaLine);
      // Rename affordance — live/central only: PUT /api/inventory/name needs the X-Wavr-Local
      // CSRF header, which companion viewers (bearer-token auth) don't send.
      if(MODE==="live"){
        if(editingMac === d.mac){
          const form = document.createElement("form"); form.className = "dev-rename-form";
          const input = document.createElement("input");
          input.type = "text"; input.maxLength = 64; input.value = editingValue;
          input.setAttribute("aria-label", WavrT("new device name"));
          input.oninput = ()=>{ editingValue = input.value; };
          const save = document.createElement("button");
          save.type = "submit"; save.className = "ctl small"; save.textContent = WavrT("Save");
          const cancel = document.createElement("button");
          cancel.type = "button"; cancel.className = "ctl small off"; cancel.textContent = WavrT("Cancel");
          cancel.onclick = ()=>{ editingMac = null; editingValue = ""; refresh(); };
          form.onsubmit = async (e)=>{
            e.preventDefault();
            const newName = input.value.trim();
            let r;
            try{
              r = await WavrAPI.fetch("/api/inventory/name", {method: "PUT", json: {mac:d.mac, name:newName}});
            }catch{}
            if(r && r.ok){
              editingMac = null; editingValue = "";
              actionFeedback(devFb, true, null, WavrT("✓ renamed"));
              refresh();
            } else {
              let msg = WavrT("rename failed");
              try{ const j = await r.json(); if(j && (j.detail || j.error)) msg = String(j.detail || j.error); }catch{}
              actionFeedback(devFb, false, msg);
            }
          };
          form.appendChild(input); form.appendChild(save); form.appendChild(cancel);
          left.appendChild(form);
          if(editingJustOpened){ editingJustOpened = false; queueMicrotask(()=>{ input.focus(); input.select(); }); }
        } else {
          const rn = document.createElement("button");
          rn.type = "button"; rn.className = "dev-rename-btn"; rn.textContent = WavrT("✎ rename");
          rn.setAttribute("aria-label", WavrT("rename {what}", {what: resolvedIdentity || vendorType}));
          rn.onclick = ()=>{ editingMac = d.mac; editingValue = d.name || ""; editingJustOpened = true; refresh(); };
          left.appendChild(rn);
        }
        // Known/unfamiliar toggle — POST /api/inventory/known (same X-Wavr-Local CSRF rule as
        // rename/type-pin). known:true immediately drops any rogue_device alert for this MAC
        // (backend's apply_known_change); known:false re-arms it. Independent of the rename
        // edit-state above so it stays visible while a rename is in progress.
        if(d.mac){
          const knownBtn = document.createElement("button");
          knownBtn.type = "button"; knownBtn.className = "dev-rename-btn dev-known-btn";
          knownBtn.textContent = d.known ? WavrT("↺ mark as unfamiliar") : WavrT("✓ this is mine");
          // One key per whole clause, with the device in a slot: gluing "mark " to a name and
          // then to " as known" only ever reads right in English.
          knownBtn.setAttribute("aria-label", d.known
            ? WavrT("mark {what} as unfamiliar", {what: resolvedIdentity || vendorType})
            : WavrT("mark {what} as known", {what: resolvedIdentity || vendorType}));
          knownBtn.onclick = async ()=>{
            knownBtn.disabled = true;
            const nextKnown = !d.known;
            let r;
            try{
              r = await WavrAPI.fetch("/api/inventory/known", {method: "POST", json: {mac:d.mac, known:nextKnown}});
            }catch{}
            if(r && r.ok){
              actionFeedback(devFb, true, null, nextKnown ? WavrT("✓ marked known") : WavrT("✓ marked unfamiliar"));
              refresh();
            } else {
              let msg = WavrT("update failed");
              try{ const j = await r.json(); if(j && (j.detail || j.error)) msg = String(j.detail || j.error); }catch{}
              actionFeedback(devFb, false, msg);
              knownBtn.disabled = false;
            }
          };
          left.appendChild(knownBtn);
        }
        // Assign person — consent-first identity registry (known-device-ui, 2026-07-11). A
        // SEPARATE opt-in from the known/unfamiliar allowlist above: this attributes the
        // device to a named person and lets it corroborate house-level who's-home (see
        // renderKnownPresence() / wavr.known_presence) — never automatic. POST/DELETE
        // /api/identity/devices, the SAME consent registry known-presence composes over.
        // One row assigns at a time (assigningMac), mirroring the rename edit-state above.
        // `person` is PII — every name is rendered via textContent, NEVER innerHTML.
        if(d.mac){
          const owned = netIdentityByMac.get(String(d.mac).toLowerCase());
          if(assigningMac === d.mac){
            const aform = document.createElement("form"); aform.className = "dev-rename-form";
            const ainput = document.createElement("input");
            ainput.type = "text"; ainput.maxLength = 64; ainput.value = assigningValue;
            ainput.placeholder = WavrT("whose device is this?");
            ainput.setAttribute("aria-label", WavrT("person this device belongs to"));
            ainput.oninput = ()=>{ assigningValue = ainput.value; };
            const asave = document.createElement("button");
            asave.type = "submit"; asave.className = "ctl small primary"; asave.textContent = WavrT("Save");
            const acancel = document.createElement("button");
            acancel.type = "button"; acancel.className = "ctl small off"; acancel.textContent = WavrT("Cancel");
            acancel.onclick = ()=>{ assigningMac = null; assigningValue = ""; refresh(); };
            aform.onsubmit = async (e)=>{
              e.preventDefault();
              const person = ainput.value.trim();
              if(!person){ actionFeedback(devFb, false, WavrT("enter a name first")); return; }
              let r;
              try{
                r = await WavrAPI.fetch("/api/identity/devices", {method: "POST", json: {person, devices:[{address:d.mac, source:"network", origin:"manual"}]}});
              }catch{}
              if(r && r.ok){
                assigningMac = null; assigningValue = "";
                actionFeedback(devFb, true, null, WavrT("✓ assigned"));
                refresh();
              } else {
                let msg = WavrT("couldn't assign — check the name");
                try{ const j = await r.json(); if(j && (j.detail || j.error)) msg = String(j.detail || j.error); }catch{}
                actionFeedback(devFb, false, msg);
              }
            };
            aform.appendChild(ainput); aform.appendChild(asave); aform.appendChild(acancel);
            left.appendChild(aform);
          } else if(owned){
            // An ANONYMOUS row (consent "yellow": counted as home, deliberately
            // never named) has no person to show. Every branch here predates that
            // level and assumed a non-empty name, so it rendered a nameless "★ "
            // and an "un-assign " with a blank subject. Name the state instead —
            // "no name" is a CHOICE the owner made, and the UI has to say so.
            const anon = owned.anonymous || !owned.person;
            const who = anon ? WavrT("(anonymous)") : owned.person;
            const ownedTag = document.createElement("span"); ownedTag.className = "dev-rename-btn dev-known-btn";
            ownedTag.textContent = "★ " + who;                        // textContent — PII
            ownedTag.setAttribute("aria-label", anon
              ? WavrT("registered anonymously — presence corroborator, no name")
              : WavrT("registered to {person} — presence corroborator", {person: owned.person}));
            left.appendChild(ownedTag);
            const unassign = document.createElement("button");
            unassign.type = "button"; unassign.className = "dev-rename-btn";
            unassign.textContent = WavrT("un-assign");
            unassign.setAttribute("aria-label", anon
              ? WavrT("un-register this anonymous device — privacy opt-out")
              : WavrT("un-assign {person} from this device — privacy opt-out", {person: owned.person}));
            unassign.onclick = async ()=>{
              unassign.disabled = true;
              let r;
              try{
                r = await WavrAPI.fetch("/api/identity/devices/"+encodeURIComponent(d.mac), {method: "DELETE"});
              }catch{}
              if(r && r.ok){ actionFeedback(devFb, true, null, WavrT("✓ un-assigned")); refresh(); }
              else { actionFeedback(devFb, false, WavrT("couldn't un-assign")); unassign.disabled = false; }
            };
            left.appendChild(unassign);
          } else {
            const assignBtn = document.createElement("button");
            assignBtn.type = "button"; assignBtn.className = "dev-rename-btn";
            assignBtn.textContent = WavrT("+ assign person");
            assignBtn.setAttribute("aria-label",
              WavrT("assign a person to {what} — Space-level presence, opt-in",
                    {what: resolvedIdentity || vendorType}));
            assignBtn.onclick = ()=>{ assigningMac = d.mac; assigningValue = ""; refresh(); };
            left.appendChild(assignBtn);
          }
        }
      }
      const tag = document.createElement("span");
      tag.className = "tag " + (d.known ? "known" : "unknown");
      tag.textContent = d.known ? WavrT("known") : WavrT("unknown");
      row.appendChild(dtypeIconEl(d.device_type, d.type_confidence, false, d.known === false));   // v2: type icon column
      row.appendChild(left); row.appendChild(tag);
      _frag.appendChild(row);
    });
    devList.appendChild(_frag);
    devList.dataset.total = String(devices.length);   // TRUE device count for the top-bar chip (rows are capped)
    // Honest "showing X of N" note whenever the list is capped or a search narrows it,
    // so a capped list never reads as "these are all your devices".
    if(devCountNote){
      if(_matches.length > _shown.length){
        devCountNote.hidden = false;
        devCountNote.textContent = _q
          ? WavrT("Showing {shown} of {total} matches — use search to find a specific device.",
                  {shown: _shown.length, total: _matches.length})
          : WavrT("Showing {shown} of {total} devices — use search to find a specific device.",
                  {shown: _shown.length, total: _matches.length});
      } else if(_q && _matches.length !== devices.length){
        devCountNote.hidden = false;
        devCountNote.textContent = WavrT("{shown} of {total} devices match.",
                                         {shown: _matches.length, total: devices.length});
      } else {
        devCountNote.hidden = true;
      }
    }
    // Rogue alerts newest first (service returns newest last; ISO ts sorts lexicographically).
    rogue.sort((a,b) => String(b.ts||"").localeCompare(String(a.ts||"")));
    alertList.textContent = "";
    if(!rogue.length){
      const e = document.createElement("div"); e.className = "net-hint";
      e.textContent = WavrT("no recent unknown devices");
      alertList.appendChild(e);
    } else rogue.forEach(a => {
      // ONE severity ladder (info<note<watch<alert<critical) drives the badge
      // for EVERY alert kind, so a guest phone (info/note) and a spoofed gateway
      // (alert/critical) never render identically (gateway-identity-rogue-dhcp).
      const sev = (typeof a.severity === "string" && a.severity) ? a.severity : "note";
      const kind = a.kind || "rogue_device";
      const row = document.createElement("div"); row.className = "alert-row sev-" + sev;
      // Stable identity so the Core-panel alert glance-box can deep-link back to THIS row after
      // the list re-renders (kind + timestamp + the strongest identity field the kind carries).
      row.dataset.kind = kind;
      row.dataset.alertKey = alertKeyOf(a);
      const badge = document.createElement("span");
      badge.className = "sev-badge sev-" + sev; badge.textContent = sevLabel(sev);   // textContent — untrusted
      const at = document.createElement("span"); at.className = "at";
      const when = (typeof a.ts==="string" && a.ts.length>=19) ? a.ts.slice(11,19) : (a.ts || "");
      const meta = document.createElement("span"); meta.className = "ameta";
      if(kind === "gateway_identity"){
        // Network-level event: the default gateway's MAC identity changed.
        at.textContent = WavrT("gateway identity changed") + (when ? " " + when : "");
        meta.appendChild(dtypeIconEl("router", "low", true, true));
        meta.appendChild(document.createTextNode(
          WavrT("gateway {ip} now answers from a new MAC", {ip: a.gateway_ip || "?"}) +
          " " + MIDDOT + " " + maskMac(a.observed_mac)));
      } else if(kind === "rogue_dhcp"){
        at.textContent = WavrT("rogue DHCP server") + (when ? " " + when : "");
        meta.appendChild(dtypeIconEl("router", "low", true, true));
        meta.appendChild(document.createTextNode(WavrT("unexpected DHCP server") + " " + MIDDOT + " " + (a.extra_server || "?")));
      } else if(kind === "fall_suspected"){
        // A9 wellbeing / fall-suspected alert (RESEARCH-GRADE, ADR-0003) -- its OWN honest
        // treatment, NEVER a device sighting. Room + how-long + the mandatory disclaimer,
        // all textContent. See wavr.fall_detect.FallAlert.
        at.textContent = WavrT("possible fall - check in") + (when ? " " + when : "");
        const durTxt = (typeof a.duration_s === "number" && isFinite(a.duration_s))
          ? " " + MIDDOT + " " + Math.round(a.duration_s) + "s" : "";
        meta.appendChild(document.createTextNode(
          WavrT("someone may be lying down in {room}", {room: a.room || WavrT("a room")}) + durTxt));
        const disc = document.createElement("span");
        disc.className = "alert-disclaimer";
        disc.textContent = a.disclaimer ||
          WavrT("Research demonstration, not a certified medical or fall-detection device (ADR-0003).");
        meta.appendChild(disc);
      } else if(kind === "intrusion"){
        // Watch/Vigia (A2) edge-triggered "unrecognized person" alert -- its OWN honest
        // treatment, NEVER a phantom device sighting (it carries no mac/vendor/ip at
        // all -- see wavr.watch.IntrusionAlert.to_dict). Count-only: a room name (or
        // no room at all for the room-agnostic house-wide aggregate, room=null) plus
        // the two integers that tripped it -- never a position, identity or geometry.
        // Phrasing matches wavr.house_status._intrusion_reasons so the alert stream,
        // the Watch panel note and the house-status card never disagree.
        const room = (typeof a.room === "string" && a.room) ? a.room : null;
        at.textContent = WavrT("unrecognized person detected") + (when ? " " + when : "");
        meta.appendChild(document.createTextNode(
          room ? WavrT("unrecognized person in {room}", {room: room})
               : WavrT("an unrecognized person is present")));
        if(typeof a.person_count === "number" && typeof a.known_present === "number"){
          meta.appendChild(document.createTextNode(
            " " + MIDDOT + " " + WavrT("{counted} counted, {known} known present",
                                       {counted: a.person_count, known: a.known_present})));
        }
      } else {
        // Per-device rogue sighting (unknown MAC).
        at.textContent = WavrT("new device") + (when ? " " + when : "");
        // v2: same type icon + honest label as the device rows (alerts carry type_confidence too).
        // Audit M1: a rogue alert is BY DEFINITION an unknown device -- always the guessed treatment.
        meta.appendChild(dtypeIconEl(a.device_type, a.type_confidence, true, true));
        meta.appendChild(document.createTextNode((a.vendor || WavrT("unknown")) + " " + MIDDOT + " "));
        meta.appendChild(dtypeLabelEl(a.device_type, a.type_confidence, true));
        const rest = [a.ip||WavrT("no IP"), maskMac(a.mac), a.hostname||""].filter(Boolean).join(" " + MIDDOT + " ");
        meta.appendChild(document.createTextNode(" " + MIDDOT + " " + rest));
      }
      row.appendChild(badge); row.appendChild(at); row.appendChild(meta);
      // Mark-as-known action — live only (needs the X-Wavr-Local CSRF header). Only for
      // rogue_device sightings (a real MAC to mark); gateway_identity/rogue_dhcp are
      // network-level events with no single device to allowlist. On success the backend
      // drops this alert from the very next GET /api/alerts (apply_known_change), so
      // refresh() removing the row IS the confirmation — no separate "gone" state needed.
      if(kind === "rogue_device" && MODE==="live" && a.mac){
        const act = document.createElement("div"); act.className = "alert-actions";
        const mineBtn = document.createElement("button");
        mineBtn.type = "button"; mineBtn.className = "dev-rename-btn dev-known-btn";
        mineBtn.textContent = WavrT("✓ this is mine");
        mineBtn.setAttribute("aria-label",
          WavrT("mark {what} as known", {what: a.hostname || a.vendor || WavrT("this device")}));
        const fb = document.createElement("span"); fb.className = "action-fb"; fb.setAttribute("aria-live", "polite");
        mineBtn.onclick = async ()=>{
          mineBtn.disabled = true;
          let r;
          try{
            r = await WavrAPI.fetch("/api/inventory/known", {method: "POST", json: {mac:a.mac, known:true}});
          }catch{}
          if(r && r.ok){
            actionFeedback(fb, true, null, WavrT("✓ marked known"));
            refresh();
          } else {
            let msg = WavrT("update failed");
            try{ const j = await r.json(); if(j && (j.detail || j.error)) msg = String(j.detail || j.error); }catch{}
            actionFeedback(fb, false, msg);
            mineBtn.disabled = false;
          }
        };
        act.appendChild(mineBtn); act.appendChild(fb);
        row.appendChild(act);
      }
      alertList.appendChild(row);
    });
  }
  window.__wavrNetRefresh = refresh;   // v2: the drill-down type-pin re-renders through this same poll path
  window.__wavrRepaintDev = repaint;   // scale: search box re-filters from cache (no re-fetch)
  refresh(); setInterval(refresh, 15000);
}
if(!window.WAVR_MOBILE) renderNetwork();   // mobile: deferred to the post-ready boot (token cache is empty at parse time)

// ---- Network tab: tap "Security & network alerts (n)" -> explain + dismiss each one, in EVERY mode
// (live/companion), not just the ?core kiosk face -- #netAlertCount previously only got a
// count from Core Panel's CORE_MODE-gated renderCoreAlerts(), so outside that mode it stayed a
// permanently-empty dead span. Runs unconditionally (cheap DOM listener wiring); harmless if
// the Network tab never populates (demo mode never fills #alertList).
function updateNetAlertCount(){
  var badge = document.getElementById("netAlertCount");
  if(!badge) return;
  var list = document.getElementById("alertList");
  var rows = list ? list.querySelectorAll(".alert-row") : [];
  var n = rows.length, hasCritical = false, hasWatch = false;
  for(var i = 0; i < rows.length; i++){
    if(rows[i].classList.contains("sev-critical") || rows[i].classList.contains("sev-alert")) hasCritical = true;
    else if(rows[i].classList.contains("sev-watch")) hasWatch = true;
  }
  badge.textContent = n ? ("(" + n + ")") : "";
  badge.className = "net-alert-count" + (hasCritical ? " has-alert" : hasWatch ? " has-watch" : "");
}
function initNetAlertsPop(){
  var countEl = document.getElementById("netAlertCount");
  var popEl   = document.getElementById("netAlertsPop");
  var listEl  = document.getElementById("netAlertsPopList");
  var ttlEl   = document.getElementById("netAlertsPopTitle");
  var xEl     = document.getElementById("netAlertsPopClose");
  if(!countEl || !popEl || !listEl) return;
  function close(){ popEl.hidden = true; }
  function render(){
    var rows = document.querySelectorAll("#alertList .alert-row");
    listEl.textContent = "";
    if(!rows.length){ close(); return; }
    if(ttlEl) ttlEl.textContent = WavrT("{n} alert|{n} alerts", {n: rows.length});
    Array.prototype.forEach.call(rows, function(row){
      listEl.appendChild(buildAlertItem(row, {
        onOpen: function(r){
          close();
          r.classList.remove("alert-jump"); void r.offsetWidth; r.classList.add("alert-jump");
          try{ r.scrollIntoView({behavior: "smooth", block: "center"}); }catch(e){ r.scrollIntoView(); }
        },
        onDismissed: render   // re-render from the now-shrunk #alertList
      }));
    });
    popEl.hidden = false;
  }
  countEl.setAttribute("data-tappable", "1");
  countEl.setAttribute("role", "button");
  countEl.setAttribute("tabindex", "0");
  countEl.setAttribute("aria-label", WavrT("Alerts"));
  countEl.addEventListener("click", function(ev){
    ev.stopPropagation();
    if(!popEl.hidden){ close(); return; }
    render();
  });
  countEl.addEventListener("keydown", function(ev){
    if(ev.key === "Enter" || ev.key === " "){ ev.preventDefault(); countEl.click(); }
  });
  if(xEl) xEl.addEventListener("click", close);
}
initNetAlertsPop();
updateNetAlertCount();
var __netAlertListEl = document.getElementById("alertList");
if(__netAlertListEl) new MutationObserver(updateNetAlertCount).observe(__netAlertListEl, {childList: true});

