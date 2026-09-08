// ==========================================================================
// shell-nav.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ==== Stage-1 Command Center shell: tab router + gear overlay + topbar pills ====
// Additive only. All data wiring above is untouched; this block uses the globals the
// main script already defines (MODE, companionToken) and the render-loop hook
// (window.__wavrMap) that renderRadar() exposes at the end of its closure.
(function(){
  "use strict";
  // Two levels, and the split IS the information architecture.
  //
  // PRIMARY is what a household member is offered. MANAGE is the control
  // plane, one level down behind the Manage item — the same panels, the same
  // ids, the same `data-tab` values, simply not competing for a person's
  // first choice. `TABS` stays the union so `switchTab` still resolves every
  // destination, including deep links from the tray, the attention rows and
  // `window.switchTab(...)` calls elsewhere in this file.
  //
  // `quemcasa` and `novos` are gone from this list because their PANELS are
  // gone: their tiles were moved into Space and Devices & sensors. The ids
  // travelled with them, so nothing that renders those tiles changed.
  var PRIMARY = ["inicio", "historico", "rotinas"];
  var MANAGE  = ["sistema", "dispositivos", "rede", "discoveries", "transparencia"];
  var TABS = PRIMARY.concat(MANAGE);
  var content = document.getElementById("content");
  var navButtons = Array.prototype.slice.call(
    document.querySelectorAll(".nav-item[data-tab], .nav-sub-item[data-tab]"));
  var manageBtn = document.getElementById("tab-manage");
  var manageSub = document.getElementById("navManageSub");
  var panels = {};
  TABS.forEach(function(t){ panels[t] = document.querySelector('.tab-panel[data-tab="' + t + '"]'); });
  var activeTab = "inicio";
  var overlayOpen = false;

  // ---- 3D render loop gate (MASTER-SPEC §4 render-on-demand): run only while the map is
  // actually on screen — Home active, or the gear overlay holding the relocated map — and
  // the document itself is visible.
  function mapVisible(){
    if(overlayOpen) return (typeof MODE !== "undefined" && MODE === "live"); // map relocates into the overlay only on live
    return activeTab === "inicio";
  }
  function syncMapLoop(){
    var m = window.__wavrMap; if(!m) return;
    if(document.hidden || !mapVisible()) m.pause(); else m.resume();
  }
  window.__wavrShellSync = syncMapLoop;   // renderRadar() calls this once its hook lands
  document.addEventListener("visibilitychange", syncMapLoop);

  // ---- Tab router: exactly one panel visible; switching resets scroll to top. ----
  function switchTab(name){
    if(!panels[name]) return;
    activeTab = name;
    navButtons.forEach(function(btn){
      var on = btn.getAttribute("data-tab") === name;
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
      btn.tabIndex = on ? 0 : -1;
      // Phone bar now scrolls horizontally (see TABS above — the count in this
      // comment said 9 for as long as there were 10, .nav-tabs overflow-x:auto) — keep the
      // active one in view whether switchTab was reached by tap, keyboard, or a deep-link
      // elsewhere in the file (e.g. window.switchTab("sistema")).
      if(on && btn.scrollIntoView) btn.scrollIntoView({block:"nearest", inline:"nearest"});
    });
    TABS.forEach(function(t){
      if(panels[t]) panels[t].classList.toggle("active", t === name);
    });
    // The second level follows the destination rather than the click: a deep
    // link straight to Network from the tray, or a Needs Attention row jumping
    // to Devices, must leave a person looking at an opened Manage — not at a
    // primary rail with nothing selected and no idea where they are.
    var inManage = MANAGE.indexOf(name) !== -1;
    if(manageSub) manageSub.hidden = !inManage;
    if(manageBtn){
      manageBtn.classList.toggle("active", inManage);
      manageBtn.setAttribute("aria-selected", inManage ? "true" : "false");
      manageBtn.setAttribute("aria-expanded", inManage ? "true" : "false");
      manageBtn.tabIndex = inManage ? 0 : -1;
    }
    if(content) content.scrollTop = 0;
    if(panels[name]) panels[name].scrollTop = 0;
    syncMapLoop();
  }
  window.switchTab = switchTab;

  navButtons.forEach(function(btn){
    btn.addEventListener("click", function(){ switchTab(btn.getAttribute("data-tab")); });
  });

  // Manage opens the level and lands on its first destination. Pressing it
  // again while already inside collapses back to the primary rail, which is
  // what a person expects from a disclosure control and what stops Manage
  // becoming a one-way door.
  if(manageBtn) manageBtn.addEventListener("click", function(){
    if(MANAGE.indexOf(activeTab) !== -1){
      switchTab(PRIMARY[0]);
    } else {
      switchTab(MANAGE[0]);
      var first = document.getElementById("tab-" + MANAGE[0]);
      if(first) first.focus();
    }
  });

  // Keyboard-navigable tablist: arrows move (roving tabindex), Home/End jump.
  // Arrows move within the level you are ON, not through the union. Cycling a
  // keyboard user out of the primary rail and into the middle of the control
  // plane, with no announcement that the level changed, is worse than no
  // keyboard support: it is silent teleportation.
  function ring(){
    return MANAGE.indexOf(activeTab) !== -1 ? MANAGE : PRIMARY;
  }
  function bindArrows(el){
    if(!el) return;
    el.addEventListener("keydown", function(e){
      if(["ArrowRight","ArrowDown","ArrowLeft","ArrowUp","Home","End"].indexOf(e.key) === -1) return;
      e.preventDefault();
      var list = ring();
      var i = list.indexOf(activeTab);
      if(i === -1) i = 0;
      if(e.key === "ArrowRight" || e.key === "ArrowDown") i = (i + 1) % list.length;
      else if(e.key === "ArrowLeft" || e.key === "ArrowUp") i = (i - 1 + list.length) % list.length;
      else if(e.key === "Home") i = 0;
      else i = list.length - 1;
      switchTab(list[i]);
      var btn = document.getElementById("tab-" + list[i]);
      if(btn) btn.focus();
    });
  }
  bindArrows(document.querySelector(".nav-tabs"));
  bindArrows(document.getElementById("navManageSub"));

  // ---- Gear overlay (Settings): takeover, not a 6th tab. Opening never changes the
  // active tab; closing simply re-reveals it exactly as it was (scroll included). ----
  var overlay = document.getElementById("gearOverlay");
  var gearNavBtn = document.getElementById("gearNavBtn");
  var gearTopBtn = document.getElementById("gearTopBtn");
  var gearBack = document.getElementById("gearBack");
  var mapHome = document.getElementById("mapHome");
  var gearMapHost = document.getElementById("gearMapHost");
  var gearMapHint = document.getElementById("gearMapHint");
  var lastGearTrigger = null;

  // ---- P5 fix 5: aria-modal contract for the takeover overlays (gear + setup) ----
  // While an overlay is open the app root goes `inert` (out of the tab order AND the
  // accessibility tree; aria-hidden doubles as the fallback where inert is unsupported),
  // and Tab/Shift+Tab cycle within the overlay's own focusable elements. Shared with the
  // Stage-2 setup overlay via window.__wavr* (that block runs later in document order).
  var appRoot = document.getElementById("app");
  function setAppInert(on){
    if(!appRoot) return;
    try{ appRoot.inert = on; }catch(err){}
    if(on) appRoot.setAttribute("aria-hidden", "true");
    else appRoot.removeAttribute("aria-hidden");
  }
  function trapFocus(el){
    el.addEventListener("keydown", function(e){
      if(e.key !== "Tab" || el.hidden) return;
      var sel = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),' +
                'textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';
      var f = Array.prototype.filter.call(el.querySelectorAll(sel), function(n){
        return !n.hidden && n.offsetParent !== null;   // skips display:none subtrees
      });
      if(!f.length){ e.preventDefault(); return; }
      var first = f[0], last = f[f.length - 1], cur = document.activeElement;
      if(e.shiftKey && (cur === first || !el.contains(cur))){ e.preventDefault(); last.focus(); }
      else if(!e.shiftKey && (cur === last || !el.contains(cur))){ e.preventDefault(); first.focus(); }
    });
  }
  window.__wavrSetAppInert = setAppInert;
  window.__wavrTrapFocus = trapFocus;
  if(overlay) trapFocus(overlay);

  // ---- Settings left rail (findings #6/#7/#8): single-section visibility, panel-MQ only —
  // outside that MQ every .settings-section is display:contents (base CSS), so .active never
  // gates anything on desktop and this whole block is a harmless no-op there. HOME_PANEL_MQ
  // (defined earlier in this file) is the exact same media query as the panel CSS block. ----
  var gearRailItems = Array.prototype.slice.call(document.querySelectorAll(".gear-rail-item"));
  var gearRightPane = document.getElementById("gearRightPane");
  var gearSections = {};
  gearRailItems.forEach(function(btn){
    var id = btn.getAttribute("data-section");
    if(id) gearSections[id] = document.getElementById(id);
  });
  function showGearSection(id){
    if(!gearSections[id]) return;
    gearRailItems.forEach(function(btn){
      var on = btn.getAttribute("data-section") === id;
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    });
    Object.keys(gearSections).forEach(function(k){
      if(gearSections[k]) gearSections[k].classList.toggle("active", k === id);
    });
    if(gearRightPane) gearRightPane.scrollTop = 0;
    // Desktop: every `.settings-section` is `display:contents` (base CSS) and stacks
    // into ONE flow, so `.active` above changes nothing visible there and
    // `#gearRightPane` has no box of its own for `scrollTop` to reset — opening
    // Settings, or asking for a specific section (the runtime chip's "go to Trust",
    // a tray deep link), always surfaced whatever sat physically first in that flow
    // (Layout) and switching section never moved the read position either. Scrolling
    // the target section's own heading — a real box on BOTH layouts — into its
    // nearest scrolling ancestor reaches the right place either way: the panel's
    // `#gearRightPane` or the desktop `.overlay-content`.
    var heading = gearSections[id].querySelector(".settings-section-h") || gearSections[id];
    if(heading.scrollIntoView) heading.scrollIntoView({block: "start"});
    // Trust makes four requests and nobody reads it every session, so it renders
    // when opened rather than on load. `js/trust.js` publishes the hook.
    if(id === "gearSecTrust" && window.__wavrRenderTrust) window.__wavrRenderTrust();
    if(id === "gearSecPrivacy" && window.__wavrRenderPrivacyData) window.__wavrRenderPrivacyData();
    if(id === "gearSecDeveloper" && window.__wavrRenderDeveloper) window.__wavrRenderDeveloper();
  }
  gearRailItems.forEach(function(btn){
    btn.addEventListener("click", function(){ showGearSection(btn.getAttribute("data-section")); });
  });
  // finding #6: "Panel lock" is a light pointer, not a duplicated admin control (OQ2 open) —
  // closing Settings and landing on System's real "Panel & admin" card mirrors gearEgressLink.
  var gearPanelLockLink = document.getElementById("gearPanelLockLink");
  if(gearPanelLockLink) gearPanelLockLink.addEventListener("click", function(){
    closeGear();
    if(typeof window.switchTab === "function") window.switchTab("sistema");
    var cl = document.getElementById("coreLockPanel");
    if(cl) cl.scrollIntoView({ block: "start" });
  });

  // finding #8: the heavy OrbitControls canvas mounts only when this disclosure is opened —
  // at the panel form factor the map otherwise never mounts on Settings open at all (desktop
  // keeps the original eager-mount in openGear() below, untouched).
  var gearMapToggle = document.getElementById("gearMapToggle");
  function mountGearMap(){
    if(typeof MODE === "undefined" || MODE !== "live") return;
    var rw = document.getElementById("radarWrap");
    if(rw && gearMapHost && rw.parentNode !== gearMapHost) gearMapHost.appendChild(rw);
    if(gearMapHint) gearMapHint.hidden = false;
  }
  if(gearMapToggle) gearMapToggle.addEventListener("click", function(){
    var open = gearMapToggle.getAttribute("aria-expanded") !== "true";
    gearMapToggle.setAttribute("aria-expanded", open ? "true" : "false");
    if(open) mountGearMap();
    syncMapLoop();
  });

  function openGear(trigger){
    if(overlayOpen || !overlay) return;
    overlayOpen = true;
    lastGearTrigger = trigger || null;
    overlay.hidden = false;
    setAppInert(true);
    document.body.classList.add("gear-open");
    // Default landing = a light section (Devices), never the floor-plan editor (finding #6).
    // Only meaningful under the panel-MQ rail — a no-op elsewhere (sections are
    // display:contents on desktop, so .active never gates visibility there).
    showGearSection("gearSecDevices");
    // Trust renders on demand, and on DESKTOP nothing ever calls
    // showGearSection: the rail is panel-width-only and every section is
    // display:contents there, so the panel would sit permanently empty —
    // a screen that works on a phone and not on a laptop, silently.
    // `__wavrRenderTrust` rebuilds its own host, so this and the rail click
    // are both safe.
    if(window.__wavrRenderTrust) window.__wavrRenderTrust();
    if(window.__wavrRenderPrivacyData) window.__wavrRenderPrivacyData();
    if(window.__wavrRenderDeveloper) window.__wavrRenderDeveloper();
    // Live/central only: bring the map tile (the house editor's drawing surface) into the
    // overlay so drawing/zone editing works here; it returns to Home on close. The node
    // itself moves, so every id still exists exactly once (same move-a-panel technique
    // initCompanion() already uses). Finding #8: at the panel form factor this eager mount
    // is skipped — the map mounts only when the Layout section's "Edit floor plan" disclosure
    // is opened; desktop keeps this exact eager-mount so non-panel Settings stays
    // behavior-identical.
    if(!HOME_PANEL_MQ.matches){
      mountGearMap();
    }
    syncMapLoop();
    if(gearBack) gearBack.focus();
  }
  function closeGear(){
    if(!overlayOpen || !overlay) return;
    overlayOpen = false;
    overlay.hidden = true;
    setAppInert(false);   // P5 fix 5: un-inert BEFORE restoring focus to the in-app trigger
    document.body.classList.remove("gear-open");
    var rw = document.getElementById("radarWrap");
    if(rw && mapHome && rw.parentNode !== mapHome) mapHome.appendChild(rw);
    if(gearMapToggle) gearMapToggle.setAttribute("aria-expanded", "false");   // collapse for next open
    syncMapLoop();
    if(lastGearTrigger && lastGearTrigger.focus) lastGearTrigger.focus();
  }
  // Cross-block hook (same idiom as window.switchTab/__wavrSetAppInert above): lets a script
  // block that runs later in document order (e.g. the Discoveries tab, whose "route to the
  // relevant screen" actions need to open a specific Settings section) do so without
  // duplicating openGear()/showGearSection()'s internals in a third place.
  window.__wavrOpenGearSection = function(sectionId){
    openGear();
    showGearSection(sectionId);
  };
  if(gearNavBtn) gearNavBtn.addEventListener("click", function(){ openGear(gearNavBtn); });
  if(gearTopBtn) gearTopBtn.addEventListener("click", function(){ openGear(gearTopBtn); });
  if(gearBack) gearBack.addEventListener("click", closeGear);
  document.addEventListener("keydown", function(e){
    if(e.key === "Escape" && overlayOpen) closeGear();
  });
  // P5 fix 1: the two privacy surfaces point at each other — this closes the gear and lands
  // on System's live egress dashboard ("What can leave this home").
  var gearEgressLink = document.getElementById("gearEgressLink");
  if(gearEgressLink) gearEgressLink.addEventListener("click", function(){
    closeGear();
    if(typeof window.switchTab === "function") window.switchTab("sistema");
    var ep = document.getElementById("egressPanel");
    if(ep) ep.scrollIntoView({ block: "start" });
  });
  // Privacy → Devices identity/consent registry: unlike gearEgressLink above, "My devices"
  // already lives INSIDE this same Settings overlay (gearSecDevices) — no closeGear()/tab
  // switch needed, just the same showGearSection() the left rail itself uses.
  var gearIdentityLink = document.getElementById("gearIdentityLink");
  if(gearIdentityLink) gearIdentityLink.addEventListener("click", function(){
    showGearSection("gearSecDevices");
    var md = document.getElementById("myDevices");
    if(md) md.scrollIntoView({ block: "start" });
  });

  // ---- Privacy chip ("Nothing leaves home.") — §9: always present, expandable every session,
  // never removed (collapses to icon-only on narrow phones via CSS). ----
  var chip = document.getElementById("privacyChip");
  var pop = document.getElementById("privacyPop");
  if(chip && pop){
    chip.addEventListener("click", function(e){
      e.stopPropagation();
      var opening = pop.hidden;
      pop.hidden = !opening;
      chip.setAttribute("aria-expanded", opening ? "true" : "false");
    });
    document.addEventListener("click", function(e){
      if(!pop.hidden && !pop.contains(e.target)) { pop.hidden = true; chip.setAttribute("aria-expanded", "false"); }
    });
    document.addEventListener("keydown", function(e){
      if(e.key === "Escape" && !pop.hidden){ pop.hidden = true; chip.setAttribute("aria-expanded", "false"); }
    });
  }

  // Live truth for the chip's own label — the SAME producer transparency.js
  // already polls every 20s for Manage -> Privacy & security (GET
  // /api/transparency, backed by app.py's single `_egress_now()`), so the
  // chip and that screen can never tell a household two different answers
  // to "does anything leave right now". Absent in demo/simulated on purpose:
  // transparency.js makes no backend call there at all, so the parsed
  // "Nothing leaves this Space" the chip was born with is the literal truth
  // of a page that never talks to a network — nothing to reconcile.
  //
  // Left untouched on a failed/timed-out poll (transparency.js's own
  // `cannotTell()` path): this chip shows the last known state rather than a
  // third "unknown" state, which the compact topbar pill has no room to say
  // honestly. The full "cannot be checked" wording lives on the Privacy &
  // security screen itself.
  if(chip){
    var chipIconUse = chip.querySelector("svg use");
    var chipLabelEl = chip.querySelector(".chip-label");
    var chipShortEl = chip.querySelector(".chip-label-short");
    window.__wavrOnEgressUpdate = function(onRows){
      var n = (onRows || []).length;
      if(n === 0){
        chip.classList.remove("warn");
        if(chipIconUse) chipIconUse.setAttribute("href", "#ic-shield");
        if(chipLabelEl) chipLabelEl.textContent = WavrT("Nothing leaves this Space.");
        if(chipShortEl) chipShortEl.textContent = WavrT("private");
        chip.setAttribute("aria-label", WavrT("Nothing leaves this Space — privacy guarantee"));
        return;
      }
      chip.classList.add("warn");
      if(chipIconUse) chipIconUse.setAttribute("href", "#ic-egress");
      if(chipLabelEl) chipLabelEl.textContent = WavrT(
        "{n} thing is leaving your network.|{n} things are leaving your network.", { n: n });
      if(chipShortEl) chipShortEl.textContent = WavrT("{n} leaving", { n: n });
      chip.setAttribute("aria-label", WavrT(
        "{n} thing is currently leaving your network — tap for details.|"
        + "{n} things are currently leaving your network — tap for details.", { n: n }));
    };
  }

  // ---- MODE-aware shell chrome (placeholder notes only; the real panels keep their own
  // MODE gating inside the render*() functions above). ----
  try{
    if(typeof MODE !== "undefined"){
      if(MODE !== "live"){
        var dn = document.getElementById("dispNote"); if(dn) dn.hidden = false;
        var sn = document.getElementById("sisNote"); if(sn) sn.hidden = false;
      }
      if(MODE === "simulated"){
        var rn = document.getElementById("redeNote"); if(rn) rn.hidden = false;
        // P4 fix 6: with no hub there's no live data — show the clearly-flagged
        // "example" mockup so the tab's value is visible instead of a blank note.
        var rx = document.getElementById("redeExemplo"); if(rx) rx.hidden = false;
      }
      if(MODE === "companion" && typeof companionToken === "function" && !companionToken()){
        document.body.classList.add("pairing-mode");   // pairing screen: hide nav chrome
      }
    }
  }catch(err){ /* shell chrome must never break the app */ }

  // ---- Topbar status pills (live only): mirror what renderStatus() already writes into the
  // Sistema panel — zero new network calls, MutationObserver re-reads the existing DOM. ----
  function setPill(pill, txt, state){
    if(!pill) return;
    pill.hidden = false;
    var t = pill.querySelector(".p-txt"); if(t) t.textContent = txt;
    pill.classList.remove("ok", "down", "off");
    if(state) pill.classList.add(state);
  }
  // P5 fix 11: ONE definition of "active sources" over #statusSources — the topbar pill
  // mirror (below) and Sistema's summary line (Stage-2 block) both count via this, so
  // "what counts as active" can never drift between the two observers.
  window.__wavrCountActiveSources = function(){
    var ss = document.getElementById("statusSources");
    if(!ss) return null;
    var rows = ss.querySelectorAll(".status-src-row");
    if(!rows.length) return null;
    return { act: ss.querySelectorAll(".h.active").length, total: rows.length };
  };
  if(typeof MODE !== "undefined" && MODE === "live"){
    // Fix F1: #pillInternet/#pillSensores (dead topbar pills — Sistema tab already shows this
    // exact data, and the phone-nowrap row could hide them entirely, see F2) are gone; this
    // mirror now ONLY feeds the Rede tab's #redeInternet tile, its one remaining consumer.
    var statusInternetEl = document.getElementById("statusInternet");
    var redeInternetTile = document.getElementById("redeInternet");
    var redeInternetLine = document.getElementById("redeInternetLine");
    if(statusInternetEl){
      var mirrorInternet = function(){
        var txt = statusInternetEl.textContent || "";
        if(!txt) return;
        if(redeInternetTile) redeInternetTile.hidden = false;
        if(redeInternetLine){
          redeInternetLine.className = statusInternetEl.className;
          redeInternetLine.textContent = txt;
        }
      };
      new MutationObserver(mirrorInternet).observe(statusInternetEl,
        {childList: true, characterData: true, subtree: true, attributes: true, attributeFilter: ["class"]});
      mirrorInternet();
    }
  }

  // ---- P4 fix 6: header network badge — mirrors what the Network tab already shows (zero new
  // network calls: it observes #devList, which renderNetwork's own poll rebuilds). Tapping
  // it opens the tab. In demo mode it honestly reads "example" (grey), never fakes activity.
  var pillRede = document.getElementById("pillRede");
  if(pillRede){
    pillRede.addEventListener("click", function(){ switchTab("rede"); });
    if(typeof MODE !== "undefined" && MODE === "simulated"){
      setPill(pillRede, WavrT("Network: example"), "off");
    } else {
      var devListMon = document.getElementById("devList");
      if(devListMon){
        var mirrorRede = function(){
          // Rendered rows are capped for scale (see renderNetwork), so prefer the TRUE
          // total the render stamps on #devList; fall back to the row count if absent.
          var total = parseInt(devListMon.dataset.total || "", 10);
          var n = isNaN(total) ? devListMon.querySelectorAll(".dev-row").length : total;
          if(!n){ pillRede.hidden = true; return; }   // no inventory yet — no badge, no guess
          setPill(pillRede, WavrT("Network: {n} device|Network: {n} devices", {n: n}), "ok");
        };
        new MutationObserver(mirrorRede).observe(devListMon, { childList: true });
        mirrorRede();
      }
    }
  }

  switchTab("inicio");   // normalize roving tabindex + panel/scroll state on load
})();
