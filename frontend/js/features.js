// ==========================================================================
// features.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ==== Stage-3b Command Center: feature surfacing (P0 privacy + P1, MASTER-SPEC §7/§9) ====
// Additive IIFE. Consumes ONLY payloads the existing pollers already fetch, via chained
// hooks: __wavrStatus is new (fed by renderStatus); __wavrSystem/__wavrCameras/
// __wavrInventory/__wavrRS are WRAPPED, preserving the Stage-2 handlers. The camera
// kill-switch composes the existing POST /api/sources/{name}/toggle — no new endpoints,
// zero external requests. Every dynamic string is rendered with createElement/textContent
// (never innerHTML). Trust surfaces (egress dashboard, camera banner, gear Privacy,
// export) are visible in EVERY mode; anything that mutates stays live-only.
(function(){
  "use strict";
  var M = (typeof MODE !== "undefined") ? MODE : "simulated";
  var $ = function(id){ return document.getElementById(id); };
  // `modalityLabel` lives in radar.js, which loads later in the shell — so this
  // is resolved when a row is BUILT, not at parse time.
  var MODLBL = function(n){ return (typeof modalityLabel === "function")
    ? modalityLabel(n) : String(n || ""); };
  function lbl(n){ return MODLBL(n) || n; }
  function brandLbl(b){ return String(b || "—").replace(/^Gen\u00e9rico/, "Generic"); }
  function elc(tag, cls, text){
    var e = document.createElement(tag);
    if(cls) e.className = cls;
    if(text != null) e.textContent = text;
    return e;
  }
  function norm(s){
    s = String(s == null ? "" : s).toLowerCase();
    try{ return s.normalize("NFD").replace(/[̀-ͯ]/g, ""); }catch(e){ return s; }
  }

  // ---------- shared state fed by the chained hooks (bottom of this block) ----------
  var sysInfo = null;      // last /api/system payload (renderControls' 3s poll)
  var camsList = [];       // last /api/cameras payload (renderCameras)
  var statusInfo = null;   // last /api/status payload (renderStatus, live only)
  var invList = [];        // last /api/inventory devices (renderNetwork's 15s poll)
  var HIST = [];           // rolling RoomState buffer for the export (cap = backend's 1000)
  var srcSeen = {};        // modality -> {at, health, age_s} for the diagnostics
  var lastEventAt = null, evtCount = 0;
  var bootAt = Date.now();
  var wsDown = false, wsSince = null, wsDrops = 0;
  // UX HIGH #1: the SELECTED engine descriptor from GET /api/assistant/engines (Phase 2B),
  // polled independently below (own 15s cadence, live only) since it isn't part of the
  // /api/status payload. Stays null — and the trust-receipt row/popover sentence stay
  // ABSENT, not "na" — whenever this build has no assistant routes (404), the peer isn't
  // admin (403), or MODE isn't live: keeps every mode byte-identical to before this feature.
  var asstCloud = null;

  // =====================================================================
  // P0 §7#1 — Privacy & Egress dashboard (System): "what can leave this
  // home", rendered from /api/status.features. v2 egress icons: a 32px
  // state badge per row is the primary signal — green .local = stays
  // home, blue .lan = local network only, amber .egress = can leave when
  // on, dim .na = inactive/no hub. A persistent legend above the list
  // teaches the code once.
  // =====================================================================
  var EGRESS_ITEMS = [
    { k: "narrate", label: "AI Narrator", icon: "ic-spark",
      on:  "can send a state summary to the cloud — only when you click “Generate summary”",
      off: "off — no summary leaves your network" },
    { k: "ntfy", label: "Notifications (ntfy)", icon: "ic-bell",
      on:  "sends derived alerts (arrived/left, unfamiliar device) to your ntfy server — never positions, video, or MACs",
      off: "off — no alerts go out" },
    { k: "mqtt", label: "MQTT", icon: "ic-hub",
      on:  "publishes events to an MQTT broker — leaves your network only if the broker isn't local",
      off: "off — no events published" },
    { k: "ha_discovery", label: "Home Assistant", icon: "ic-ha",
      on:  "announces entities via MQTT to the Home Assistant on your network",
      off: "off" },
    { k: "internet_monitor", label: "Internet monitor", icon: "ic-wifi",
      on:  "sends periodic outbound pings just to check the connection is up",
      off: "off — no pings go out" },
    // A2.3 companion row: WAVR_HEALTH_RESOLVERS makes the user-triggered health check
    // ping public DNS hosts — that IS a (user-invoked) egress path, so it belongs in
    // this trust receipt. The passive LAN collectors do NOT belong here (see #netSensing).
    { k: "health_resolvers", label: "Health check — public resolvers", icon: "ic-wifi",
      on:  "a health check also pings 3 public DNS servers (1.1.1.1, 8.8.8.8, 9.9.9.9) — only when you click “Check now”, never in the background",
      off: "off — health checks test your gateway only (local)" },
    { k: "mcp_control", label: "MCP control (AI agent)", icon: "ic-chip",
      on:  "an agent can read state and act, limited to the action allowlist configured on the hub",
      off: "off — no agent connected" },
    { k: "multidevice", label: "Multi-device (+ TLS)", icon: "ic-devices", lan: true,
      on:  "other devices on YOUR local network can view the Space (paired, with TLS) — nothing goes to the internet",
      off: "off — only this device can access the panel" },
    { k: "net_inventory", label: "Network inventory", icon: "ic-grid", local: true,
      on:  "scans only YOUR local network to list devices — nothing leaves your network",
      off: "off — no scans" },
  ];
  // v2 egress icons: badge builder. data-tip carries the row's own state sentence —
  // the wiring point for the tooltips stage (which must ignore empty data-tips).
  // Decorative (aria-hidden): the tag + state text beside it say the same thing.
  function egBadge(sym, state, tip){
    var b = elc("span", "eg-icon" + (state ? " " + state : ""));
    b.setAttribute("aria-hidden", "true");
    b.setAttribute("data-tip", tip == null ? "" : tip);
    var NS = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(NS, "svg");
    svg.setAttribute("aria-hidden", "true");
    var use = document.createElementNS(NS, "use");
    use.setAttribute("href", "#" + (sym || "ic-qmark"));
    svg.appendChild(use);
    b.appendChild(svg);
    return b;
  }
  function egRow(label, stateTxt, iconState, tagTxt, tagCls, iconSym){
    var row = elc("div", "eg-row");
    row.setAttribute("data-tip", "");   // wiring point for the tooltips stage (empty for now)
    row.appendChild(egBadge(iconSym, iconState, stateTxt));
    var main = elc("div", "eg-main");
    var name = elc("div", "eg-name");
    name.appendChild(document.createTextNode(label));
    name.appendChild(elc("span", "eg-tag" + (tagCls ? " " + tagCls : ""), tagTxt));
    main.appendChild(name);
    main.appendChild(elc("span", "eg-state", stateTxt));
    row.appendChild(main);
    return row;
  }
  // P6 fix 1: visible "Turn off cameras" affordance INSIDE the trust receipt — the row a
  // privacy-anxious user actually reads. ONE persistent element pair (button + note),
  // re-mounted by every renderEgress() rebuild so listeners/state survive. In live,
  // renderKill() drives it exactly like camKillBtn/gearKillBtn (same killToggle fan-out).
  // In demo it is ILLUSTRATIVE ONLY: disabled + labeled "example", and the note points at
  // the real switch (the hub + the ⚙ Privacy shortcut) — it never fakes an action.
  var egKill = elc("button", "ctl switch killswitch eg-kill");
  egKill.type = "button";
  egKill.id = "egKillBtn";
  egKill.hidden = true;
  // v2 tooltips: same copy as camKillBtn/gearKillBtn — it's the same switch
  egKill.setAttribute("data-tip", WavrT("Instantly turns off every camera — cameras boot off by default"));
  var egKillState = elc("span", "kill-state eg-kill-note");
  egKillState.id = "egKillState";
  egKillState.setAttribute("aria-live", "polite");
  function egKillDemoState(){
    egKill.hidden = false;
    egKill.disabled = true;
    egKill.className = "ctl switch killswitch eg-kill off";
    egKill.textContent = WavrT("Turn off cameras");
    egKill.setAttribute("role", "switch");
    egKill.setAttribute("aria-checked", "false");
    egKill.setAttribute("aria-disabled", "true");
    egKill.title = WavrT("illustrative example — the real switch lives on the hub's panel");
    // This runs for `M !== "live"`, which is demo AND companion, and the note
    // said "in this demo" to both. A paired phone is not a demo, and being told
    // it is one about a CAMERA control is the worst place to say it: the reader
    // is checking whether the switch in front of them is real. Two lines below,
    // the same block already gives `camKillState` and `gearKillState` the
    // sentence that is true for both — the same words, so the three notes about
    // one switch cannot drift.
    egKillState.textContent = (typeof MODE !== "undefined" && MODE === "companion")
      ? WavrT("The camera master switch operates on the Wavr hub — available on the local panel. " +
              "By default, every camera starts off.")
      : WavrT("example — in this demo the switch is illustrative only; " +
        "the real control turns off every camera on the hub (shortcut: ⚙ Settings → " +
        "Privacy & security). Every camera starts off.");
  }
  function cameraEgRow(){
    // cameras are LOCAL compute — this row surfaces the boot-OFF invariant + the live count
    var txt;
    if(M === "live"){
      var cs = cameraSourceList();
      if(!cs.length) txt = WavrT("no camera configured · every new camera starts off");
      else {
        var on = cs.filter(function(s){ return s.enabled; }).length;
        txt = WavrT("{on} of {total} on · process in memory, never record, never leave this Space",
                    { on: on, total: cs.length });
      }
    } else {
      txt = WavrT("process in memory, never record, never leave this Space · start off by default");
    }
    var row = egRow(WavrT("Cameras"), txt, "local", WavrT("stays here"), "local", "ic-camera");
    // (P6 fix 1) mount the persistent kill affordance under the row's state line
    var wrap = elc("div", "eg-kill-wrap");
    wrap.appendChild(egKill);
    wrap.appendChild(egKillState);
    var main = row.querySelector(".eg-main");
    if(main) main.appendChild(wrap);
    if(M !== "live") egKillDemoState();
    return row;
  }
  // UX HIGH #1: "Wavr Assistant (cloud engine)" trust-receipt row — describes exactly
  // what leaves when the Ask panel (Settings -> Assistant) is pointed at a cloud engine.
  // Driven by asstCloud (the SELECTED descriptor from GET /api/assistant/engines, polled
  // below), NOT by /api/status.features — that endpoint has no assistant key. `egress`/
  // `needs` are the backend's own live classification (assistant_engine._descriptor):
  // needs === null means the engine is both configured AND the assistant-cloud connector
  // is on, i.e. the next "Ask" really would leave home right now.
  function asstVendorName(e){
    if(!e) return WavrT("the selected engine");
    // e.label/e.id name a third-party vendor — DATA, never translated.
    return e.id === "manual" ? WavrT("your configured endpoint") : (e.label || e.id);
  }
  function assistantCloudEgRow(){
    if(!asstCloud) return null;   // no live data yet / assistant routes absent (404) / non-admin / not live
    var active = !!asstCloud.egress && asstCloud.needs == null;
    var row;
    if(active){
      var vendor = asstVendorName(asstCloud);
      var txt = WavrT("sends your question, plus a coarse Space-state summary (current occupancy " +
        "and the Space-status verdict), to {vendor} — only when you use Ask with this engine selected",
        { vendor: vendor });
      row = egRow(WavrT("Wavr Assistant (cloud engine)"), txt, "egress", WavrT("egress possible"), "on", "ic-spark");
      row.__open = true;
    } else {
      var offTxt = WavrT("off — the Assistant is set to a local engine, or the cloud engine isn't " +
        "enabled yet in Connectors; no question leaves your network");
      row = egRow(WavrT("Wavr Assistant (cloud engine)"), offTxt, "local", WavrT("off"), "", "ic-spark");
    }
    return row;
  }
  // system-toggles (32b412f): shared wiring for the two System-tab master switches
  // (egress / network_sensing) behind #egressMasterBtn / #sensingMasterBtn. POST
  // /api/system/toggles/{name} is loopback-root only (require_local CSRF +
  // require_root — same M1 tier as the ARP-block/nodes-admin primitives; a paired
  // 'central' peer 403s). MODE==="live" IS that loopback-root browser session
  // (isLoopbackHost gate, see MODE's own definition above) and statusInfo is only
  // ever populated in live mode (renderStatus() is live-only) — so gating the
  // actuating button on "feats loaded" already means "loopback operator only": a
  // paired peer or the demo viewer never gets feats, so it keeps seeing the
  // pre-existing honest read-only receipt below instead of a disabled/dead button.
  var _sysToggleBusy = {};
  function wireSysToggleMaster(btn, fb, name, on, labelOn, labelOff, tipOn, tipOff){
    if(!btn) return;
    // The caller passes the SOURCE English label/tip; translation happens here, once,
    // so both the visible text and the composed aria-label speak one language.
    var lab = WavrT(on ? labelOn : labelOff);
    btn.hidden = false;
    btn.textContent = lab;
    btn.className = "ctl switch small " + (on ? "on" : "off");
    btn.setAttribute("role", "switch");
    btn.setAttribute("aria-checked", on ? "true" : "false");
    btn.setAttribute("aria-label", WavrT("{label}: {state}",
      { label: lab, state: on ? WavrT("on") : WavrT("off") }));
    btn.setAttribute("data-tip", WavrT(on ? tipOn : tipOff));
    btn.disabled = !!_sysToggleBusy[name];
    btn.onclick = async function(){
      if(_sysToggleBusy[name]) return;
      _sysToggleBusy[name] = true;
      btn.disabled = true;
      try{
        var r = await WavrAPI.fetch("/api/system/toggles/" + name, {method: "POST", json: { enabled: !on }});
        if(r && r.ok){
          if(fb) actionFeedback(fb, true, "", "✓ " + (!on ? WavrT("on") : WavrT("off")));
          refreshStatus?.();   // same passive-poll hook the System panel uses — repaints immediately
        } else {
          if(fb) actionFeedback(fb, false, WavrT("Couldn't change — this control needs local admin access"));
        }
      } catch(e){
        if(fb) actionFeedback(fb, false, WavrT("Couldn't change — this control needs local admin access"));
      } finally {
        _sysToggleBusy[name] = false;
        btn.disabled = false;
      }
    };
  }
  function renderEgress(){
    var list = $("egressList"), sum = $("egressSummary"), note = $("egressNote");
    var demoNote = $("egressDemoNote");
    if(!list || !sum) return;
    // Fix D: only an admin (central) companion needs the "why is the master switch missing"
    // explainer — a plain 'user' companion never sees the System tab at all (the whole
    // #controls/#healthCheck/#doctorCheck group stays hidden for it), so the note would be
    // noise there.
    var companionNote = $("egressCompanionNote");
    if(companionNote) companionNote.hidden = !(M === "companion" && companionIsCentral());
    // Item 8: a companion viewer with a token reads the SAME statusInfo.features (populated by
    // renderStatus's companion branch), so the egress rows show real state instead of "no hub
    // connected". The ACTUATING master switch stays live-only: POST /api/system/toggles/egress is
    // require_local+root (loopback), which even a paired 'central' token can't pass — so companion
    // gets the honest read-only receipt (button hidden), never a dead/misleading toggle.
    var _egCompanionRead = (M === "companion" && typeof companionToken === "function" && !!companionToken());
    var feats = ((M === "live" || _egCompanionRead) && statusInfo && statusInfo.features && typeof statusInfo.features === "object")
      ? statusInfo.features : null;
    var egMasterBtn = $("egressMasterBtn"), egMasterFb = $("egressMasterFb");
    if(M === "live" && feats && typeof feats.egress_allowed === "boolean"){
      wireSysToggleMaster(egMasterBtn, egMasterFb, "egress", feats.egress_allowed,
        "Leaving this Space: allowed", "Leaving this Space: blocked",
        "Master switch — blocks everything below that could leave this Space, even if a feature is individually enabled. Tap to block.",
        "Blocked by the operator — nothing below may leave this Space, even if individually enabled. Tap to allow again.");
    } else if(egMasterBtn){
      egMasterBtn.hidden = true;
      egMasterBtn.onclick = null;
    }
    list.textContent = "";
    var open = 0;
    // P4 fix 1: the reassuring Cameras line ("process in memory, never record, never
    // leave home") leads the list — the biggest fear gets answered before MQTT/TLS/etc.
    list.appendChild(cameraEgRow());
    EGRESS_ITEMS.forEach(function(it){
      var on = feats ? !!feats[it.k] : null;
      var st, tagTxt, tagCls, txt;
      // P4 fix 1: no bare "—" — the no-central state reads calm and explicit, never like an error.
      // v2 egress icons: st = the badge state class (local / lan / egress / na); "off"
      // keeps the green .local badge — nothing leaves home, same language as before.
      //
      // `it.lan` used to fall into the same `open++` as real egress, so a
      // household reading "4 egress path(s) enabled" was seeing "multidevice"
      // (own legend two lines up: "LAN — your network only") counted as if it
      // reached the internet — the exact confusion this legend exists to
      // prevent, self-contradicted three lines below it. LAN-only never leaves
      // this Space, so it never joins the leaving-Space count.
      if(on === null){ st = "na"; tagTxt = WavrT("inactive"); tagCls = ""; txt = WavrT("no hub connected — feature inactive (not an error)"); }
      else if(!on){ st = "local"; tagTxt = WavrT("off"); tagCls = ""; txt = WavrT(it.off); }
      else if(it.local){ st = "local"; tagTxt = WavrT("local"); tagCls = "local"; txt = WavrT(it.on); }
      else if(it.lan){ st = "lan"; tagTxt = WavrT("local network"); tagCls = "lan"; txt = WavrT(it.on); }
      else { st = "egress"; tagTxt = WavrT("egress possible"); tagCls = "on"; txt = WavrT(it.on); open++; }
      list.appendChild(egRow(WavrT(it.label), txt, st, tagTxt, tagCls, it.icon));
      // UX HIGH #1: grouped right after the AI Narrator row — both are "ask an AI" cloud
      // paths. Absent (not even an "na" row) unless a live /api/assistant/engines fetch
      // has actually succeeded — see assistantCloudEgRow()'s own docstring.
      if(it.k === "narrate"){
        var asstRow = assistantCloudEgRow();
        if(asstRow){ list.appendChild(asstRow); if(asstRow.__open) open++; }
      }
    });
    if(feats){
      if(note) note.hidden = true;
      if(demoNote) demoNote.hidden = true;
      if(open === 0){
        sum.className = "egress-summary ok";
        sum.textContent = WavrT("Nothing below can leave your network right now.");
      } else {
        sum.className = "egress-summary warn";
        // "if you use them", not "enabled": most of the rows counted here — the
        // narrator, the Assistant's cloud engine — only reach outside on the
        // one click that invokes them, so "enabled" read as "already
        // happening" over a Space that had sent nothing. This is a capability
        // list, not a live activity feed; "Nothing leaves your network right
        // now" (the chip, and Privacy & security) answers the other question.
        sum.textContent = WavrT("{n} feature(s) below can leave your network if you use them.", { n: open });
      }
    } else if(M === "simulated"){
      // P6 fix 3: the PERMANENT architecture guarantee and the demo-mode note are two
      // separate sentences in two elements — "nothing leaves" never reads as demo-only.
      sum.className = "egress-summary";
      sum.textContent = WavrT("Wavr runs locally; nothing about your Space leaves the device by default.");
      if(demoNote) demoNote.hidden = false;
      if(note) note.hidden = false;
    } else {
      sum.className = "egress-summary na";
      sum.textContent = WavrT("The real state of these features comes from the Wavr hub (local panel).");
      if(demoNote) demoNote.hidden = true;
      if(note) note.hidden = false;
    }
  }

  // UX HIGH #1: the header privacy popover (#privacyPop) is the OTHER surface that claimed
  // "only the narrator sends a summary to the cloud" — now stale once a cloud assistant
  // engine is live. Reuses the same asstCloud state as the trust-receipt row; the sentence
  // is appended (not rewritten) so the fixed legal-ish copy above it never gets touched.
  function renderPrivacyPopAssistant(){
    var el = $("privacyPopAsst");
    if(!el) return;
    var active = !!(asstCloud && asstCloud.egress && asstCloud.needs == null);
    if(!active){ el.hidden = true; el.textContent = ""; return; }
    el.textContent = " " + WavrT("The Assistant is currently set to a cloud engine ({vendor}): " +
      "your question and a coarse Space-state summary go to it each time you use Ask.",
      { vendor: asstVendorName(asstCloud) });
    el.hidden = false;
  }

  // UX HIGH #1: independent poll for the assistant's SELECTED engine — /api/status has no
  // assistant key, so this can't ride the existing 3s __wavrStatus hook. Same fail-hidden
  // discipline as renderAssistant() itself (Settings -> Assistant): a non-ok response (404
  // routes absent, 403 non-admin peer, or the fetch throwing) leaves asstCloud null, which
  // keeps both the trust-receipt row and the popover sentence ABSENT — byte-identical to
  // every mode's pre-existing rendering whenever this build has no assistant feature.
  var asstEgressStarted = false;
  async function pollAssistantEgress(){
    if(M !== "live"){ asstCloud = null; renderEgress(); renderPrivacyPopAssistant(); return; }
    try{
      var r = await WavrAPI.fetch("/api/assistant/engines");
      if(!r.ok){ asstCloud = null; }
      else{
        var d = await r.json();
        var engines = Array.isArray(d && d.engines) ? d.engines : [];
        asstCloud = engines.find(function(e){ return e && e.selected; }) || null;
      }
    }catch(e){ asstCloud = null; }
    renderEgress();
    renderPrivacyPopAssistant();
    if(!asstEgressStarted){ asstEgressStarted = true; setInterval(pollAssistantEgress, 15000); }
  }

  // =====================================================================
  // A2.1 — Network-sensing collectors (System, below the egress list — LOCAL
  // sensing, deliberately NOT an egress row): read-only surface of
  // /api/status.features.{mdns,ssdp,netbios,snmp,dhcp_fp,rogue_dhcp}, rendered
  // off the SAME 3s poll renderStatus() already runs (window.__wavrStatus,
  // wired below) — zero new network calls. F1: these 6 flags are env-only
  // today (no runtime toggle exists), so the copy is honest about needing a
  // hub restart to change, never implying an in-app switch. F3: netbios/snmp
  // are active targeted probes (one packet per known device); the others are
  // passive listeners — the tag TEXT says so, the badge color does not
  // differ (both stay on-LAN either way, see the CSS comment above).
  // =====================================================================
  var SENSING_ITEMS = [
    { k:"mdns", label:"mDNS / Bonjour", icon:"ic-wifi", active:false,
      on:  "listening for Apple/IoT device announcements (UDP 5353) — passive, local only",
      off: "off — enable with WAVR_NET_MDNS=1 on the hub (restart required)" },
    { k:"ssdp", label:"SSDP / UPnP", icon:"ic-grid", active:false,
      on:  "listening for UPnP devices announcing themselves (TVs, printers, routers) — passive, local only",
      off: "off — enable with WAVR_NET_SSDP=1 on the hub (restart required)" },
    { k:"netbios", label:"NetBIOS", icon:"ic-devices", active:true,
      on:  "sends one lookup to each device already on your network to read its Windows/workgroup name — local only, never leaves your LAN",
      off: "off — enable with WAVR_NET_NETBIOS=1 on the hub (restart required)" },
    { k:"snmp", label:"SNMP", icon:"ic-chip", active:true,
      on:  "sends one read-only query to each device already on your network to identify make/model — local only, never leaves your LAN",
      off: "off — enable with WAVR_NET_SNMP=1 on the hub (restart required)" },
    // dhcp_fp/rogue_dhcp bind a raw socket on UDP/68, which a non-root proot/container
    // build can't do even after the env flag is set + restarted (works fine on a host
    // with real network capabilities, e.g. the G9 Core's proot under Magisk su). These
    // two are NOT SourceManager sources and carry no {enabled,active} pair, but GET
    // /api/status's "availability" key (Fix #9/#17) now carries a real per-collector
    // bind-success signal (renderSensing's 4th "unavailable" branch, below), so a
    // failed bind renders honestly instead of the misleading on-copy. The off-copy
    // below still states the raw-socket caveat for the (off, never-attempted) case,
    // where availability is null and there is genuinely no signal yet.
    { k:"dhcp_fp", label:"DHCP fingerprint", icon:"ic-radar", active:false,
      on:  "reads the DHCP requests devices already broadcast on your network to guess OS/hostname — passive, local only",
      off: "off — enable with WAVR_NET_DHCP_FP=1 on the hub (restart required; needs a raw-socket network capability some restricted environments, like a non-root container, can't grant)" },
    { k:"rogue_dhcp", label:"Rogue DHCP-server detection", icon:"ic-alert", active:false,
      on:  "watches for a second/unexpected DHCP server on your network (a common attack) — passive, local only; alerts appear in the Network tab",
      off: "off — enable with WAVR_NET_DHCP_MONITOR=1 on the hub (restart required; needs a raw-socket network capability some restricted environments, like a non-root container, can't grant)" },
  ];
  function snBadge(sym, on, tip){
    var b = elc("span", "sn-icon" + (on ? " on" : " na"));
    b.setAttribute("aria-hidden", "true");
    b.setAttribute("data-tip", tip == null ? "" : tip);
    var NS = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(NS, "svg");
    svg.setAttribute("aria-hidden", "true");
    var use = document.createElementNS(NS, "use");
    use.setAttribute("href", "#" + (sym || "ic-qmark"));
    svg.appendChild(use);
    b.appendChild(svg);
    return b;
  }
  // Own row builder (never egRow) — can never end up rendered into #egressList by accident.
  function snRow(label, stateTxt, on, tagTxt, tagCls, iconSym){
    var row = elc("div", "sn-row");
    row.setAttribute("data-tip", "");
    row.appendChild(snBadge(iconSym, on, stateTxt));
    var main = elc("div", "sn-main");
    var name = elc("div", "sn-name");
    name.appendChild(document.createTextNode(label));
    name.appendChild(elc("span", "sn-tag" + (tagCls ? " " + tagCls : ""), tagTxt));
    main.appendChild(name);
    main.appendChild(elc("span", "sn-state", stateTxt));
    row.appendChild(main);
    return row;
  }
  function renderSensing(){
    var list = $("sensingList");
    if(!list) return;
    // Item 8: companion viewer reads the same statusInfo.features so the collector rows show real
    // state; the actuating master toggle stays live-only (require_local+root — see renderEgress).
    var _snCompanionRead = (M === "companion" && typeof companionToken === "function" && !!companionToken());
    var feats = ((M === "live" || _snCompanionRead) && statusInfo && statusInfo.features && typeof statusInfo.features === "object")
      ? statusInfo.features : null;
    var snMasterBtn = $("sensingMasterBtn"), snMasterFb = $("sensingMasterFb");
    // Privacy tab (gearSecPrivacy): a second instance of the SAME network_sensing master
    // toggle, surfaced where the real controls now live — same wireSysToggleMaster() call,
    // same fail-hidden gate, just a different pair of DOM nodes.
    var privSnBtn = $("privSensingBtn"), privSnFb = $("privSensingFb");
    if(M === "live" && feats && typeof feats.sensing_allowed === "boolean"){
      wireSysToggleMaster(snMasterBtn, snMasterFb, "network_sensing", feats.sensing_allowed,
        "Sensing: allowed", "Sensing: paused",
        "Master switch — pauses the optional collectors below at once (the base LAN presence scan is unaffected). Tap to pause.",
        "Paused by the operator — the collectors below are held off, even if individually enabled. Tap to resume.");
      wireSysToggleMaster(privSnBtn, privSnFb, "network_sensing", feats.sensing_allowed,
        "Sensing: allowed", "Sensing: paused",
        "Master switch — pauses the optional network-sensing collectors (mDNS/SSDP/NetBIOS/SNMP/DHCP-fp) at once. Tap to pause.",
        "Paused by the operator — the optional collectors are held off, even if individually enabled. Tap to resume.");
    } else {
      if(snMasterBtn){ snMasterBtn.hidden = true; snMasterBtn.onclick = null; }
      if(privSnBtn){ privSnBtn.hidden = true; privSnBtn.onclick = null; }
    }
    // Fix #9/#17: GET /api/status's additive "availability" key -- tri-state per
    // privileged-bind collector (dhcp_fp/rogue_dhcp are netinventory collectors, not
    // SourceManager sources, so they carry no {enabled,active} pair of their own).
    // available===null: feature off, or no collect()/check_once() cycle has run yet
    // since startup -- no signal either way, treated exactly like today (silent).
    // available===false: the raw socket bind itself failed on the most recent
    // attempt (e.g. non-root proot lacking CAP_NET_BIND_SERVICE) -- distinct from a
    // quiet LAN, which is available===true with zero devices seen.
    var avail = ((M === "live" || _snCompanionRead) && statusInfo && statusInfo.availability && typeof statusInfo.availability === "object")
      ? statusInfo.availability : null;
    list.textContent = "";
    SENSING_ITEMS.forEach(function(it){
      var on = feats ? !!feats[it.k] : null;
      var a = avail ? avail[it.k] : null;
      var tagTxt, txt, iconOn;
      if(on === null){ iconOn = false; tagTxt = WavrT("inactive"); txt = WavrT("no hub connected — feature inactive (not an error)"); }
      else if(on && a && a.available === false){
        // 4th branch: enabled, but the bind itself failed -- calm/non-alarming,
        // and does NOT repeat the "enable with WAVR_*=1" instruction from it.off,
        // since the flag is already set and restarting won't fix an environment
        // that can't grant the capability.
        iconOn = false; tagTxt = WavrT("unavailable");
        // a.reason is the hub's own diagnostic string — backend DATA, appended verbatim.
        txt = WavrT("unavailable on this device — not an error") + (a.reason ? " (" + a.reason + ")" : "");
      }
      else if(!on){ iconOn = false; tagTxt = WavrT("off"); txt = WavrT(it.off); }
      else { iconOn = true; tagTxt = it.active ? WavrT("active probe") : WavrT("passive listen"); txt = WavrT(it.on); }
      var tagCls = iconOn ? (it.active ? "probe" : "on") : "";
      list.appendChild(snRow(WavrT(it.label), txt, iconOn, tagTxt, tagCls, it.icon));
    });
  }

  // =====================================================================
  // P0 §7#3 — Camera master kill-switch (Ativos + gear shortcut, same
  // closure/state): camera-type sources = /api/system sources whose name
  // matches a configured camera (the backend registers each camera under
  // its camera name). One toggle fans POST /api/sources/{name}/toggle to
  // EVERY one of them; "Off for privacy." reads calm, not red.
  // =====================================================================
  var killBusy = false;
  function cameraSourceList(){
    if(!sysInfo || !Array.isArray(sysInfo.sources)) return [];
    var names = {};
    camsList.forEach(function(c){ if(c && c.name) names[c.name] = 1; });
    return sysInfo.sources.filter(function(s){ return s && names[s.name]; });
  }
  function renderKill(){
    // P6 fix 1: egKill (the affordance inside "What can leave this home") joins the two
    // static kill buttons — in live it is the SAME master switch; in demo it stays visible
    // as a labeled example (egKillDemoState) while the central-panel buttons hide.
    // dashboard-topbar: #coreKillBtn (the Core Panel ambient-face pill) joins the fan-out
    // too — no separate state paragraph for it (it's a compact pill, not a tile row), so it
    // only appears in btns, never in states.
    var btns = [$("camKillBtn"), $("gearKillBtn"), egKill, $("coreKillBtn")];
    var states = [$("camKillState"), $("gearKillState"), egKillState];
    // Saliency fix (07 screen): state-reflecting left accent bar on the master-control tile
    // itself (see .cam-privacy.cp-live in the CSS) — accent only while cameras are live.
    var camPrivacyTile = $("camPrivacy");
    function setCamPrivacyState(live){
      if(camPrivacyTile) camPrivacyTile.classList.toggle("cp-live", !!live);
    }
    function say(t, calm){
      states.forEach(function(s){
        if(s){
          s.textContent = t;
          s.className = (s === egKillState ? "kill-state eg-kill-note" : "kill-state") + (calm ? " calm" : "");
        }
      });
    }
    if(M !== "live"){
      btns.forEach(function(b){ if(b && b !== egKill) b.hidden = true; });
      egKillDemoState();   // visible, disabled, honestly labeled — sets its own note text
      [$("camKillState"), $("gearKillState")].forEach(function(s){
        if(s){
          s.textContent = WavrT("The camera master switch operates on the Wavr hub — available on the local panel. " +
            "By default, every camera starts off.");
          s.className = "kill-state";
        }
      });
      setCamPrivacyState(false);
      return;
    }
    var cs = cameraSourceList();
    if(!cs.length){
      btns.forEach(function(b){ if(b) b.hidden = true; });
      say(WavrT("No camera configured — when you add one, it starts off."));
      egKillState.textContent = "";   // the Cameras row's own state line already says this
      setCamPrivacyState(false);
      return;
    }
    var on = cs.filter(function(s){ return s.enabled; });
    var anyOn = on.length > 0;
    btns.forEach(function(b){
      if(!b) return;
      b.hidden = false;
      b.disabled = killBusy || tierBusy;
      b.className = "ctl switch killswitch " + (b === egKill ? "eg-kill " : "") + (anyOn ? "on" : "off");
      b.textContent = killBusy ? WavrT("applying…") : (anyOn ? WavrT("Turn off all cameras") : WavrT("Turn cameras back on"));
      b.setAttribute("role", "switch");
      b.setAttribute("aria-checked", anyOn ? "true" : "false");
      b.setAttribute("aria-label", anyOn
        ? WavrT("turn off all cameras ({on} of {total} on)", { on: on.length, total: cs.length })
        : WavrT("turn all cameras back on"));
    });
    if(anyOn) say(WavrT("{on} of {total} camera(s) on — one tap turns them all off.", { on: on.length, total: cs.length }));
    else say(WavrT("Off for privacy."), true);
    setCamPrivacyState(anyOn);
  }
  async function killToggle(){
    if(M !== "live" || killBusy || tierBusy) return;   // tierBusy: a Sensing-level change is mid-fan-out
    var cs = cameraSourceList();
    if(!cs.length) return;
    var target = !cs.some(function(s){ return s.enabled; });   // any on -> all off; all off -> all on
    killBusy = true; renderKill();
    await Promise.all(cs.map(function(s){
      return WavrAPI.fetch("/api/sources/" + encodeURIComponent(s.name) + "/toggle", {method: "POST", json: { enabled: target }}).catch(function(){});
    }));
    // quick re-sync (renderControls' own 3s poll converges anyway; this just closes the gap)
    try{
      var r = await fetch(location.origin + "/api/system");
      if(r.ok){ var s2 = await r.json(); if(s2 && typeof s2 === "object") sysInfo = s2; }
    }catch(e){}
    killBusy = false;
    renderKill(); renderEgress(); renderTier();
  }
  [$("camKillBtn"), $("gearKillBtn"), egKill, $("coreKillBtn")].forEach(function(b){
    if(b) b.addEventListener("click", killToggle);
  });

  // =====================================================================
  // Global Sensing-level meter (Off | Presence | Precise) — a segmented control
  // DERIVED from the SAME sysInfo/camsList this closure already holds:
  //   Off      = system stopped (manager.running === false)
  //   Presence = system running, NO camera source enabled (network/BLE only, zero VRAM)
  //   Precise  = system running AND >=1 camera source enabled (person-detection on)
  // Setting a tier composes ONLY the existing seams: POST /api/system/toggle + the same
  // POST /api/sources/{name}/toggle camera fan-out the master kill uses. No new backend
  // state. Boot tier is Presence (cameras boot off) — privacy-first by default. Live
  // mutates; demo/companion renders it as an illustrative, non-acting example.
  // OUT of this slice (pending owner decisions): a Max tier, per-room granularity,
  // and identification — deliberately not built here.
  // =====================================================================
  var TIER_ORDER = ["off", "presence", "precise"];
  var TIER_META = {
    off:      { label: "Off",
                posture: "System paused — nothing is sensing. Zero compute, zero VRAM." },
    presence: { label: "Presence",
                posture: "Network and Bluetooth only — Space-level presence. No cameras, no video, zero VRAM." },
    precise:  { label: "Precise",
                posture: "Cameras on — per-room person detection. Video is processed in memory (never recorded, never leaves this Space) and uses the GPU/VRAM." },
  };
  var tierBusy = false;
  function anyCamEnabled(){ return cameraSourceList().some(function(s){ return s.enabled; }); }
  function deriveTier(){
    if(!sysInfo || !sysInfo.running) return "off";
    return anyCamEnabled() ? "precise" : "presence";
  }
  function tierPost(url, body){
    return WavrAPI.fetch(url, {method: "POST", json: body});
  }
  async function tierSetRunning(on){
    var r = await tierPost("/api/system/toggle", { on: on });
    if(r && r.ok){ var s = await r.json(); if(s && typeof s === "object") sysInfo = s; }
  }
  async function tierFanCameras(enabled){
    // idempotent: only toggles cameras not already in the wanted state
    var targets = cameraSourceList().filter(function(s){ return s.enabled !== enabled; });
    await Promise.all(targets.map(function(s){
      return tierPost("/api/sources/" + encodeURIComponent(s.name) + "/toggle",
                      { enabled: enabled }).catch(function(){});
    }));
  }
  async function setTier(tier){
    if(M !== "live" || tierBusy || killBusy) return;
    if(!TIER_META[tier] || tier === deriveTier()) return;
    tierBusy = true; renderTier(); renderKill();
    try{
      if(tier === "off"){
        await tierSetRunning(false);
      } else if(tier === "presence"){
        // cameras OFF first so turning the system on can't spawn YOLO for even one frame
        await tierFanCameras(false);
        if(!sysInfo || !sysInfo.running) await tierSetRunning(true);
      } else { // precise
        if(!sysInfo || !sysInfo.running) await tierSetRunning(true);
        await tierFanCameras(true);
      }
      // converge (renderControls' 3s poll would too; this just closes the gap)
      try{
        var rr = await fetch(location.origin + "/api/system");
        if(rr.ok){ var s2 = await rr.json(); if(s2 && typeof s2 === "object") sysInfo = s2; }
      }catch(e){}
    }catch(e){}
    tierBusy = false;
    renderTier(); renderKill(); renderEgress();
  }
  function privacyPause(){
    if(M !== "live" || tierBusy || killBusy) return;
    var idx = TIER_ORDER.indexOf(deriveTier());
    if(idx <= 0) return;   // already Off — nothing lower to drop to
    setTier(TIER_ORDER[idx - 1]);
  }
  function renderTier(){
    var seg = $("sensingLevelSeg"), posture = $("sensingLevelPosture");
    var pause = $("privacyPauseBtn"), note = $("sensingLevelNote");
    if(!seg || !posture) return;
    var live = (M === "live");
    // Item 2/8: a companion viewer shows the HOME's REAL sensing level, read-only — from the
    // server-derived hub_level in status.features (so it doesn't re-implement deriveTier, which
    // needs the live-only sysInfo). Falls back to Presence only until the hub reports it. Demo stays
    // the illustrative Presence example. Buttons stay disabled for companion (writes run on the hub).
    var _tierCompanion = (M === "companion" && typeof companionToken === "function" && !!companionToken()
                          && statusInfo && statusInfo.features && typeof statusInfo.features === "object");
    var _hubLevel = (_tierCompanion && TIER_ORDER.indexOf(statusInfo.features.hub_level) >= 0)
                    ? statusInfo.features.hub_level : null;
    var cur = live ? deriveTier() : (_hubLevel || "presence");
    var locked = !live || tierBusy || killBusy;
    var btns = seg.querySelectorAll(".seg-btn");
    Array.prototype.forEach.call(btns, function(b){
      var t = b.getAttribute("data-tier");
      b.setAttribute("aria-pressed", (t === cur) ? "true" : "false");
      b.disabled = locked;
    });
    seg.classList.toggle("locked", !live);
    var meta = TIER_META[cur] || TIER_META.presence;
    posture.className = "sensing-level-posture t-" + cur;
    posture.textContent = "";
    posture.appendChild(elc("b", null, WavrT(meta.label) + " — "));
    posture.appendChild(document.createTextNode(WavrT(meta.posture)));
    if(pause){
      var canDrop = live && TIER_ORDER.indexOf(cur) > 0;
      pause.hidden = !live;
      pause.disabled = !canDrop || tierBusy || killBusy;
      pause.className = "ctl small off";
    }
    if(note){
      note.textContent = live
        ? (tierBusy ? WavrT("applying…")
           : cur === "off" ? WavrT("Everything is paused.")
           : WavrT("Privacy pause drops one level instantly."))
        : _tierCompanion
          ? WavrT("This is your Space's sensing level, set on the hub. Choose what THIS device shares below.")
          : WavrT("Live control runs on the hub (local panel). Default is Presence — cameras start off.");
    }
  }
  (function(){
    var seg = $("sensingLevelSeg"), pause = $("privacyPauseBtn");
    if(seg) seg.addEventListener("click", function(e){
      var b = (e.target && e.target.closest) ? e.target.closest(".seg-btn") : null;
      if(b && b.getAttribute("data-tier")) setTier(b.getAttribute("data-tier"));
    });
    if(pause) pause.addEventListener("click", privacyPause);
  })();

  // =====================================================================
  // Watch/Guard ("Vigia") -- an ORTHOGONAL privacy overlay on the sensing
  // ladder (server-side, wavr.WatchMode). While on, the hub suppresses every
  // family position/identity/vital from state + every egress and surfaces only
  // counts + the intrusion room; this control just reflects + toggles it. Live
  // mutates via POST /api/watch (require_local); demo renders an honest example.
  // =====================================================================
  var watchInfo = null, watchBusy = false;
  function paintWatch(){
    var btn = $("watchToggle"), stEl = $("watchState"), note = $("watchNote");
    if(!btn) return;
    var live = (M === "live");
    var on = live ? !!(watchInfo && watchInfo.on) : false;
    btn.disabled = !live || watchBusy;
    btn.className = "ctl switch " + (on ? "on" : "off");
    btn.setAttribute("aria-checked", on ? "true" : "false");
    btn.textContent = watchBusy ? WavrT("applying…") : (on ? WavrT("Watch: on") : WavrT("Watch: off"));
    if(stEl) stEl.textContent = on ? WavrT("On — everyone's positions hidden") : WavrT("Off");
    if(note){
      note.className = "watch-note";
      if(!live){
        note.textContent = WavrT("Watch runs on the hub (local panel). It detects an unrecognized person while hiding everyone positions, identities and vitals from the dashboard and every export.");
      } else if(!on){
        note.textContent = WavrT("Off — full per-room view. Turn Watch on to hide everyone positions and be alerted only to an unrecognized person.");
      } else if(watchInfo && !watchInfo.intrusion_detection){
        note.textContent = WavrT("On — positions, identities and vitals are hidden everywhere. To flag an unrecognized person, enable the identity layer (who is here) so Wavr knows who is expected.");
      } else {
        var rooms = (watchInfo && watchInfo.unrecognized_rooms) || [];
        if(rooms.length){
          note.className = "watch-note alarm";
          // rooms are the household's own room names — DATA, dropped in whole.
          note.textContent = WavrT("Unrecognized person detected in: {rooms}. Everyone's positions stay hidden.",
                                   { rooms: rooms.join(", ") });
        } else {
          note.textContent = WavrT("Watching — no unrecognized person. Positions, identities and vitals are hidden from the dashboard and every export; only counts + an intrusion room can leave.");
        }
      }
    }
  }
  function renderWatch(){
    if(!$("watchToggle")) return;
    if(M !== "live"){ paintWatch(); return; }
    fetch(location.origin + "/api/watch").then(function(r){ return r.ok ? r.json() : null; })
      .then(function(j){ if(j && typeof j === "object") watchInfo = j; paintWatch(); })
      .catch(function(){ paintWatch(); });
  }
  async function watchToggle(){
    if(M !== "live" || watchBusy) return;
    var target = !(watchInfo && watchInfo.on);
    watchBusy = true; paintWatch();
    try{
      var r = await WavrAPI.fetch("/api/watch", {method: "POST", json: { on: target }});
      if(r.ok){ var j = await r.json(); if(j && typeof j === "object") watchInfo = j; }
    }catch(e){}
    watchBusy = false; paintWatch();
  }
  (function(){ var b = $("watchToggle"); if(b) b.addEventListener("click", watchToggle); })();
  // dashboard-topbar: #coreWatchPill (Core Panel ambient-face quick control) mirrors
  // #watchToggle's rendered text/class 1:1 (same MutationObserver technique the topbar's
  // #pillRede badge already uses elsewhere) and forwards its click to the
  // SAME watchToggle() above, so the two controls can never drift out of sync.
  (function(){
    var srcBtn = $("watchToggle"), mirror = $("coreWatchPill");
    if(!srcBtn || !mirror) return;
    function mirrorWatch(){
      mirror.hidden = false;
      mirror.disabled = srcBtn.disabled;
      mirror.className = "ctl switch core-pill" + (srcBtn.classList.contains("on") ? " on" : " off");
      mirror.textContent = srcBtn.textContent;
      mirror.setAttribute("role", "switch");
      mirror.setAttribute("aria-checked", srcBtn.getAttribute("aria-checked") || "false");
    }
    new MutationObserver(mirrorWatch).observe(srcBtn,
      { attributes: true, attributeFilter: ["class", "aria-checked", "disabled"], childList: true, characterData: true, subtree: true });
    mirror.addEventListener("click", function(ev){ ev.stopPropagation(); watchToggle(); });
    mirrorWatch();
  })();

  // =====================================================================
  // P1 §7#4 — History filters: room + kind, purely over the rows the
  // timeline already holds. pushTimeline() tags each row (data-room/kind)
  // and calls __wavrTlRow so new rows respect the active filter.
  // =====================================================================
  var tlRoomSel = $("tlRoom"), tlNoneEl = $("tlNone");
  var tlKindBtns = Array.prototype.slice.call(document.querySelectorAll("#tlFilters .fchip"));
  var tlKinds = {};        // kind -> true (none set = show all)
  var tlRoomVal = "";
  var tlRooms = [];
  function tlAnyKind(){ for(var k in tlKinds) if(tlKinds[k]) return true; return false; }
  function tlMatch(row){
    if(tlRoomVal && row.dataset.room !== tlRoomVal) return false;
    if(tlAnyKind() && !tlKinds[row.dataset.kind || "presenca"]) return false;
    return true;
  }
  function applyTlFilter(){
    var tl = $("timeline");
    if(!tl) return;
    var rows = tl.querySelectorAll(".row");
    var shown = 0;
    Array.prototype.forEach.call(rows, function(r){
      var ok = tlMatch(r);
      r.classList.toggle("tl-hide", !ok);
      if(ok) shown++;
    });
    if(tlNoneEl) tlNoneEl.hidden = !(rows.length && !shown);
  }
  tlKindBtns.forEach(function(b){
    b.addEventListener("click", function(){
      var k = b.dataset.kind;
      tlKinds[k] = !tlKinds[k];
      b.classList.toggle("on", !!tlKinds[k]);
      b.setAttribute("aria-pressed", tlKinds[k] ? "true" : "false");
      applyTlFilter();
    });
  });
  if(tlRoomSel) tlRoomSel.addEventListener("change", function(){
    tlRoomVal = tlRoomSel.value;
    applyTlFilter();
  });
  function tlAddRoom(name){
    if(!name || !tlRoomSel || tlRooms.indexOf(name) >= 0) return;
    tlRooms.push(name); tlRooms.sort();
    var keep = tlRoomSel.value;
    while(tlRoomSel.options.length > 1) tlRoomSel.remove(1);
    tlRooms.forEach(function(r){
      var o = document.createElement("option");
      o.value = r; o.textContent = r;   // room names are RoomState data — textContent only
      tlRoomSel.appendChild(o);
    });
    tlRoomSel.value = keep;             // options are never removed, so this always resolves
  }
  window.__wavrTlRow = function(row){
    try{
      tlAddRoom(row.dataset.room);
      var ok = tlMatch(row);
      row.classList.toggle("tl-hide", !ok);
      if(ok && tlNoneEl) tlNoneEl.hidden = true;
    }catch(e){}
  };

  // =====================================================================
  // A2.2 — Presence report (History tab): pure LOCAL aggregation off
  // GET /api/presence/report ("safe to call on every GET" per the module's
  // own docstring — no new scanning/I-O). Polls on the same 15s cadence
  // renderNetwork() uses for /api/inventory; live-mode only — demo/companion
  // show the honest "needs a hub" note (same convention as redeNote/
  // dispNote/sisNote). Rows reuse the Network tab's dev-row markup +
  // maskMac()/dtypeLabel()/dtypeIconEl() (global top-level helpers) so a
  // MAC is never printed unmasked in a new component.
  // =====================================================================
  var presenceStarted = false;
  var presRetries = 0;   // A2.2 companion: bounded retries while the async token loads (see renderPresence)
  // The report's own device_type is the user PIN only (device_meta) — for unpinned
  // devices, borrow the fused type+confidence from the /api/inventory payload the
  // Network tab already polls (zero new fetches; falls back honestly when absent).
  var presInvIdx = {};
  function presBuildInvIdx(){
    presInvIdx = {};
    invList.forEach(function(v){ if(v && v.mac) presInvIdx[v.mac] = v; });
  }
  function presRow(d, kind){
    var inv = presInvIdx[d.mac];
    var dtype = d.device_type || (inv && inv.device_type) || null;
    // a pin always reads back "high" on /api/inventory; mirror that rule here
    var conf = d.device_type ? "high" : (inv ? (inv.type_confidence || null) : null);
    var row = elc("div", "dev-row");
    row.appendChild(dtypeIconEl(dtype, conf, false, false));
    var left = elc("div", "dev-left");
    var nameLine = elc("div", "dev-name-line");
    if(d.name){
      nameLine.appendChild(elc("span", "dev-main named", d.name));
      nameLine.appendChild(elc("span", "dev-sub", " · " + dtypeLabel(dtype)));
    } else {
      nameLine.appendChild(elc("span", "dev-main", dtypeLabel(dtype)));
    }
    left.appendChild(nameLine);
    var metaLine = elc("div", "dev-meta-line");
    metaLine.appendChild(elc("span", "mac", maskMac(d.mac)));
    var secs = kind === "present" ? d.tenure_seconds : d.quiet_for_seconds;
    if(secs != null){
      var dur = fmtDur(secs * 1000);
      metaLine.appendChild(elc("span", "dev-seen", " · " + (kind === "present"
        ? WavrT("here {duration}", { duration: dur })
        : WavrT("away {duration}", { duration: dur }))));
    }
    left.appendChild(metaLine);
    row.appendChild(left);
    return row;
  }
  function renderPresenceList(el, arr, kind, emptyTxt){
    if(!el) return;
    el.textContent = "";
    if(!arr || !arr.length){ el.appendChild(elc("div", "net-hint", emptyTxt)); return; }
    arr.forEach(function(d){ el.appendChild(presRow(d, kind)); });
  }
  function presenceOffState(){
    var note = $("presenceNote"); if(note) note.hidden = false;
    var sum = $("presenceSummary"); if(sum) sum.textContent = "";
    ["presPresentCount", "presAwayCount", "presStaleCount"].forEach(function(id){
      var e = $(id); if(e) e.textContent = "";
    });
    ["presPresentList", "presTopList", "presAwayList", "presStaleList"].forEach(function(id){
      var e = $(id); if(e) e.textContent = "";
    });
  }
  async function presenceRefresh(){
    // A2.2 companion (Fix): route through the pinned central base + Bearer (same __nf pattern
    // renderStatus/renderNetwork use), NOT the app's own https://localhost — on the companion a
    // bare location.origin fetch hits the WebView, not the hub, so the report was always empty.
    // network:read is granted to user AND central, so any paired companion sees the report.
    var auth = (MODE==="companion" && companionToken()) ? {"Authorization":"Bearer "+companionToken()} : null;
    var __nf = (path, opt)=> window.WAVR_MOBILE
      ? window.WAVR_MOBILE.netFetch(window.WAVR_MOBILE.base + path, opt)
      : fetch(location.origin + path, opt);
    var r;
    try{ r = await __nf("/api/presence/report", auth?{headers:auth}:undefined); }
    catch(e){ return; }   // backend momentarily unreachable — keep last view
    if(auth && (r.status===401||r.status===403)){ if(typeof companionAuthFailed==="function") companionAuthFailed(); return; }
    if(!r.ok){
      // route missing = a hub running pre-A2 code (404) — say so honestly instead
      // of rendering the error body as "0 devices tracked"
      var sum0 = $("presenceSummary");
      if(sum0 && !sum0.textContent) sum0.textContent =
        WavrT("unavailable on this hub version — restart/update the Wavr hub to enable the presence report");
      return;
    }
    var d;
    try{ d = await r.json(); }catch(e){ return; }
    if(!d || typeof d !== "object") return;
    presBuildInvIdx();
    var sum = $("presenceSummary");
    if(sum){
      var n = d.device_count || 0;
      var txt = WavrT("{n} device tracked|{n} devices tracked", { n: n });
      // quiet_period_seconds = time since ANY device was last seen — phrase it as
      // "last network activity", not "house quiet" (scans land every few seconds).
      txt += " · " + ((d.quiet_period_seconds != null)
        ? WavrT("last network activity {ago}", { ago: fmtAgo(d.quiet_period_seconds * 1000) })
        : WavrT("no activity recorded yet"));
      sum.textContent = txt;
    }
    // parens live in the VALUE, not the static markup — empty spans (old hub /
    // pre-first-fetch) render nothing instead of a bare "()"
    var pc = $("presPresentCount"); if(pc) pc.textContent = "(" + (d.currently_present || []).length + ")";
    var ac = $("presAwayCount"); if(ac) ac.textContent = "(" + (d.recently_away || []).length + ")";
    var sc = $("presStaleCount"); if(sc) sc.textContent = "(" + (d.stale || []).length + ")";
    renderPresenceList($("presPresentList"), d.currently_present, "present", WavrT("no devices currently present"));
    renderPresenceList($("presTopList"), d.most_present, "present", WavrT("not enough data yet"));
    renderPresenceList($("presAwayList"), d.recently_away, "away", WavrT("none"));
    renderPresenceList($("presStaleList"), d.stale, "away", WavrT("none"));
  }
  function renderPresence(){
    var tile = $("presenceReport");
    if(!tile) return;
    tile.hidden = false;   // visible in every mode (own honest note when not live, like egressPanel)
    // A2.2 companion (Fix): a paired companion (user OR central — both hold network:read) pulls
    // the real house-level report; demo / unpaired companion keeps the honest off-note.
    if(M !== "live" && !(MODE==="companion" && companionToken())){
      presenceOffState();
      // On a companion the token loads async (Keystore) and usually isn't ready at this single
      // boot-time call — without a retry the off-note latches forever (presenceStarted never
      // arms). Re-poll ourselves (bounded ~20s) until the token resolves; renderPresence lives
      // in a different scope than the companion role-settle hook, so retrying here is the fix.
      if(MODE==="companion" && !companionToken() && !presenceStarted && presRetries < 40){
        presRetries++; setTimeout(renderPresence, 500);
      }
      return;
    }
    var note = $("presenceNote"); if(note) note.hidden = true;
    presenceRefresh();
    if(!presenceStarted){ presenceStarted = true; setInterval(presenceRefresh, 15000); }
  }

  // =====================================================================
  // P1 §7#6 — Data export (gear → Privacy & security): client-side
  // Blob download of what THIS panel already loaded (history buffer +
  // house map + zones) — same pattern as the house-map export. Works in
  // every mode (a demo export contains demo data, honestly labeled).
  // =====================================================================
  function dl(name, text, mime){
    try{
      var blob = new Blob([text], { type: mime });
      var url = URL.createObjectURL(blob);
      var a = document.createElement("a");
      a.href = url; a.download = name;
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
      return true;
    }catch(e){ return false; }
  }
  function stamp(){ return new Date().toISOString().slice(0, 10); }
  var expJsonBtn = $("expJson"), expCsvBtn = $("expCsv"), expFbEl = $("expFb");
  function expFb(ok){
    if(typeof actionFeedback === "function") actionFeedback(expFbEl, ok, WavrT("export failed"), "✓ " + WavrT("exported"));
  }
  if(expJsonBtn) expJsonBtn.onclick = function(){
    var doc = {
      format: "wavr-export-v1",
      exported_at: new Date().toISOString(),
      mode: M,
      house: (typeof HOUSE !== "undefined") ? HOUSE : null,
      zones: (typeof zones !== "undefined") ? zones : [],
      history: HIST,
    };
    expFb(dl("wavr-data-" + stamp() + ".json", JSON.stringify(doc, null, 2), "application/json"));
  };
  function csvCell(v){
    var s = String(v == null ? "" : v);
    if(/^[=+\-@]/.test(s)) s = "'" + s;   // spreadsheet formula-injection guard (room names are untrusted)
    return '"' + s.replace(/"/g, '""') + '"';
  }
  if(expCsvBtn) expCsvBtn.onclick = function(){
    var lines = ["ts,room,occupied,confidence,sources,explanation"];
    HIST.forEach(function(h){
      var srcs = (h.sources || []).map(function(s){
        return s ? (s.modality + ":" + (s.presence ? "present" : "empty")) : "";
      }).join(" | ");
      lines.push([csvCell(h.ts), csvCell(h.room), csvCell(h.occupied ? 1 : 0),
                  csvCell(h.confidence), csvCell(srcs), csvCell(h.explanation)].join(","));
    });
    expFb(dl("wavr-history-" + stamp() + ".csv", "\uFEFF" + lines.join("\r\n"), "text/csv"));   // BOM: Excel
  };

  // =====================================================================
  // P1 §7#8 — Device profile drill-down (Network): tap/Enter a device row →
  // expansion with every /api/inventory field. MAC is masked by default,
  // unmasked only on explicit "show" (collapses back on every rebuild).
  // =====================================================================
  var expandedMac = null;
  function ddRow(k, vText){
    var r = elc("div", "dd-row");
    r.appendChild(elc("span", "dk", k));
    r.appendChild(elc("span", "dv", vText));
    return r;
  }
  // v2 device identity: evidence-signal display names (mirrors backend recog precedence).
  var SIGNAL_LABEL = { user_pin:"your pin", upnp:"UPnP", bonjour:"Bonjour / mDNS", snmp:"SNMP",
    dhcp:"DHCP", hostname:"hostname", port_hint:"open ports", oui:"MAC vendor (OUI)",
    mobile_vendor:"mobile-heavy vendor", random_mac:"randomized MAC" };
  // Session-side pin memory: a fresh pin reads back as type_confidence:"high" on the very
  // next GET, but the `user_pin` evidence row only appears after the next scan re-fuses —
  // this map keeps the "pinned" marker honest in that gap (and hides it right after a clear).
  var PIN_SESSION = {};   // mac -> taxonomy type (pinned this session) | null (cleared this session)
  function devPinned(d){
    if(d && Object.prototype.hasOwnProperty.call(PIN_SESSION, d.mac)) return PIN_SESSION[d.mac] != null;
    return !!(d && Array.isArray(d.sources) && d.sources.some(function(s){ return s && s.signal === "user_pin"; }));
  }
  function buildDevDetail(d){
    var box = elc("div", "dev-detail");
    box.appendChild(ddRow(WavrT("name"), d.name || "—"));
    var masked = (typeof maskMac === "function") ? maskMac(d.mac) : String(d.mac || "—");
    var revealed = false;
    var macRow = elc("div", "dd-row");
    macRow.appendChild(elc("span", "dk", "MAC"));
    var macV = elc("span", "dv");
    var macVal = elc("span", "mac", masked);
    var macBtn = elc("button", "linklike", WavrT("show"));
    macBtn.type = "button";
    macBtn.onclick = function(){
      revealed = !revealed;
      macVal.textContent = revealed ? String(d.mac || "—") : masked;
      macBtn.textContent = revealed ? WavrT("hide") : WavrT("show");
    };
    macV.appendChild(macVal);
    macV.appendChild(document.createTextNode(" "));
    macV.appendChild(macBtn);
    macRow.appendChild(macV);
    box.appendChild(macRow);
    box.appendChild(ddRow("IP", d.ip || WavrT("no IP")));
    box.appendChild(ddRow(WavrT("vendor"), d.vendor || WavrT("unknown")));
    // v2 identity block: type icon + label + confidence (+ "pinned" marker), then
    // make/model/OS/open ports ONLY when the backend actually knows them.
    var typeRow = elc("div", "dd-row");
    typeRow.appendChild(elc("span", "dk", WavrT("type")));
    var tval = elc("span", "dv dd-type");
    tval.appendChild(dtypeIconEl(d.device_type, d.type_confidence, true, d.known === false));
    tval.appendChild(dtypeLabelEl(d.device_type, d.type_confidence, d.known === false));
    var confTxt = d.type_confidence === "high" ? WavrT("high confidence")
                : d.type_confidence === "medium" ? WavrT("medium confidence")
                : WavrT("low confidence — a guess");
    // Audit M1: keep the drill-down text consistent with the capped label treatment above —
    // "high" from an untrusted device's own (spoofable) signals is not a confirmed fact.
    if(d.known === false && d.type_confidence === "high") confTxt = WavrT("high signal match — unverified device");
    tval.appendChild(elc("span", "dd-conf", " · " + confTxt));
    if(devPinned(d)) tval.appendChild(elc("span", "pin-tag", WavrT("pinned")));
    typeRow.appendChild(tval);
    box.appendChild(typeRow);
    // make/model/OS VALUES are the device's own strings — DATA; only the row labels translate.
    if(d.make) box.appendChild(ddRow(WavrT("make"), String(d.make)));
    if(d.model) box.appendChild(ddRow(WavrT("model"), String(d.model)));
    if(d.os) box.appendChild(ddRow(WavrT("OS"), String(d.os)));
    if(Array.isArray(d.open_ports) && d.open_ports.length)
      box.appendChild(ddRow(WavrT("open ports"), d.open_ports.join(", ")));
    var fs = (typeof fmtDateTime === "function") ? fmtDateTime(d.first_seen) : null;
    box.appendChild(ddRow(WavrT("first seen on the network"), fs || "—"));
    var ls = (typeof fmtDateTime === "function") ? fmtDateTime(d.last_seen) : null;
    var lr = (typeof fmtRelative === "function") ? fmtRelative(d.last_seen) : null;
    box.appendChild(ddRow(WavrT("last seen"), ls ? (ls + (lr ? " (" + lr + ")" : "")) : "—"));
    box.appendChild(ddRow(WavrT("status"), d.known ? WavrT("trusted (known)") : WavrT("unknown")));
    // v2: explainable evidence trail — every signal recog fused, strongest first
    // (same "why" ethos as the room cards' sensor-consensus drill).
    if(Array.isArray(d.sources) && d.sources.length){
      var ev = elc("div", "dd-evidence");
      ev.appendChild(elc("div", "dd-ev-h", WavrT("Why this type")));
      d.sources.forEach(function(s){
        if(!s || typeof s !== "object") return;
        var line = elc("div", "dd-ev-line");
        // A signal Wavr has a name for gets translated; an unmapped one is a raw backend
        // token (an identifier, not prose) and is printed as-is.
        line.appendChild(elc("span", "dd-ev-sig",
          SIGNAL_LABEL[s.signal] ? WavrT(SIGNAL_LABEL[s.signal]) : String(s.signal || "?")));
        if(s.value != null && String(s.value) !== "")
          line.appendChild(document.createTextNode(" · " + s.value));
        if(typeof s.weight === "number")
          line.appendChild(document.createTextNode(" · " +
            WavrT("weight {pct}%", { pct: Math.round(s.weight * 100) })));
        ev.appendChild(line);
      });
      box.appendChild(ev);
    }
    // v2: user device-type pin — live/hub only (PUT needs the X-Wavr-Local CSRF header,
    // same rule as rename). "Auto" clears the pin (device_type: null on the wire).
    if(M === "live" && d.mac){
      var pinRow = elc("div", "dd-row dd-pin");
      pinRow.appendChild(elc("span", "dk", WavrT("set type")));
      var pv = elc("span", "dv");
      var sel = document.createElement("select");
      sel.className = "dd-pin-select";
      sel.setAttribute("aria-label", WavrT("set the device type (overrides the automatic guess)"));
      sel.setAttribute("data-tip", WavrT("Pin the device type — overrides the automatic guess; saved on the hub"));
      var auto = document.createElement("option");
      auto.value = ""; auto.textContent = WavrT("Auto (no pin)");
      sel.appendChild(auto);
      Object.keys(DTYPE_LABEL).forEach(function(t){
        var o = document.createElement("option");
        o.value = t; o.textContent = WavrT(DTYPE_LABEL[t]);
        sel.appendChild(o);
      });
      sel.value = (devPinned(d) && DTYPE_LABEL[d.device_type]) ? d.device_type : "";
      var pinFb = elc("span", "action-fb");
      pinFb.setAttribute("aria-live", "polite");
      sel.onchange = function(){
        var v = sel.value;
        sel.disabled = true;
        WavrAPI.fetch("/api/inventory/type", {method: "PUT", json: { mac: d.mac, device_type: v || null }})
        .then(function(r){
          if(!r.ok) return r.json().catch(function(){ return null; }).then(function(j){
            throw new Error((j && (j.detail || j.error)) ? String(j.detail || j.error)
                            : WavrT("failed ({status})", { status: r.status }));
          });
          PIN_SESSION[d.mac] = v || null;
          if(typeof actionFeedback === "function")
            actionFeedback(pinFb, true, null, "✓ " + (v ? WavrT("type pinned") : WavrT("pin cleared")));
          // Re-render through the same poll path — the pin reads back on the very next GET
          // (highest precedence), so the row's icon/label update immediately.
          if(typeof window.__wavrNetRefresh === "function") window.__wavrNetRefresh();
          else sel.disabled = false;
        })
        .catch(function(e){
          if(typeof actionFeedback === "function")
            actionFeedback(pinFb, false, (e && e.message) ? String(e.message) : WavrT("failed — try again"));
          sel.disabled = false;
        });
      };
      pv.appendChild(sel);
      pv.appendChild(pinFb);
      pinRow.appendChild(pv);
      box.appendChild(pinRow);
    }
    var act = elc("div", "dd-actions");
    var toDet = elc("button", "linklike", WavrT("see in Devices → Detected"));
    toDet.type = "button";
    toDet.onclick = function(){
      if(typeof window.switchTab === "function") window.switchTab("dispositivos");
      if(typeof window.__wavrSetDsub === "function") window.__wavrSetDsub("detectados");
    };
    act.appendChild(toDet);
    box.appendChild(act);
    return box;
  }
  function attachDevDetail(){
    document.querySelectorAll("#devList .dev-detail").forEach(function(n){ n.remove(); });
    document.querySelectorAll("#devList .dev-row.open").forEach(function(n){
      n.classList.remove("open"); n.setAttribute("aria-expanded", "false");
    });
    if(!expandedMac) return;
    var d = null;
    for(var i = 0; i < invList.length; i++)
      if(invList[i] && invList[i].mac === expandedMac){ d = invList[i]; break; }
    var rows = document.querySelectorAll("#devList .dev-row");
    var row = null;
    for(var j = 0; j < rows.length; j++)
      if(rows[j].dataset.mac === expandedMac){ row = rows[j]; break; }
    if(!d || !row){ expandedMac = null; return; }
    row.classList.add("open");
    row.setAttribute("aria-expanded", "true");
    row.insertAdjacentElement("afterend", buildDevDetail(d));
    applyDevSearch();   // the panel's visibility follows its row's search visibility
  }
  var devListEl = $("devList");
  function devRowActivate(e){
    var t = e.target;
    if(t.closest && t.closest("button, input, form, a, .dev-detail")) return;
    var row = t.closest ? t.closest(".dev-row") : null;
    if(!row || !row.dataset.mac) return;
    expandedMac = (expandedMac === row.dataset.mac) ? null : row.dataset.mac;
    attachDevDetail();
  }
  if(devListEl){
    devListEl.addEventListener("click", devRowActivate);
    devListEl.addEventListener("keydown", function(e){
      // Audit M3: role="button" contract — Space activates too (preventDefault stops page scroll)
      if(e.key !== "Enter" && e.key !== " ") return;
      if(!e.target.classList || !e.target.classList.contains("dev-row")) return;
      e.preventDefault();
      devRowActivate(e);
    });
  }

  // =====================================================================
  // P1 §7#13 — instant device search (Network): pure client-side row filter.
  // =====================================================================
  var devSearchEl = $("devSearch");
  var devQ = "";
  function applyDevSearch(){
    var rows = document.querySelectorAll("#devList .dev-row");
    Array.prototype.forEach.call(rows, function(r){
      var hit = !devQ || norm(r.textContent).indexOf(devQ) >= 0;
      r.classList.toggle("f-hide", !hit);
    });
    var det = document.querySelector("#devList .dev-detail");
    if(det){
      var prev = det.previousElementSibling;
      det.classList.toggle("f-hide", !!(prev && prev.classList.contains("f-hide")));
    }
  }
  if(devSearchEl) devSearchEl.addEventListener("input", function(){
    // Scale: filter the FULL cached device array + re-render a capped window (no
    // re-fetch), so search reaches devices beyond the render cap. Falls back to the
    // old post-render CSS hide only before the Network tab has first rendered.
    window.__wavrDevQ = devSearchEl.value.trim().toLowerCase();
    if(window.__wavrRepaintDev){ window.__wavrRepaintDev(); }
    else { devQ = norm(devSearchEl.value.trim()); applyDevSearch(); }
  });

  // =====================================================================
  // P1 §7#10 — Sensor & system diagnostics (System): composed from client
  // WS state + the frames already flowing. "Panel open" is session
  // uptime (labeled honestly — the hub's own uptime isn't exposed).
  // =====================================================================
  var HEALTH_EN = { fresh: "fresh signal", stale: "aging signal", dead: "no signal" };
  function fmtAgo(ms){
    var s = Math.max(0, Math.floor(ms / 1000));
    if(s < 2) return WavrT("now");
    if(s < 60) return WavrT("{n}s ago", { n: s });
    if(s < 3600) return WavrT("{n}min ago", { n: Math.floor(s / 60) });
    if(s < 86400) return WavrT("{n}h ago", { n: Math.floor(s / 3600) });
    return WavrT("{n}d ago", { n: Math.floor(s / 86400) });
  }
  // The same span with no "ago" — the two callers that wanted an elapsed DURATION used to
  // strip the English suffix with /\ ago$/, which is a no-op the moment "ago" is translated.
  function fmtDur(ms){
    var s = Math.max(0, Math.floor(ms / 1000));
    if(s < 2) return WavrT("now");
    if(s < 60) return WavrT("{n}s", { n: s });
    if(s < 3600) return WavrT("{n}min", { n: Math.floor(s / 60) });
    if(s < 86400) return WavrT("{n}h", { n: Math.floor(s / 3600) });
    return WavrT("{n}d", { n: Math.floor(s / 86400) });
  }
  function diagRow(k, v, cls){
    var r = elc("div", "diag-row");
    r.appendChild(elc("span", "dk", k));
    r.appendChild(elc("span", "dv" + (cls ? " " + cls : ""), v));
    return r;
  }
  function renderDiag(){
    var box = $("diagList");
    if(!box) return;
    box.textContent = "";
    var now = Date.now();
    if(M === "simulated"){
      box.appendChild(diagRow(WavrT("connection"), WavrT("demo — local generator, no network"), "ok"));
    } else if(wsDown){
      var since = wsSince ? WavrFmt.time(wsSince, {hour:"2-digit", minute:"2-digit", second:"2-digit"}) : "?";
      box.appendChild(diagRow(WavrT("connection"),
        WavrT("reconnecting… since {since} · {n} drop(s)", { since: since, n: wsDrops }), "warn"));
    } else {
      box.appendChild(diagRow(WavrT("connection"),
        WavrT("real-time (WebSocket) — live")
        + (wsDrops ? " · " + WavrT("{n} reconnection(s) this session", { n: wsDrops }) : ""), "ok"));
    }
    box.appendChild(diagRow(WavrT("last event"), lastEventAt ? fmtAgo(now - lastEventAt) : WavrT("none yet"),
      (lastEventAt && now - lastEventAt > 30000) ? "warn" : null));
    box.appendChild(diagRow(WavrT("events received"), String(evtCount)));
    box.appendChild(diagRow(WavrT("panel open"),
      WavrT("{duration} (this session, not the hub)", { duration: fmtDur(now - bootAt) })));
    Object.keys(srcSeen).sort().forEach(function(m){
      var e = srcSeen[m];
      // e.health is a backend token; only the words Wavr itself supplies get translated.
      var health = HEALTH_EN[e.health] ? WavrT(HEALTH_EN[e.health]) : (e.health || "—");
      box.appendChild(diagRow(lbl(m),
        WavrT("{health} · last event {ago}", { health: health, ago: fmtAgo(now - e.at) }),
        e.health === "fresh" ? "ok" : e.health ? "warn" : null));
    });
  }
  setInterval(function(){
    if(document.hidden) return;
    var p = $("panel-sistema");
    if(!p || !p.classList.contains("active")) return;   // only tick while Sistema is on screen
    renderDiag();
  }, 2000);

  // =====================================================================
  // A2.3 — Health check (System): USER-TRIGGERED ONLY. GET /api/health pings
  // the gateway and, when the hub has WAVR_HEALTH_RESOLVERS on, three public
  // DNS resolvers (1.1.1.1/8.8.8.8/9.9.9.9) on EVERY call — same egress class
  // as the AI Narrator, so the confirm shape is identical (two-step, focus
  // moves to Confirm on open / back to the trigger on cancel).
  //
  // LOAD-BEARING: this fires EXACTLY ONE /api/health call per confirmed
  // click. Do NOT add a setInterval/poll around this — that would turn a
  // disclosed, user-invoked check into a silent recurring egress ping
  // (invariant violation; mirrors the narrator's own "only egress" rule).
  // Any future topbar mirror must read the last client-side result only,
  // never fetch on its own.
  // =====================================================================
  var HEALTH_SEV_CLASS = { ok:"ok", minor:"warn", degraded:"warn", major:"danger", critical:"danger" };
  // Tri-state on purpose: statusInfo not yet loaded (no /api/status response since page open)
  // is UNKNOWN, not "off" — defaulting an unknown resolver state to "off" would understate a
  // possible egress, so "unknown" gets the same full-disclosure copy as "known on". Only a
  // CONFIRMED features.health_resolvers===false counts as known-off.
  function healthResolversState(){
    if(!(statusInfo && statusInfo.features && typeof statusInfo.features === "object")) return "unknown";
    return statusInfo.features.health_resolvers === false ? "off" : "on";
  }
  function renderHealthTile(){
    var tile = $("healthCheck");
    if(!tile) return;
    // Fix D: admin (central) companion also gets the Health check tile -- companionIsCentral()
    // reads the SAME GET /api/devices/me-confirmed cache renderControls uses; a 'user'
    // companion (or an unresolved/unconfirmed companion) keeps it hidden.
    tile.hidden = !(M === "live" || (M === "companion" && companionIsCentral()));
    var state = healthResolversState();
    var cr = $("healthConfirmResolvers");
    if(cr) cr.hidden = (state === "off");
    var note = $("healthNote");
    if(note){
      note.textContent = state === "off"
        ? WavrT("Local checks only — this hub has public-resolver checks off (WAVR_HEALTH_RESOLVERS), so “Check now” only tests your gateway. Nothing leaves your network.")
        : state === "on"
        ? WavrT("This check contacts your gateway and 3 public DNS servers outside your network — only when you click “Check now” and confirm, and only once per click.")
        : WavrT("This check contacts your gateway and, if the hub has public-resolver checks on, 3 public DNS servers outside your network — only when you click “Check now” and confirm, and only once per click.");
    }
  }
  // 5 tiers → icon + plain words, never color alone (A2.5 #1 / WCAG 1.4.1).
  var HEALTH_SEV_TEXT = {
    ok: "everything reachable",
    minor: "one check failed",
    degraded: "several checks failing",
    major: "gateway OK — the internet looks down",
    critical: "gateway unreachable — no LAN routing",
  };
  // One phrasing for both the passive status line and the interactive result, so the
  // severity word and its sentence can never end up in two different languages. An
  // unrecognised severity keeps the hub's own raw token — a value, not a phrase.
  function healthSevLine(sev){
    var txt = HEALTH_SEV_TEXT[sev];
    if(!txt) return WavrT("Health: {sev}", { sev: String(sev == null ? "" : sev) });
    return WavrT("Health: {sev} — {text}", { sev: WavrT(sev), text: WavrT(txt) });
  }
  // Severity vocabularies (OQ4 decision — keep two domain ladders): this ok/minor/degraded/
  // major/critical scale is the canonical HEALTH ladder. The Network-alerts ladder (info/note/
  // watch/alert/critical, see :427) is a deliberately separate domain vocabulary (per-alert-kind
  // badge, backend-driven string) and stays as-is — they are not force-merged. No shared-severity
  // consumer needs a cross-map today, so none is defined (avoids dead code).
  // =====================================================================
  // System regroup — persistent, PASSIVE Health status line (card C): fed
  // ONLY from signals already polled elsewhere (sysInfo from /api/system,
  // wsDown from the live WebSocket) — zero new calls, and NEVER touches
  // GET /api/health (that stays user-triggered only, see the load-bearing
  // comment above renderHealthTile). Reuses the SAME canonical HEALTH_SEV_*
  // vocabulary as the interactive check above as the one shared severity
  // ladder. major/critical require the gateway/resolver reachability data
  // only the interactive (egress) check has, so the passive line never
  // asserts them — it only ever reports what it can actually verify.
  // =====================================================================
  function computePassiveHealthSeverity(){
    if(M !== "live") return null;   // no hub connected — the line stays hidden, not "ok"
    // Deliberately paused (master System switch off): nothing is sensing, so this line has
    // no health signal to assert. Hide it rather than count enabled-but-inactive sources as
    // "degraded" — that would false-alarm on the exact case the switch describes (paused,
    // not failing), contradicting the honest "not an error" chips on the same tab.
    if(sysInfo && sysInfo.running === false) return null;
    var unavailN = 0;
    if(sysInfo && Array.isArray(sysInfo.sources)){
      sysInfo.sources.forEach(function(s){ if(s && s.enabled && !s.active) unavailN++; });
    }
    if(unavailN >= 2) return "degraded";
    if(unavailN === 1 || wsDown) return "minor";
    return "ok";
  }
  function renderHealthStatusLine(){
    var el = $("healthStatusLine");
    if(!el) return;
    var sev = computePassiveHealthSeverity();
    if(sev == null){ el.hidden = true; return; }
    el.hidden = false;
    el.className = "egress-summary health-sev " + (HEALTH_SEV_CLASS[sev] || "na");
    el.textContent = healthSevLine(sev);
  }
  function renderHealthResult(d){
    var box = $("healthResult");
    if(!box) return;
    box.hidden = false;
    box.textContent = "";
    var sevCls = HEALTH_SEV_CLASS[d.severity] || "na";
    // Icon + text label carry the tier, never color alone (A2.5 #1) — 5 tiers roll
    // into the codebase's existing ok/warn/danger palette (no new colors invented).
    var line = elc("p", "egress-summary health-sev " + sevCls);
    var NS = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(NS, "svg");
    svg.setAttribute("aria-hidden", "true");
    var use = document.createElementNS(NS, "use");
    use.setAttribute("href", d.severity === "ok" ? "#ic-check" : "#ic-alert");
    svg.appendChild(use);
    line.appendChild(svg);
    var sev = d.severity || "unknown";
    line.appendChild(document.createTextNode(healthSevLine(sev)));
    box.appendChild(line);
    var list = elc("div", "diag-list");
    // Hosts/IPs are network DATA — the row label is a frame around them, never a rewrite.
    var gw = (d.gateway && typeof d.gateway === "object") ? d.gateway : {};
    list.appendChild(diagRow(WavrT("{host} (gateway)", { host: gw.host || WavrT("gateway") }),
      gw.ok ? WavrT("reachable") : WavrT("unreachable"), gw.ok ? "ok" : "danger"));
    var resolvers = (d.resolvers && typeof d.resolvers === "object") ? d.resolvers : {};
    Object.keys(resolvers).forEach(function(host){
      list.appendChild(diagRow(WavrT("{host} (public resolver)", { host: host }),
        resolvers[host] ? WavrT("reachable") : WavrT("unreachable"), resolvers[host] ? "ok" : "warn"));
    });
    var extra = (d.extra && typeof d.extra === "object") ? d.extra : {};
    Object.keys(extra).forEach(function(host){
      list.appendChild(diagRow(WavrT("{host} (extra target)", { host: host }),
        extra[host] ? WavrT("reachable") : WavrT("unreachable"), extra[host] ? "ok" : "warn"));
    });
    box.appendChild(list);
    if(!Object.keys(resolvers).length){
      // data-driven, not flag-driven: an empty resolvers dict in the RESPONSE means
      // this check really only touched the gateway — say so where the result is read.
      box.appendChild(elc("p", "panel-note", WavrT("local checks only — public-resolver checks are off on this hub")));
    }
    box.appendChild(elc("p", "panel-note pres-n",
      WavrT("checked at {time} — point-in-time snapshot, never refreshes on its own",
            { time: WavrFmt.time(new Date()) })));
  }
  (function(){
    var btn = $("healthBtn"), out = $("healthOut"), box = $("healthConfirm");
    var yes = $("healthYes"), no = $("healthNo"), checking = $("healthChecking");
    if(!btn || !box) return;
    var inflight = false;   // spam guard alongside btn.disabled (belt + suspenders)
    function runHealthCheck(){
      if(inflight) return;
      inflight = true;
      box.hidden = true;
      btn.disabled = true;
      if(checking) checking.hidden = false;
      var DEFAULT_OUT = WavrT("Runs an on-demand check of your gateway plus, if enabled on the hub, three public DNS resolvers (1.1.1.1, 8.8.8.8, 9.9.9.9) to tell LAN problems from internet problems.");
      // Exactly ONE /api/health call per invocation — see the load-bearing block
      // comment above. Never add a poll/interval around this fetch. cache:"no-store" so a
      // repeat click always re-triggers a real check instead of risking a stale browser-
      // cached GET response (this route can have a genuine external side effect per call).
      // Fix D: admin companion sends its Bearer instead of the loopback-only X-Wavr-Local
      // header (require_local admits root+X-Wavr-Local OR an authenticated central peer);
      // native mobile routes through the pinned fetch so the token never hits app https://localhost.
      (function(){
        var companionCentral = (M === "companion" && companionIsCentral());
        var headers = companionCentral
          ? { "Authorization": "Bearer " + companionToken() }
          : { "X-Wavr-Local": "1" };
        var __nf = window.WAVR_MOBILE
          ? function(p,o){ return window.WAVR_MOBILE.netFetch(window.WAVR_MOBILE.base + p, o); }
          : function(p,o){ return fetch(location.origin + p, o); };
        return __nf("/api/health", { headers: headers, cache: "no-store" });
      })()
        .then(function(r){ if(!r.ok) throw new Error(String(r.status)); return r.json(); })
        .then(function(d){
          renderHealthResult(d || {});
          if(out){ out.className = "narrate-out muted"; out.textContent = DEFAULT_OUT; }
        })
        .catch(function(err){
          if(out){
            out.className = "narrate-out muted";
            out.textContent = (err && err.message === "404")
              ? WavrT("not available on this hub version — restart/update the Wavr hub")
              : WavrT("connection failed — couldn't reach the hub");
          }
        })
        .finally(function(){
          inflight = false;
          if(checking) checking.hidden = true;
          btn.disabled = false;
          // keyboard users: disabling the focused trigger dropped focus to <body> —
          // restore it, but never steal focus from anywhere else the user went.
          var ae = document.activeElement;
          if(!ae || ae === document.body) btn.focus();
        });
    }
    // Step 1: resolvers CONFIRMED off (features.health_resolvers === false) → the check
    // is gateway-only/local, no egress to disclose — run directly. On or unknown → arm
    // the two-step confirm with the full public-DNS disclosure (never fetches by itself).
    btn.onclick = function(){
      if(inflight) return;
      if(healthResolversState() === "off"){ runHealthCheck(); return; }
      box.hidden = false; btn.disabled = true;
      if(yes) yes.focus();
    };
    if(no) no.onclick = function(){ box.hidden = true; btn.disabled = false; btn.focus(); };
    // Step 2 (confirmed): the ONLY caller of runHealthCheck besides the local-only shortcut.
    if(yes) yes.onclick = runHealthCheck;
    // A2.5 #2: Esc cancels the confirm, focus returns to the trigger button.
    document.addEventListener("keydown", function(e){
      if(e.key === "Escape" && box && !box.hidden){ box.hidden = true; btn.disabled = false; btn.focus(); }
    });
  })();

  // =====================================================================
  // network-doctor (Deep network diagnosis): GET /api/health/doctor reuses
  // already-polled gateway/dhcp/source/camera/mdns/inventory state for every
  // check EXCEPT the dns leg, which — same as /api/health — freshly pings 3
  // public DNS resolvers whenever egress is allowed on this hub, and is
  // SKIPPED (gateway/LAN-only) the moment the operator blocks egress. Any
  // auto-fix only cycles an ALREADY-enabled source, re-announces THIS hub's
  // own mDNS record, or re-scans the LAN — see net_doctor.py's SAFE-AUTO
  // allowlist. No two-step confirm here (unlike the Health check above):
  // egress on/off is the operator's own standing choice on this hub, not a
  // per-click decision — the doctor just respects whatever that switch
  // already says, same immediate-action precedent as the kill-switch/
  // System-power controls otherwise. Visibility mirrors renderHealthTile
  // (live mode only), painted off the same statusInfo poll (zero new calls
  // until the button is actually clicked).
  // =====================================================================
  function renderDoctorTile(){
    var tile = $("doctorCheck");
    if(!tile) return;
    // Fix D: same admin-companion allowance as renderHealthTile above.
    tile.hidden = !(M === "live" || (M === "companion" && companionIsCentral()));
  }
  function doctorLbl(id){
    return String(id == null ? "" : id).replace(/_/g, " ").replace(/:/g, ": ");
  }
  // discovery_reach (CL-02): map the STRUCTURED verdict's copy_key to plain-language,
  // hypothesis-framed copy ("provavelmente" — ADR-0003) with the two numbers. Only the
  // PROBLEM case (multicast_dead) gets a prominent callout; healthy / small-net / probe-
  // unavailable just ride as a normal check row. PR2 splits multicast_dead into isolation
  // vs second-network (each with its own key here); PR3 wires the "Como arrumar" link.
  var DISCOVERY_COPY = {
    // PR2 discriminates the cause. HARD RULE: the router is only ever named when the hub's
    // multicast viability was PROVEN (discovery_ap_isolation / discovery_second_network). When
    // it could NOT be proven the copy points at the hub's own environment, never the router.
    // Each case: what Wavr sees + what still works + what the user can do (ADR-0003 honest).
    // The SOURCE language is English and the catalogue key IS the English
    // string, so copy here is written in English and translated at paint time
    // through `WavrT`. This line used to read "Product language is ENGLISH
    // (v0.2.0 decision) — all user-facing copy here stays EN", which stopped
    // being true at the localisation pass and, worse, told the next author not
    // to mark their strings.
    discovery_host_unavailable: function(v){
      return {
        cls: "warn",
        title: WavrT("This hub isn't receiving discovery traffic"),
        body: WavrT("Wavr can see {n} devices on your network, but the device Wavr runs on"
          + " isn't receiving the network's discovery (multicast) traffic — so this is not your"
          + " router's fault. This is common when Wavr runs inside a restricted environment"
          + " (a container). Network detection keeps working normally. What you can do: run the Core"
          + " on a computer/laptop on the same network, or check that the router doesn't have client"
          + " isolation turned on.", { n: v.arp_count })
      };
    },
    discovery_ap_isolation: function(v){
      return {
        cls: "warn",
        title: WavrT("Your network is blocking discovery"),
        body: WavrT("Wavr can see {n} devices and confirmed it receives multicast from the"
          + " network — yet the devices don't answer name discovery. Most likely the router is"
          + " filtering that traffic (client isolation / \"AP isolation\", or an mDNS filter)."
          + " Network detection keeps working. What to do: look for \"client isolation\" / \"AP"
          + " isolation\" in your router settings and turn it off. Some ISP routers don't expose"
          + " these switches at all — not finding them is normal, see the guide.", { n: v.arp_count })
      };
    },
    discovery_second_network: function(v){
      return {
        cls: "warn",
        title: WavrT("Your devices are on separate networks"),
        body: WavrT("Wavr can see {n} devices, but they appear to be on more than one"
          + " network (different subnets or more than one DHCP server). A separate IoT/guest network"
          + " stops discovery from crossing between them. Network detection keeps working. What to"
          + " do: put Wavr on the same network/VLAN as the devices you want discovered.", { n: v.arp_count })
      };
    },
    // Neutral fallback: viability could not be established -> DO NOT blame the router (hard rule).
    discovery_multicast_dead: function(v){
      return {
        cls: "warn",
        title: WavrT("Wavr can't discover your devices"),
        body: WavrT("Wavr can see {n} devices on your network, but only {answered}"
          + " answered name discovery. Network detection keeps working — but discovery (names/mDNS)"
          + " isn't getting through, and the cause couldn't be confirmed yet (it may be a filter on"
          + " the network or a limitation of the device Wavr runs on). Wavr won't point fingers"
          + " without being sure.", { n: v.arp_count, answered: Math.max(0, v.mcast_responders) })
      };
    }
  };
  // PR3: local, no-egress "how to fix" — keyed by the DETECTED cause's copy_key. Steps say WHAT
  // to change; the router search says WHERE (per-router), with the full guide's filename in the
  // public repo (docs/network-fixes/). Nothing here calls out to the internet. English-only copy.
  var FIX_GUIDE = {
    discovery_ap_isolation: { steps: [
      "In your router admin, look for \"client isolation\" / \"AP isolation\" and turn it OFF for the network Wavr and your devices are on.",
      "Make sure Wavr and your devices are NOT on the guest network — it isolates on purpose.",
      "Some ISP routers expose none of these switches — not finding them is normal; see your router's guide below.",
      "Apply and run the diagnosis again." ] },
    discovery_second_network: { steps: [
      "Your devices appear to be on more than one network (different subnets or 2 DHCP servers).",
      "Put Wavr on the SAME network/VLAN as the devices you want discovered (e.g. off the guest/IoT network).",
      "If you use mesh or a second router, that's usually where the second network comes from." ] },
    discovery_host_unavailable: { steps: [
      "The device Wavr runs on isn't receiving the network's discovery traffic — this is not your router's fault.",
      "If the Core runs inside a restricted environment (container), run it on a computer/laptop on the same network.",
      "Alternatively, check that the router doesn't have client isolation turned on." ] },
    discovery_multicast_dead: { steps: [
      "The cause couldn't be confirmed — work through the quick network self-check.",
      "Check: client isolation off, Wavr and devices on the same network, multicast/mDNS allowed.",
      "Full requirements guide in the repo: docs/network-fixes/wavr-network-requirements.md" ] }
  };
  // Searchable router database — match by brand, ISP, model OR country so the list is never
  // an Ireland-only dropdown. `kw` is a lowercase haystack of names/ISPs/models/countries.
  var ROUTER_DB = [
    { name: "Virgin Media Hub", kw: "virgin media hub 3 4 5 ireland uk liberty global",
      tip: "Advanced settings → Wireless → Guest network. Main-network isolation is rare on these; the usual culprit is the guest SSID.",
      doc: "virgin-media-hub.md" },
    { name: "eir (F3000 / F2000)", kw: "eir ireland f3000 f2000",
      tip: "Wi-Fi → Advanced → \"Isolate clients\" / \"AP isolation\" → off. Keep Wavr and devices off the guest network.",
      doc: "eir.md" },
    { name: "Sky Hub / Max Hub", kw: "sky hub max broadband uk ireland italy",
      tip: "\"Broadband Shield\" is content filtering, NOT isolation. Locked-down hub with no toggles → use your own router behind it.",
      doc: "sky.md" },
    { name: "Vodafone (Ultra Hub / Station)", kw: "vodafone ultra hub station thg3000 ireland uk germany italy spain portugal netherlands",
      tip: "Stock Vodafone hubs expose few or none of these toggles — not finding them is normal, not a mistake. Check the guest network; if discovery still fails, the practical route is your own router/AP behind it.",
      doc: "vodafone.md" },
    { name: "TP-Link (Archer / Deco)", kw: "tp-link tplink archer deco mesh",
      tip: "Archer: Advanced → Wireless → untick \"AP Isolation\" (per band). Deco app: More → IoT Network (that network isolates).",
      doc: "tp-link.md" },
    { name: "UniFi (Ubiquiti)", kw: "unifi ubiquiti dream machine udm cloud gateway",
      tip: "SSID → Advanced → \"Client Device Isolation\" off. An IoT VLAN is the usual cause → enable the mDNS reflector or merge the networks.",
      doc: "unifi.md" }
  ];
  var ROUTER_GENERIC = {
    name: "Any other router",
    tip: "In the Wi-Fi settings, hunt for the words: \"client isolation\" / \"AP isolation\", \"guest\" network, and \"mDNS\" / \"multicast\". Many ISP routers expose none of these — that's normal; the requirements guide covers what to check instead.",
    doc: "wavr-network-requirements.md"
  };
  function discoveryFixPanel(copyKey){
    var g = FIX_GUIDE[copyKey];
    if(!g) return null;
    var wrap = elc("div", "dv-fix");
    var ol = document.createElement("ol");
    g.steps.forEach(function(s){ ol.appendChild(elc("li", "", WavrT(s))); });
    wrap.appendChild(ol);
    wrap.appendChild(elc("label", "", WavrT("Find your router — search by brand, ISP or country")));
    var inp = document.createElement("input");
    inp.type = "search"; inp.className = "dv-router-search";
    inp.placeholder = WavrT("e.g. Vodafone, TP-Link, Ireland…");
    inp.setAttribute("aria-label", WavrT("search router brand, ISP or country"));
    wrap.appendChild(inp);
    var hits = elc("div", "dv-router-hits");
    wrap.appendChild(hits);
    var tip = elc("p", "dv-brand-tip"); var doc = elc("p", "dv-doc");
    wrap.appendChild(tip); wrap.appendChild(doc);
    // b.name is the router's brand/model — DATA. b.doc is a filename. Only the prose moves.
    function show(b){ tip.textContent = WavrT(b.tip);
      doc.textContent = WavrT("Full guide (in the repository): docs/network-fixes/{file}", { file: b.doc }); }
    function render(){
      var q = (inp.value || "").trim().toLowerCase();
      hits.textContent = "";
      var matches = !q ? ROUTER_DB
        : ROUTER_DB.filter(function(b){
            return (b.name.toLowerCase() + " " + b.kw).indexOf(q) !== -1; });
      matches.forEach(function(b){
        var btn = elc("button", "dv-router-hit", b.name);
        btn.type = "button";
        btn.addEventListener("click", function(){ show(b); });
        hits.appendChild(btn);
      });
      var other = elc("button", "dv-router-hit", WavrT(ROUTER_GENERIC.name));
      other.type = "button";
      other.addEventListener("click", function(){ show(ROUTER_GENERIC); });
      hits.appendChild(other);
      if(q && !matches.length) show(ROUTER_GENERIC);
    }
    inp.addEventListener("input", render);
    render(); show(ROUTER_GENERIC);
    return wrap;
  }
  function discoveryVerdictCallout(checks){
    var dr = null;
    for(var i=0;i<checks.length;i++){ if(checks[i] && checks[i].id === "discovery_reach"){ dr = checks[i]; break; } }
    if(!dr || !dr.verdict) return null;
    var mk = DISCOVERY_COPY[dr.verdict.copy_key];
    if(!mk) return null;
    var info = mk(dr.verdict);
    var el = elc("div", "doctor-verdict " + info.cls);
    el.appendChild(elc("p", "dv-title", info.title));
    el.appendChild(elc("p", "dv-body", info.body));
    // [Como arrumar] — only for causes that have an actionable guide (never for ok/small/probe).
    if(FIX_GUIDE[dr.verdict.copy_key]){
      var btn = elc("button", "dv-fix-btn", WavrT("How to fix"));
      btn.type = "button";
      var panel = null;
      btn.addEventListener("click", function(){
        if(panel){ panel.remove(); panel = null; btn.textContent = WavrT("How to fix"); return; }
        panel = discoveryFixPanel(dr.verdict.copy_key);
        if(panel){ el.appendChild(panel); btn.textContent = WavrT("Hide"); }
      });
      el.appendChild(btn);
    }
    return el;
  }
  // Local SVG-sprite helper for the diagnostics UI. This script block is scope-isolated from
  // the one that defines the shared icon() helper (each block is its own closure — calling
  // icon() from here is a ReferenceError that the doctor's .catch silently swallowed, which is
  // exactly how the Copy/Send report buttons vanished on the kiosk). Self-contained on purpose.
  function dvIcon(sym){
    var NS = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(NS, "svg");
    svg.setAttribute("aria-hidden", "true");
    var use = document.createElementNS(NS, "use");
    use.setAttribute("href", "#" + sym);
    svg.appendChild(use);
    return svg;
  }
  // PR4: fetch just the diagnosis (no auto_fix — a setup surface must never trigger fixes) so the
  // end-of-setup step can surface the discovery verdict when a device doesn't show up. Same auth
  // dance as the doctor button; returns the parsed JSON or null (never throws at the call site).
  function fetchDiscoveryVerdict(){
    var companionCentral = (M === "companion" && companionIsCentral());
    var headers = companionCentral
      ? { "Authorization": "Bearer " + companionToken() }
      : { "X-Wavr-Local": "1" };
    var __nf = window.WAVR_MOBILE
      ? function(p,o){ return window.WAVR_MOBILE.netFetch(window.WAVR_MOBILE.base + p, o); }
      : function(p,o){ return fetch(location.origin + p, o); };
    return __nf("/api/health/doctor", { headers: headers, cache: "no-store" })
      .then(function(r){ return r.ok ? r.json() : null; })
      .catch(function(){ return null; });
  }
  function renderDoctorResult(d){
    var box = $("doctorResult");
    if(!box) return;
    box.hidden = false;
    box.textContent = "";
    var checks = Array.isArray(d.checks) ? d.checks : [];
    var fixed = Array.isArray(d.auto_fixed) ? d.auto_fixed : [];
    var suggestions = Array.isArray(d.suggestions) ? d.suggestions : [];
    box.appendChild(elc("p", "panel-note pres-n",
      WavrT("{checks} check(s) · {fixed} auto-fixed · {suggestions} suggestion(s)",
            { checks: checks.length, fixed: fixed.length, suggestions: suggestions.length })));
    var dvCallout = discoveryVerdictCallout(checks);
    if(dvCallout) box.appendChild(dvCallout);
    var list = elc("div", "diag-list");
    checks.forEach(function(c){
      var cls = (c && c.ok === true) ? "ok"
        : (c && c.ok === false) ? (HEALTH_SEV_CLASS[c.severity] || "danger")
        : null;   // ok===null — honest "not applicable" (monitor off), never fabricated good/bad
      list.appendChild(diagRow(doctorLbl(c && c.id), (c && c.detail) || "—", cls));
    });
    box.appendChild(list);
    if(fixed.length){
      // "safe/local only" per net_doctor's SAFE-AUTO allowlist — never the router or other devices.
      box.appendChild(elc("p", "panel-note", WavrT("Auto-fixed just now — safe, local-only:")));
      var fl = elc("div", "diag-list");
      var now = Date.now();
      fixed.forEach(function(a){
        var age = (a && a.ts) ? fmtAgo(now - Date.parse(a.ts)) : "";
        fl.appendChild(diagRow(doctorLbl(a && a.kind) + ": " + ((a && a.target) || "—"),
          ((a && a.detail) || "") + (age ? " · " + age : ""), "ok"));
      });
      box.appendChild(fl);
    }
    if(suggestions.length){
      box.appendChild(elc("p", "panel-note", WavrT("Suggested — needs you:")));
      var sl = elc("div", "diag-list");
      suggestions.forEach(function(s){
        sl.appendChild(diagRow(doctorLbl(s && s.id),
          ((s && s.message) || "") + ((s && s.action_hint) ? " (" + s.action_hint + ")" : ""), "warn"));
      });
      box.appendChild(sl);
    }
    // PR4: copy-a-shareable-report (flutter-doctor pattern). The report string is built + MAC-
    // redacted server-side; the button copies it verbatim. Nothing leaves the device unless the
    // user pastes it somewhere themselves.
    if(typeof d.report === "string" && d.report){
      var rep = elc("div", "dv-report");
      var row = elc("div", "dv-report-row");
      var rbtn = elc("button", "dv-report-btn", WavrT("Copy diagnostic report"));
      rbtn.type = "button";
      rbtn.addEventListener("click", function(){
        var done = function(){ rbtn.textContent = WavrT("Copied ✓"); setTimeout(function(){
          rbtn.textContent = WavrT("Copy diagnostic report"); }, 2000); };
        if(navigator.clipboard && navigator.clipboard.writeText){
          navigator.clipboard.writeText(d.report).then(done, function(){ done(); });
        } else {
          var ta = document.createElement("textarea"); ta.value = d.report;
          document.body.appendChild(ta); ta.select();
          try { document.execCommand("copy"); } catch(e) {}
          document.body.removeChild(ta); done();
        }
      });
      row.appendChild(rbtn);
      // Authed fetch for the diagnostics actions — same header dance as the doctor call.
      var diagFetch = function(path, opts){
        var companionCentral = (M === "companion" && companionIsCentral());
        opts = opts || {};
        opts.headers = Object.assign(
          companionCentral ? { "Authorization": "Bearer " + companionToken() }
                           : { "X-Wavr-Local": "1" },
          opts.headers || {});
        var __nf = window.WAVR_MOBILE
          ? function(p,o){ return window.WAVR_MOBILE.netFetch(window.WAVR_MOBILE.base + p, o); }
          : function(p,o){ return fetch(location.origin + p, o); };
        return __nf(path, opts);
      };
      // MANUAL send — the tap IS the consent for this one send. Carries the universal
      // #ic-egress pictogram ("this leaves your home"). Honest failure texts per cause.
      var sbtn = elc("button", "dv-report-btn dv-send");
      sbtn.type = "button";
      sbtn.appendChild(dvIcon("ic-egress"));
      sbtn.appendChild(document.createTextNode(WavrT("Send report")));
      var sbtnReset = function(txt){ sbtn.textContent = ""; sbtn.appendChild(dvIcon("ic-egress"));
        sbtn.appendChild(document.createTextNode(txt)); };
      sbtn.addEventListener("click", function(){
        sbtn.disabled = true; sbtnReset(WavrT("Sending…"));
        diagFetch("/api/health/doctor/send", { method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ report: d.report }) })
          .then(function(r){ return r.json().then(function(j){ return { s: r.status, j: j }; }); })
          .then(function(res){
            if(res.s === 200 && res.j && res.j.ok){ sbtnReset(WavrT("Sent ✓")); }
            else if(res.s === 409){ sbtnReset(WavrT("Unavailable")); }
            else { sbtnReset(WavrT("Failed — try again")); }
          })
          .catch(function(){ sbtnReset(WavrT("Failed — offline?")); })
          .then(function(){ sbtn.disabled = false;
            setTimeout(function(){ sbtnReset(WavrT("Send report")); }, 3000); });
      });
      row.appendChild(sbtn);
      rep.appendChild(row);
      rep.appendChild(elc("p", "dv-report-note",
        WavrT("Copy: paste into a GitHub issue to ask for help. Send: ships this report outside your"
        + " local network to the Wavr diagnostics endpoint — one send, only when you tap. MACs are"
        + " already redacted (aa:bb:cc:**:**:**); Wavr never sends anything on its own.")));
      // AUTO-SEND opt-in toggle (standing consent) — wired to the `diagnostics` connector.
      // The enable route is loopback-root only, so the toggle is hidden on companions
      // (they'd 403); the Connectors screen remains the canonical surface for it.
      if(M !== "companion"){
        diagFetch("/api/connectors", {}).then(function(r){ return r.ok ? r.json() : null; })
          .then(function(list){
            var c = null;
            (Array.isArray(list) ? list : (list && list.connectors) || []).forEach(function(x){
              if(x && x.id === "diagnostics") c = x; });
            if(!c) return;
            var wrap = elc("div", "dv-auto");
            var cb = document.createElement("input");
            cb.type = "checkbox"; cb.id = "dvAutoDiag";
            cb.checked = (c.override === "on");
            var lab = document.createElement("label");
            lab.className = "dv-auto-label"; lab.htmlFor = "dvAutoDiag";
            lab.appendChild(dvIcon("ic-egress"));
            lab.appendChild(document.createTextNode(WavrT("Auto-send diagnostics when a problem is found")));
            var note = elc("p", "dv-report-note",
              WavrT("Opt-in — off by default. When ON, Wavr automatically sends a MAC-redacted"
              + " diagnostic report OUTSIDE your local network whenever a diagnosis finds a"
              + " problem. Nothing else is ever sent, and Wavr never turns this on by itself.")
              + (c.available ? "" : " " + WavrT("(Needs a diagnostics endpoint configured — WAVR_DIAG_ENDPOINT.)")));
            cb.addEventListener("change", function(){
              cb.disabled = true;
              diagFetch("/api/connectors/diagnostics/enable", { method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ enabled: cb.checked }) })
                .then(function(r){ if(!r.ok){ cb.checked = !cb.checked; } })
                .catch(function(){ cb.checked = !cb.checked; })
                .then(function(){ cb.disabled = false; });
            });
            var col = elc("div");
            col.appendChild(lab); col.appendChild(note);
            wrap.appendChild(cb); wrap.appendChild(col);
            rep.appendChild(wrap);
          }).catch(function(){});
      }
      box.appendChild(rep);
    }
    box.appendChild(elc("p", "panel-note pres-n",
      WavrT("checked at {time} — point-in-time snapshot, never refreshes on its own",
            { time: WavrFmt.time(new Date()) })));
  }
  (function(){
    var btn = $("doctorBtn"), out = $("doctorOut"), checking = $("doctorChecking");
    if(!btn) return;
    var inflight = false;   // spam guard alongside btn.disabled (belt + suspenders)
    var DEFAULT_OUT = out ? out.textContent : "";
    btn.onclick = function(){
      if(inflight) return;
      inflight = true;
      btn.disabled = true;
      if(checking) checking.hidden = false;
      // Single GET per click. auto_fix=true is this call's half of the two-factor auto-fix
      // gate (net_doctor.apply_fixes) -- the hub's own WAVR_NET_DOCTOR_AUTOFIX setting is the
      // other half. When the hub has it off, the route still runs (diagnose-only) and every
      // fixable item comes back as a suggestion instead -- exactly as renderDoctorResult shows it,
      // never assumed as fixed.
      // Fix D: admin companion sends its Bearer instead of the loopback-only X-Wavr-Local
      // header (same require_local rule as the Health check above); native mobile routes
      // through the pinned fetch so the token never hits app https://localhost.
      (function(){
        var companionCentral = (M === "companion" && companionIsCentral());
        var headers = companionCentral
          ? { "Authorization": "Bearer " + companionToken() }
          : { "X-Wavr-Local": "1" };
        var __nf = window.WAVR_MOBILE
          ? function(p,o){ return window.WAVR_MOBILE.netFetch(window.WAVR_MOBILE.base + p, o); }
          : function(p,o){ return fetch(location.origin + p, o); };
        return __nf("/api/health/doctor?auto_fix=true", { headers: headers, cache: "no-store" });
      })()
        .then(function(r){ if(!r.ok) throw new Error(String(r.status)); return r.json(); })
        .then(function(d){
          renderDoctorResult(d || {});
          if(out){ out.className = "narrate-out muted"; out.textContent = DEFAULT_OUT; }
        })
        .catch(function(err){
          if(out){
            out.className = "narrate-out muted";
            out.textContent = (err && err.message === "404")
              ? WavrT("not available on this hub version — restart/update the Wavr hub")
              : WavrT("connection failed — couldn't reach the hub");
          }
        })
        .finally(function(){
          inflight = false;
          if(checking) checking.hidden = true;
          btn.disabled = false;
          var ae = document.activeElement;
          if(!ae || ae === document.body) btn.focus();
        });
    };
  })();

  // =====================================================================
  // P1 §7#14 — PWA install prompt (gear → Sobre): browser event capture
  // only, zero network. Gracefully absent where unsupported/ineligible.
  // =====================================================================
  var deferredPrompt = null;
  var PWA_KEY = "wavr.pwa.dismissed.v1";
  var pwaRow = $("pwaRow"), pwaBtn = $("pwaInstallBtn"), pwaNo = $("pwaDismissBtn");
  function pwaDismissed(){ try{ return localStorage.getItem(PWA_KEY) === "1"; }catch(e){ return false; } }
  window.addEventListener("beforeinstallprompt", function(e){
    e.preventDefault();            // suppress the mini-infobar; surface our own affordance
    deferredPrompt = e;
    if(pwaRow && !pwaDismissed()) pwaRow.hidden = false;
  });
  window.addEventListener("appinstalled", function(){
    deferredPrompt = null;
    if(pwaRow) pwaRow.hidden = true;
  });
  if(pwaBtn) pwaBtn.onclick = function(){
    var p = deferredPrompt;
    deferredPrompt = null;
    if(pwaRow) pwaRow.hidden = true;
    if(p){ try{ p.prompt(); }catch(e){} }
  };
  if(pwaNo) pwaNo.onclick = function(){
    try{ localStorage.setItem(PWA_KEY, "1"); }catch(e){}
    if(pwaRow) pwaRow.hidden = true;
  };

  // ---------- gear → Sobre: honest mode label (+ version once live status arrives) ----------
  var sobreModeEl = $("sobreMode");
  if(sobreModeEl) sobreModeEl.textContent = " · " + WavrT("mode: {mode}", { mode:
    M === "live" ? WavrT("real Space (local)") : M === "companion" ? WavrT("viewer") : WavrT("demo") });

  // =====================================================================
  // Hook chaining — ALWAYS call the Stage-2 handler first, then ours.
  // =====================================================================
  var _sys = window.__wavrSystem;
  window.__wavrSystem = function(s){
    try{ if(_sys) _sys(s); }catch(e){}
    sysInfo = (s && typeof s === "object") ? s : null;
    renderKill(); renderEgress(); renderTier(); renderHealthStatusLine();
  };
  var _cams = window.__wavrCameras;
  window.__wavrCameras = function(c){
    try{ if(_cams) _cams(c); }catch(e){}
    camsList = Array.isArray(c) ? c : [];
    renderKill(); renderEgress(); renderTier();
  };
  var _inv = window.__wavrInventory;
  window.__wavrInventory = function(devs){
    try{ if(_inv) _inv(devs); }catch(e){}
    invList = Array.isArray(devs) ? devs : [];
    // renderNetwork rebuilds #devList right after this hook fires — re-apply the search
    // filter and re-attach the open profile panel once that synchronous rebuild is done.
    setTimeout(function(){ applyDevSearch(); attachDevDetail(); }, 0);
  };
  var _rs = window.__wavrRS;
  window.__wavrRS = function(rs){
    try{ if(_rs) _rs(rs); }catch(e){}
    try{
      lastEventAt = Date.now(); evtCount++;
      ((rs && rs.sources) || []).forEach(function(s){
        if(s && s.modality) srcSeen[s.modality] = { at: Date.now(), health: s.health || "", age_s: s.age_s };
      });
      HIST.push({ ts: rs.ts, room: rs.room, occupied: rs.occupied, confidence: rs.confidence,
                  sources: rs.sources, explanation: rs.explanation, vitals: rs.vitals });
      if(HIST.length > 1000) HIST.shift();
    }catch(e){}
  };
  window.__wavrStatus = function(s){
    // `unavailable` is the status poll saying it got NO ANSWER. Treated as
    // absence, not as an empty payload: an empty payload renders "0 egress
    // paths enabled", which is a reassurance nobody earned.
    statusInfo = (s && typeof s === "object" && !s.unavailable) ? s : null;
    renderEgress();
    renderSensing();     // A2.1 — same payload, zero new fetches
    renderHealthTile();  // A2.3 — only (re)paints visibility/copy; never fetches
    renderDoctorTile();  // network-doctor — only (re)paints visibility; never fetches
    renderHealthStatusLine();   // System regroup — same passive poll, zero new fetches
    renderWatch();       // Watch/Vigia — reflect + poll the live suppression state
    var sv = $("sobreVersion");
    if(sv && statusInfo && statusInfo.version != null)
      sv.textContent = " · " + WavrT("version {v}", { v: statusInfo.version });
  };
  // WS reconnect state for the diagnostics: wrap the global setReconnecting (a top-level
  // function declaration = window property; the providers resolve it through global scope).
  if(typeof window.setReconnecting === "function"){
    var _sr = window.setReconnecting;
    window.setReconnecting = function(on){
      try{ _sr(on); }catch(e){}
      if(on && !wsDown){ wsDown = true; wsSince = Date.now(); wsDrops++; }
      if(!on && wsDown) wsDown = false;
      // Item 6 (mobile health screen): expose live-socket liveness so the shim's on-device
      // Connection check can report it. Boolean only — no payloads, no identifiers.
      try{ window.__wavrWsDown = wsDown; }catch(e){}
    };
  }

  // ---------- initial paint (every mode) ----------
  renderEgress();
  renderSensing();     // A2.1 — paints the honest "no hub connected" state in demo/companion
  renderHealthTile();  // A2.3 — hidden outside live; never fetches on its own
  renderDoctorTile();  // network-doctor — hidden outside live; never fetches on its own
  renderKill();
  renderTier();        // global Sensing-level meter — illustrative in demo, live in live
  renderWatch();       // Watch/Vigia overlay — illustrative in demo, live in live
  renderDiag();
  applyTlFilter();
  renderPresence();    // A2.2 — live-only poller; no-ops (shows the note) elsewhere
  pollAssistantEgress();   // UX HIGH #1 — live-only poller; no-ops (stays absent) elsewhere
})();
