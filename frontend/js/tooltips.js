// ==========================================================================
// tooltips.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ==== v2 tooltips / context-help system (redesign2-MASTER §3, redesign2-uirefine §1) ====
// One shared #tipPop popover + four coexisting reveal paths: mouse hover (350ms delay),
// keyboard focus (:focus-visible), touch long-press (450ms), and a persistent Help Mode
// toggle (#helpModeBtn) that turns any tap on a [data-tip] control into a tooltip reveal
// instead of the action. Screen readers never depend on the popover: a static .sr-tip
// span + aria-describedby is stamped next to every non-empty [data-tip] control (and
// re-stamped after dynamic rebuilds via a debounced MutationObserver). All tip text is
// rendered with textContent — never HTML. EMPTY data-tip values (the egress rows carry
// data-tip="" as a reserved wiring point) are ALWAYS ignored — no blank popovers.
(function(){
  "use strict";
  var pop = document.getElementById("tipPop");
  var helpBtn = document.getElementById("helpModeBtn");
  var helpBanner = document.getElementById("helpBanner");
  if(!pop) return;

  // ---------- per-source tips for the Devices→Active toggles (renderControls reads
  // this through window.__wavrSourceTip on every 3s rebuild; the fallback string
  // guarantees an unmapped source name is never tooltip-less) ----------
  var SOURCE_TIP = {
    network:  "Watches which known devices are on your Wi-Fi — coarse whole-home presence, no camera",
    ruview:   "Wi-Fi CSI sensing — detects motion from changes in the Wi-Fi signal",
    wifi_csi: "Wi-Fi CSI sensing — detects motion from changes in the Wi-Fi signal",
    sim:      "Simulated source — generates fake presence data for testing",
    mmwave:   "mmWave radar — presence and micro-motion, works in the dark",
    ble:      "Bluetooth — detects known BLE devices nearby",
    camera:   "Visual presence detection — frames stay in memory, never recorded"
  };
  // Translated at READ time, not at declaration: the table is built while this classic
  // script parses, and a locale switch has to repaint tips that were already stamped.
  window.__wavrSourceTip = function(name){
    if(SOURCE_TIP[name]) return WavrT(SOURCE_TIP[name]);
    // The source's own label is a modality name from elsewhere — kept whole in a slot so
    // Portuguese can put it wherever the clause needs it.
    var lbl = (typeof modalityLabel === "function") ? modalityLabel(name) : String(name || "");
    return WavrT("{label} sensor — toggles this presence source", { label: lbl });
  };

  // ---------- core state ----------
  var tipEl = null;          // trigger currently owning the popover
  var pinned = false;        // long-press / Help-Mode tips don't hide on mouseleave
  var helpOn = false;
  var hoverTimer = 0, hideTimer = 0, autoHideTimer = 0, pendingEl = null;
  var suppressNextClick = false, suppressTimer = 0;

  function tipText(el){
    if(!el || !el.getAttribute) return "";
    var t = el.getAttribute("data-tip");
    return t == null ? "" : String(t).trim();   // CRITICAL: "" and whitespace-only = no tip
  }
  function findTip(node){
    if(!node || !node.closest) return null;
    var el = node.closest("[data-tip]");
    return (el && tipText(el)) ? el : null;
  }

  // ---------- show / hide (position above by default, flip below when clipped,
  // clamp horizontally so it can never overflow the viewport) ----------
  function showTip(el, mode, overrideText){
    // overrideText: show a caller-supplied string (the nav double-tap pops the short NAME)
    // instead of the element's verbose data-tip sentence.
    var text = overrideText || tipText(el);
    if(!text) return;
    clearTimeout(hoverTimer); clearTimeout(hideTimer); clearTimeout(autoHideTimer);
    pendingEl = null;
    pop.textContent = text;
    pop.hidden = false;
    pop.classList.remove("show", "above", "below");
    // measure at 0,0 — a fixed element's shrink-to-fit width is capped by the viewport
    // space RIGHT of its left offset, so measuring at a stale position under-reports
    pop.style.left = "0px"; pop.style.top = "0px";
    var pr = pop.getBoundingClientRect();
    var pw = pr.width, ph = pr.height;
    var r = el.getBoundingClientRect();
    var vw = window.innerWidth, vh = window.innerHeight;
    var below = (r.top - ph - 12) < 8;               // not enough room above → flip
    var y = below ? (r.bottom + 10) : (r.top - ph - 10);
    if(below && y + ph > vh - 8) y = Math.max(8, vh - ph - 8);   // pathological: clamp
    var x = r.left + r.width / 2 - pw / 2;
    x = Math.min(Math.max(x, 8), Math.max(8, vw - pw - 8));
    var ax = Math.min(Math.max(r.left + r.width / 2 - x, 10), pw - 10);  // arrow → trigger center
    pop.style.left = x + "px";
    pop.style.top = y + "px";
    pop.style.setProperty("--ax", Math.round(ax) + "px");
    pop.classList.add(below ? "below" : "above");
    void pop.offsetWidth;                            // reflow so the fade transition runs
    pop.classList.add("show");                       // reduced-motion: CSS kills the fade
    tipEl = el;
    pinned = (mode === "press" || mode === "help");
    if(mode === "press") autoHideTimer = setTimeout(hideTip, 7000);  // touch has no hover-out
    else if(mode === "name") autoHideTimer = setTimeout(hideTip, 1600);  // brief name pop-out
  }
  // Fix 4: double-tap (dblclick / second tap) a nav item pops just its short NAME — a
  // discoverable touch reveal that complements the always-visible rail labels, distinct from
  // the long-press that shows the full data-tip sentence. Idempotent/harmless on desktop.
  document.addEventListener("dblclick", function(e){
    var nav = (e.target && e.target.closest) ? e.target.closest(".nav-item") : null;
    if(!nav) return;
    var lbl = nav.querySelector(".nav-label");
    var name = (lbl && lbl.textContent.trim()) || nav.getAttribute("aria-label") || "";
    if(name) showTip(nav, "name", name);
  });
  function hideTip(){
    // NOTE: deliberately does NOT touch the pending-hover state — the 100ms leave-grace
    // hide of an OLD tip must never cancel the 350ms timer of the control the pointer
    // is on NOW (verified live: this exact race ate the sysToggle tip after a tab click)
    clearTimeout(hideTimer); clearTimeout(autoHideTimer);
    tipEl = null; pinned = false;
    if(pop.hidden) return;
    pop.classList.remove("show");
    pop.hidden = true;
    pop.textContent = "";
  }
  function cancelPending(){ clearTimeout(hoverTimer); pendingEl = null; }

  // ---------- mouse: 350ms show delay, 100ms leave grace. Touch fires compatibility
  // mouseover right after a tap (emulators AND real devices) — a plain tap must not
  // grow a tooltip 350ms later, so hover is suppressed briefly after any touch. ----------
  var lastTouchAt = 0;
  document.addEventListener("mouseover", function(e){
    if(Date.now() - lastTouchAt < 900) return;   // touch-derived compat hover
    var el = findTip(e.target);
    if(!el) return;
    if(el === tipEl){ clearTimeout(hideTimer); return; }
    if(pinned) return;                       // never let hover replace a pinned tip
    if(el === pendingEl) return;
    clearTimeout(hoverTimer);
    pendingEl = el;
    hoverTimer = setTimeout(function(){
      if(pendingEl !== el) return;           // superseded/cancelled — stale timers never show
      showTip(el, "hover");
    }, 350);
  });
  document.addEventListener("mouseout", function(e){
    var el = findTip(e.target);
    if(!el) return;
    if(e.relatedTarget && el.contains(e.relatedTarget)) return;   // still inside the trigger
    if(pendingEl === el) cancelPending();
    if(tipEl === el && !pinned){
      clearTimeout(hideTimer);
      hideTimer = setTimeout(hideTip, 100);
    }
  });

  // ---------- keyboard: focus shows immediately (visible focus only); blur hides.
  // data-tip-nofocus opts a control out (the Catalog search autofocuses on desktop —
  // entering the tab must not pop a tooltip; hover/long-press still work there). ----------
  document.addEventListener("focusin", function(e){
    var el = findTip(e.target);
    if(!el || el.hasAttribute("data-tip-nofocus")) return;
    try{ if(el.matches && !el.matches(":focus-visible")) return; }catch(err){}
    showTip(el, "focus");
  });
  document.addEventListener("focusout", function(e){
    var el = findTip(e.target);
    if(el && tipEl === el && !pinned) hideTip();
  });

  // ---------- touch: 450ms long-press (cancelled by >10px move or early release);
  // the synthetic click that follows is swallowed once so the action never fires ----------
  var lpTimer = 0, lpEl = null, lpX = 0, lpY = 0;
  function armSuppress(){
    suppressNextClick = true;
    clearTimeout(suppressTimer);
    // some browsers never fire the post-long-press click — don't let the flag
    // swallow an unrelated later tap
    suppressTimer = setTimeout(function(){ suppressNextClick = false; }, 800);
  }
  document.addEventListener("touchstart", function(e){
    lastTouchAt = Date.now();                // gates the compat mouseover that follows a tap
    cancelPending();
    if(helpOn) return;                       // Help Mode owns the tap path
    var el = (e.touches.length === 1) ? findTip(e.target) : null;
    if(tipEl && el !== tipEl) hideTip();     // tap elsewhere dismisses a pinned tip
    clearTimeout(lpTimer); lpEl = null;
    if(!el) return;
    lpEl = el; lpX = e.touches[0].clientX; lpY = e.touches[0].clientY;
    lpTimer = setTimeout(function(){ armSuppress(); showTip(lpEl, "press"); lpEl = null; }, 450);
  }, { passive: true });
  document.addEventListener("touchmove", function(e){
    if(!lpEl || !e.touches.length) return;
    var t = e.touches[0];
    if(Math.abs(t.clientX - lpX) > 10 || Math.abs(t.clientY - lpY) > 10){
      clearTimeout(lpTimer); lpEl = null;
    }
  }, { passive: true });
  document.addEventListener("touchend", function(){ clearTimeout(lpTimer); lpEl = null; }, { passive: true });
  document.addEventListener("touchcancel", function(){ clearTimeout(lpTimer); lpEl = null; }, { passive: true });
  document.addEventListener("contextmenu", function(e){
    if(suppressNextClick) e.preventDefault();    // long-press must not open a context menu
  });

  // ---------- Help Mode: tap a control → see its tip instead of acting; tap the SAME
  // control again → the action goes through (and the tip hides). ? / Esc exits. ----------
  function setHelpMode(on){
    helpOn = !!on;
    document.body.classList.toggle("help-mode", helpOn);
    if(helpBtn) helpBtn.setAttribute("aria-pressed", helpOn ? "true" : "false");
    if(helpBanner) helpBanner.hidden = !helpOn;
    if(!helpOn) hideTip();
  }
  if(helpBtn) helpBtn.addEventListener("click", function(){ setHelpMode(!helpOn); });

  // ---------- capture-phase click: swallows the post-long-press synthetic click,
  // implements Help-Mode interception, and dismisses any open tip on real activation ----------
  document.addEventListener("click", function(e){
    if(suppressNextClick){
      suppressNextClick = false;
      clearTimeout(suppressTimer);
      e.preventDefault(); e.stopPropagation();
      return;                                  // tip stays up (7s auto-hide / tap elsewhere)
    }
    if(helpOn){
      var el = findTip(e.target);
      if(el && el.id !== "helpModeBtn"){       // the ? toggle itself always acts
        if(tipEl === el && !pop.hidden){ hideTip(); return; }   // 2nd tap = do it
        e.preventDefault(); e.stopPropagation();
        showTip(el, "help");
        return;
      }
      if(tipEl) hideTip();                     // tap elsewhere dismisses, action proceeds
      return;
    }
    cancelPending();                           // activation cancels a brewing hover tip too
    if(tipEl) hideTip();                       // real activation → stale tip goes away
  }, true);

  // ---------- Audit M4: native <select> pickers open on pointer/mousedown — BEFORE the
  // capture-phase click above can intercept — so Help Mode showed the tip AND the dropdown.
  // Special-case select triggers: intercept the gesture's FIRST event, pin the tip, and arm
  // the one-shot click swallow so this same gesture's click keeps the tip up. A 2nd press
  // falls through untouched (= the "tap the same control again to do it" contract). The
  // suppressNextClick branch also closes the armed-long-press hole (only the click used to
  // be swallowed; the picker could still open from the compat mousedown). Canceling
  // pointerdown suppresses the compat mousedown, so this never runs twice per gesture. ----------
  function selectTipDown(e){
    var el = findTip(e.target);
    if(!el || el.tagName !== "SELECT") return;
    if(suppressNextClick){ e.preventDefault(); return; }   // post-long-press: picker must not open
    if(!helpOn) return;
    if(tipEl === el && !pop.hidden) return;                // 2nd press → native picker opens
    e.preventDefault();
    armSuppress();                                         // swallow this gesture's own click — tip stays up
    showTip(el, "help");
  }
  document.addEventListener("pointerdown", selectTipDown, true);
  document.addEventListener("mousedown", selectTipDown, true);

  // ---------- Esc: first press dismisses the tip (and stops there — the gear overlay's
  // own Esc-to-close only sees the NEXT press); with no tip open it exits Help Mode ----------
  window.addEventListener("keydown", function(e){
    if(e.key !== "Escape") return;
    if(!pop.hidden){ cancelPending(); hideTip(); e.stopPropagation(); return; }
    if(helpOn) setHelpMode(false);
  }, true);

  // ---------- scroll / resize / focus loss: a fixed-position popover must never drift ----------
  document.addEventListener("scroll", function(){ cancelPending(); if(tipEl) hideTip(); }, true);
  window.addEventListener("resize", function(){ cancelPending(); if(tipEl) hideTip(); });
  window.addEventListener("blur", function(){ cancelPending(); if(tipEl) hideTip(); });

  // ---------- static screen-reader descriptions: stamp a .sr-tip span + aria-describedby
  // NEXT TO every non-empty [data-tip] control (sibling, not child — several controls
  // overwrite their own textContent on refresh). Skips aria-hidden decorations (the
  // egress badges — their row text already says the same thing) and empty wiring points.
  // Re-runs debounced after DOM rebuilds (source toggles, catalog chips, drill-downs). ----------
  var tipSeq = 0;
  function stampSrTips(){
    var nodes = document.querySelectorAll("[data-tip]");
    for(var i = 0; i < nodes.length; i++){
      var el = nodes[i];
      var text = tipText(el);
      if(!text) continue;
      if(el.getAttribute("aria-hidden") === "true") continue;
      if(el.__wavrTipText === text && el.__wavrTipSpan && document.contains(el.__wavrTipSpan)) continue;
      if(!el.parentNode) continue;
      if(el.__wavrTipSpan && el.__wavrTipSpan.parentNode) el.__wavrTipSpan.remove();
      var sp = document.createElement("span");
      sp.className = "sr-tip";
      var id = el.id ? "tip-" + el.id : "tip-g" + (++tipSeq);
      while(document.getElementById(id)) id = "tip-g" + (++tipSeq);
      sp.id = id;
      sp.textContent = text;
      el.insertAdjacentElement("afterend", sp);
      var db = (el.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
      db = db.filter(function(x){ return x !== el.__wavrTipDescId; });
      db.push(id);
      el.setAttribute("aria-describedby", db.join(" "));
      el.__wavrTipText = text; el.__wavrTipSpan = sp; el.__wavrTipDescId = id;
    }
  }
  var stampScheduled = false;
  function scheduleStamp(){
    if(stampScheduled) return;
    stampScheduled = true;
    setTimeout(function(){
      stampScheduled = false;
      if(tipEl && !document.contains(tipEl)) hideTip();   // trigger rebuilt away mid-tip
      stampSrTips();
    }, 250);
  }
  new MutationObserver(scheduleStamp).observe(document.body, { childList: true, subtree: true });
  stampSrTips();
})();
