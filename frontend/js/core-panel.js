// ==========================================================================
// core-panel.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ============================================================================
// Wavr Core Panel (Phase 1 of "Wavr OS"): ?core / window.WAVR_CORE ambient
// home-status face. Additive and fully gated: the very first statement below
// returns immediately unless core mode is requested, so the three existing
// modes (live/companion/simulated) run byte-for-byte unchanged. This block
// touches nothing outside #corePanel and a handful of already-established
// extension hooks when core mode is off.
//
// Reuses EXISTING data plumbing only, per the design spec's data-sources list:
//   - RoomState: chains onto window.__wavrRS, the same hook the Stage-2/3
//     command-center blocks above already chain onto (no second WebSocket).
//     Reads the dashboard's own top-level `roomOcc` accumulator directly
//     (updateHouse() populates it earlier in the same handle() call, before
//     __wavrRS fires), the same documented "readable from later script
//     blocks" pattern already used for MODE elsewhere in this file.
//   - "Internet" (renamed from "Rede"/"Net", dashboard-topbar) health: chains onto
//     window.__wavrStatus, fed by the SAME /api/status poll renderStatus()/
//     renderControls() already run every 3s in live mode. Deliberately NOT
//     /api/health: that endpoint is a USER-TRIGGERED, confirm-gated egress check
//     (see the "LOAD-BEARING" comment near healthBtn elsewhere in this file).
//     Auto-polling it here would silently re-trigger a disclosed-only public-DNS
//     ping on every idle cycle, which would violate that invariant. /api/status's
//     existing internet-monitor field is the honest, already-flowing substitute.
//   - "Hub" (renamed from "Core", backend/stream) health: chains onto
//     window.setReconnecting, which handle() already calls on every WS open/close.
//   - Alert count: observes the EXISTING #alertList DOM (rebuilt every 15s by
//     renderNetwork()'s own poll) via MutationObserver, the same zero-new-
//     fetch technique the header's #pillRede badge already uses above.
//
// Assumption flagged for review: "N HOME" counts ROOMS with confirmed
// presence, not a literal person headcount. RoomState never carries a people
// count (fusion is per-room occupied/confidence, never a headcount), so
// counting occupied rooms is the honest reading available from this feed.
// (This cited docs/superpowers/specs/2026-07-06-wavr-core-panel-design.md,
// which is not in the tree and, by the shape of the path, never was. The
// reasoning it stood in for is the paragraph above.)
// ============================================================================
(function(){
  "use strict";
  var CORE_MODE = false;
  try{ CORE_MODE = new URLSearchParams(location.search).has("core") || !!window.WAVR_CORE; }catch(e){}
  if(!CORE_MODE) return;   // gate: every existing mode exits here, nothing below ever runs

  var panel = document.getElementById("corePanel");
  if(!panel) return;
  document.body.classList.add("core-mode");
  panel.hidden = false;   // shown by default (ambient face); dissolves on the first interaction

  // ---------------- What's New takeover (first run after an update) ----------------
  // Layered OVER the lock (z-index 20 > core-lock's 10). Shown once per version; OK
  // persists the acknowledgement and reveals the still-locked panel underneath. Never
  // bypasses the lock — it is read-only release notes, not an unlock.
  (function whatsNewTakeover(){
    var box = document.getElementById("coreWhatsNew");
    if(!box) return;
    var SEEN_KEY = "wavr_whatsnew_seen";
    var cur = window.WAVR_APP_VERSION;
    var seen = null;
    try{ seen = localStorage.getItem(SEEN_KEY); }catch(e){}
    var entry = window.__wavrWhatsNewFor(cur);
    if(!entry || seen === cur) return;   // nothing new, or already acknowledged this version
    var verEl = document.getElementById("cwnVersion");
    var ul    = document.getElementById("cwnList");
    // Through the formatter: the entry's `date` is an ISO day, and rendering it
    // raw shows a Brazilian reader 2026-09-06 in the middle of a Portuguese
    // sentence. `WavrFmt.date` answers in the reader's own conventions.
    if(verEl) verEl.textContent = WavrT("Version {version} · {date}",
      {version: entry.version, date: WavrFmt.date(entry.date) || entry.date});
    window.__wavrRenderWhatsNewItems(ul, entry);
    box.hidden = false;
    var ok = document.getElementById("cwnOk");
    function dismiss(){
      box.hidden = true;
      try{ localStorage.setItem(SEEN_KEY, cur); }catch(e){}
      // Do NOT reveal the dashboard — hand control back to the normal wake/lock gate.
      try{ if(ok) ok.removeEventListener("click", dismiss); }catch(e){}
    }
    if(ok) ok.addEventListener("click", dismiss);
  })();

  // ---------------- Clock (top-left): local time, ticks once a second ----------------
  var timeEl = document.getElementById("coreClockTime");
  var dateEl = document.getElementById("coreClockDate");
  function tickClock(){
    var d = new Date();
    if(timeEl) timeEl.textContent = WavrFmt.time(d);
    if(dateEl) dateEl.textContent = WavrFmt.date(d, {weekday:"short", day:"2-digit", month:"2-digit"});
  }
  tickClock();
  setInterval(tickClock, 1000);

  // ---------------- Ambient wave field (the hero) + active rooms: fed by RoomState
  // (__wavrRS chain). The center hero is now a canvas wave, not text — the "signature
  // animated green wave/radar" the owner asked for in place of the big "HOME EMPTY" word
  // (the note this used to cite, docs/superpowers/specs/2026-07-06-wavr-core-panel-design.md,
  // is not in the tree; its "ambient face" description of the hero predated this change in
  // any case). #coreHeroSr carries the exact
  // same information the old text hero rendered, off-screen, so occupancy is still announced
  // to screen readers even though sighted users now read the wave instead of a word.
  var heroSrEl   = document.getElementById("coreHeroSr");
  var roomsEl    = document.getElementById("coreRooms");
  var waveCanvas = document.getElementById("coreWave");
  var haveFrame  = false;   // no RoomState frame yet this session: calm/idle wave, neutral sr text
  var prevRoomOcc = {};     // per-room edge detection: a fresh true flips a transient "pulse"

  // ---- Wave engine -----------------------------------------------------------------
  // A handful of layered sine ribbons, additive-blended (globalCompositeOperation
  // "lighter") for a soft neon glow — no shadowBlur (recomputes per pixel every frame,
  // too costly 24/7 on a weak always-on phone) and no three.js/WebGL (the dashboard's
  // 3D map already owns the one GPU context this page needs; a fixed handful of sin()
  // samples on Canvas 2D is orders of magnitude cheaper). A single scalar "energy"
  // (0 = empty/calm .. ~1 = many confirmed rooms at high confidence/alive) drives
  // amplitude, drift speed and brightness together, smoothly interpolated toward its
  // target every drawn frame — never a hard cut between calm and alive. Capped at
  // ~30fps and fully paused whenever the panel isn't the active view (dissolved by a
  // touch, or the document itself hidden) via wave.stop()/start() below.
  var wave = (function(){
    var ctx = waveCanvas ? waveCanvas.getContext("2d") : null;
    var FRAME_MS = 1000 / 30;
    var W = 0, H = 0, dpr = 1;
    var energy = 0, targetEnergy = 0, pulseE = 0;
    var t0 = null, lastDraw = null, raf = null, running = false;
    // ---- Colour-by-state (green is ALWAYS the default). Each mood = [darkRGB, lightRGB];
    // the wave interpolates its live colour toward the target mood every frame (never a hard
    // cut). Priority is decided by updateWaveMood() below: critical > alert > paused > watch >
    // calm. A separate "speak" level (voice) surges motion + brightness ON TOP of any colour,
    // so Wavr visibly comes to life when its voice talks, whatever the current state. ----
    var MOODS = {
      calm:     [[61,181,74],  [130,235,150]],  // green  (--accent) — default/normal
      alert:    [[232,161,58], [245,205,140]],  // amber  (--warn)   — unresolved alerts
      critical: [[232,114,106],[245,170,165]],  // red    (--danger) — critical / security
      watch:    [[58,150,232], [150,205,245]],  // blue              — watch/guard mode active
      paused:   [[120,132,140],[175,185,195]],  // grey              — sensing paused / off
      // Grey too, and a separate NAME on purpose. "Nothing is wrong" and "I
      // could not check" are different answers, and a wall panel that shows
      // the calm green for the second is the single failure this product's
      // observability rule is about. The alert list is read from the DOM, so
      // when the Network tab could not read the Core the list is empty — and
      // empty used to mean calm.
      unknown:  [[120,132,140],[175,185,195]]
    };
    var colDark  = MOODS.calm[0].slice();   // live colour, smoothly chased toward the target
    var colLight = MOODS.calm[1].slice();
    var tgtDark  = MOODS.calm[0], tgtLight = MOODS.calm[1];
    var speakE = 0, speakTarget = 0;        // voice "life": 0 = silent .. 1 = talking
    // amplitude/alpha/speed weights per layer — index 0 calmest/back, 2 liveliest/front
    var LAYERS = [
      { freq: 1.5,  speed: 0.05,   ampBase: 0.045, ampGain: 0.22, width: 1.3, alphaBase: 0.16, alphaGain: 0.24, phase: 0.0 },
      { freq: 1.0,  speed: -0.03,  ampBase: 0.065, ampGain: 0.34, width: 1.7, alphaBase: 0.11, alphaGain: 0.30, phase: 2.1 },
      { freq: 0.65, speed: 0.018,  ampBase: 0.085, ampGain: 0.48, width: 2.2, alphaBase: 0.08, alphaGain: 0.34, phase: 4.4 }
    ];

    function resize(){
      if(!waveCanvas || !waveCanvas.parentElement) return;
      var rect = waveCanvas.parentElement.getBoundingClientRect();
      dpr = Math.min(window.devicePixelRatio || 1, 1.5);   // cap backing-store cost (G9 panel is ~2.75x)
      W = Math.max(1, Math.round(rect.width));
      H = Math.max(1, Math.round(rect.height));
      waveCanvas.width  = Math.round(W * dpr);
      waveCanvas.height = Math.round(H * dpr);
      waveCanvas.style.width  = W + "px";
      waveCanvas.style.height = H + "px";
    }
    resize();
    window.addEventListener("resize", resize);

    // Pure function of (t, e): the animated loop feeds it real elapsed seconds, the
    // reduced-motion path always feeds t=0 so the shape only ever changes with energy,
    // never with the clock (§ reduced-motion below).
    function renderFrame(t, e){
      if(!ctx || !W || !H) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);
      ctx.globalCompositeOperation = "lighter";
      var midY = H / 2, steps = 48;   // cheap: ~48 sin() samples per layer per frame
      // Live colour (chased toward the mood target in tick) + a voice-brightening toward white
      // so the wave lights up while Wavr talks. Computed ONCE per frame (constant across layers).
      var wl = 0.55 * speakE;
      var dR = (colDark[0]  + (255 - colDark[0])  * wl) | 0,
          dG = (colDark[1]  + (255 - colDark[1])  * wl) | 0,
          dB = (colDark[2]  + (255 - colDark[2])  * wl) | 0,
          lR = (colLight[0] + (255 - colLight[0]) * wl) | 0,
          lG = (colLight[1] + (255 - colLight[1]) * wl) | 0,
          lB = (colLight[2] + (255 - colLight[2]) * wl) | 0;
      LAYERS.forEach(function(layer){
        var amp   = (layer.ampBase + layer.ampGain * e) * H * 0.5;
        var alpha = Math.min(1, layer.alphaBase + layer.alphaGain * e);
        var phase = layer.phase + t * layer.speed * (1 + e * 1.5) * Math.PI * 2;
        ctx.beginPath();
        for(var i = 0; i <= steps; i++){
          var x = (i / steps) * W;
          var y = midY + Math.sin((i / steps) * layer.freq * Math.PI * 2 + phase) * amp;
          if(i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        // Cheap glow: a wide faint stroke under a thin bright one, both additive —
        // instead of shadowBlur, which is too costly per-frame at three layers, 24/7.
        ctx.lineWidth = layer.width * 3.2; ctx.strokeStyle = "rgba(" + dR + "," + dG + "," + dB + "," + (alpha * 0.35) + ")"; ctx.stroke();
        ctx.lineWidth = layer.width;       ctx.strokeStyle = "rgba(" + lR + "," + lG + "," + lB + "," + alpha + ")";         ctx.stroke();
      });
      ctx.setTransform(1, 0, 0, 1, 0, 0);
    }

    function drawStatic(){
      // reduced-motion: one deterministic frame, no clock — only "energy" ever moves it.
      renderFrame(0, Math.min(1.15, targetEnergy));
    }

    function tick(now){
      if(!running) return;
      raf = requestAnimationFrame(tick);
      if(lastDraw !== null && now - lastDraw < FRAME_MS) return;   // ~30fps cap regardless of panel refresh rate
      lastDraw = now;
      if(t0 === null) t0 = now;
      energy += (targetEnergy - energy) * 0.05;   // smooth interpolation, no hard jumps
      pulseE *= 0.94;                              // transient "movement" pulse decays away
      speakE += (speakTarget - speakE) * 0.20;     // voice level chases its target (responsive)
      speakTarget *= 0.90;                         // eases back to silent unless setSpeaking() refreshes it
      for(var ci = 0; ci < 3; ci++){               // chase the mood colour — never a hard cut
        colDark[ci]  += (tgtDark[ci]  - colDark[ci])  * 0.05;
        colLight[ci] += (tgtLight[ci] - colLight[ci]) * 0.05;
      }
      // Voice adds motion (energy) too, so the wave visibly surges to life when Wavr talks.
      renderFrame((now - t0) / 1000, Math.min(1.25, energy + pulseE + speakE * 0.6));
    }

    function start(){
      if(REDUCED_MOTION.matches){ drawStatic(); return; }   // never loop under reduced motion
      if(running) return;
      running = true; lastDraw = null;
      resize();   // re-measure in case layout wasn't settled when this module was built
      raf = requestAnimationFrame(tick);
    }
    function stop(){
      running = false;
      if(raf) cancelAnimationFrame(raf);
      raf = null;
    }
    function setEnergy(next){
      targetEnergy = Math.max(0, Math.min(1, next));
      if(REDUCED_MOTION.matches) drawStatic();   // reflect the new state immediately, still no motion
    }
    function pulse(){
      if(REDUCED_MOTION.matches) return;   // a transient boost IS motion — skip it entirely
      pulseE = Math.min(0.5, pulseE + 0.32);
    }
    function setMood(name){
      var m = MOODS[name] || MOODS.calm;
      tgtDark = m[0]; tgtLight = m[1];
      if(REDUCED_MOTION.matches){ colDark = tgtDark.slice(); colLight = tgtLight.slice(); drawStatic(); }
    }
    function setSpeaking(level){
      // 0..1 — the voice module calls this (repeatedly, with live amplitude) while Wavr talks;
      // it surges wave motion + brightness. Between calls it eases back to silent (tick decay).
      speakTarget = Math.max(0, Math.min(1, level || 0));
    }
    return { start: start, stop: stop, setEnergy: setEnergy, pulse: pulse, setMood: setMood, setSpeaking: setSpeaking };
  })();

  function renderHero(){
    // 'casa' is the house-level identities-only pseudo-room the backend uses for BLE/network
    // presence (never a real room; see the backend's own "house-level only, never a real
    // room" invariant) — it still counts toward home/away (matches updateHouse()'s own
    // definition exactly) but is never shown as a room chip or counted into N.
    var all  = (typeof roomOcc  !== "undefined" && roomOcc)  ? roomOcc  : {};
    var conf = (typeof roomConf !== "undefined" && roomConf) ? roomConf : {};
    var names = Object.keys(all);
    var homeOccupied = names.some(function(r){ return all[r]; });
    var realOccupied = names.filter(function(r){ return isRealRoom(r) && all[r]; });

    if(heroSrEl){
      heroSrEl.textContent = !haveFrame ? WavrT("Loading…")
        : !homeOccupied ? WavrT("Nobody here")
        : realOccupied.length > 0 ? WavrT("{n} room active|{n} rooms active", {n: realOccupied.length})
        : WavrT("Someone's here");
    }

    // Wave energy target: 0 (empty house, calm) .. ~1 (several confirmed rooms at high
    // fusion confidence, alive) — roomsFactor rewards more active rooms, maxConf rewards
    // fusion certainty; both read from roomOcc/roomConf for the exact reason the old text
    // hero read them. 'casa'-only presence (house-level BLE/network, no specific room) get
    // a mid-confidence default since there is no per-room confidence to read in that case.
    var target = 0;
    if(homeOccupied){
      var roomsFactor = Math.min(realOccupied.length, 4) / 4;
      var maxConf = realOccupied.length
        ? Math.max.apply(null, realOccupied.map(function(r){ return conf[r] || 0; }))
        : (conf.casa || 0.5);
      target = 0.30 + 0.35 * roomsFactor + 0.30 * maxConf;
    }
    wave.setEnergy(target);

    if(roomsEl){
      roomsEl.textContent = "";
      realOccupied.forEach(function(name){
        var chip = document.createElement("span");
        chip.className = "core-room-chip";
        var dot = document.createElement("span");
        dot.className = "core-dot"; dot.setAttribute("aria-hidden", "true"); dot.textContent = "●";
        chip.appendChild(dot);
        chip.appendChild(document.createTextNode(" " + name));   // room name is RoomState data (untrusted) — textContent only
        roomsEl.appendChild(chip);
      });
    }
  }
  renderHero();
  if(!document.hidden) wave.start();   // don't spin up the loop if the kiosk happened to load backgrounded

  var _rsCore = window.__wavrRS;
  window.__wavrRS = function(rs){
    try{ if(_rsCore) _rsCore(rs); }catch(e){}
    try{
      // The FIRST frame is the moment "I have not been told anything" stops
      // being true, and the wave is painted from that fact — so recolour on
      // the edge. Without this the panel keeps the starting-up grey until the
      // next alert refresh happens to call updateWaveMood(), which is its only
      // other caller and runs on its own schedule.
      var firstFrame = !haveFrame;
      haveFrame = true;
      // "Movement" pulse: THIS frame's room just flipped from unoccupied to occupied — a
      // fresh detection event, not steady state — gives the wave a brief lift on top of the
      // smoothed baseline (design: "if there's movement the line gets more alive").
      if(rs && rs.room && rs.occupied && !prevRoomOcc[rs.room]) wave.pulse();
      if(rs && rs.room) prevRoomOcc[rs.room] = !!rs.occupied;
      renderHero();
      if(firstFrame) updateWaveMood();
    }catch(e){}
  };

  // ---------------- Hub / Internet health (top-right) ----------------
  // dashboard-topbar: renamed from the unlabeled "Core"/"Net" — Hub = this screen's own
  // link to the Wavr hub (the live WS); Internet = the separate cloud-reachability monitor.
  // Kept as two clearly distinct words on purpose: conflating them used to read as "no
  // internet = no local presence", which is false (see the explainer popover below).
  var coreHealthCoreEl = document.getElementById("coreHealthCore");
  var coreHealthNetEl  = document.getElementById("coreHealthNet");
  // Hub: true = the live WS is down/reconnecting, false = a frame has arrived,
  // null = NOTHING IS KNOWN YET. Start unknown, not good.
  //
  // This was `false`, and `renderCoreHealth()` runs at parse time — so the very
  // first thing a kiosk painted was a green "Hub ✓", asserted before the
  // WebSocket had been constructed, let alone connected, let alone delivered a
  // frame. On a Core that never comes up, that is the only thing it ever
  // paints. The Internet pill beside it has always started at "…" for exactly
  // this reason; the two now agree on what "not yet known" looks like. The flag
  // leaves null on the first real observation, from either direction:
  // setReconnecting(false) on a delivered frame (render.js's handle()),
  // setReconnecting(true) on a close or on the staleness watchdog in
  // core-connection.js.
  var wsDownCore = null;
  var internetOkCore = null;    // Internet: null = unknown (monitor off, or no /api/status yet)

  function renderCoreHealth(){
    if(coreHealthCoreEl){
      coreHealthCoreEl.textContent = WavrT(wsDownCore === true ? "Hub ⚠"
                                         : wsDownCore === false ? "Hub ✓" : "Hub …");
      coreHealthCoreEl.className = "core-health-item" + (wsDownCore === true ? " core-health-warn" : "");
    }
    if(coreHealthNetEl){
      var label = internetOkCore === true ? WavrT("Internet ✓") : internetOkCore === false ? WavrT("Internet ⚠") : WavrT("Internet …");
      coreHealthNetEl.textContent = label;
      coreHealthNetEl.className = "core-health-item" + (internetOkCore === false ? " core-health-warn" : "");
    }
  }
  renderCoreHealth();

  if(typeof window.setReconnecting === "function"){
    var _srCore = window.setReconnecting;
    window.setReconnecting = function(on){
      try{ _srCore(on); }catch(e){}
      wsDownCore = !!on;
      renderCoreHealth();
    };
  }

  // ---------------- Hub/Internet 2-line explainer popover (tap either pill) ----------------
  var infoPopEl      = document.getElementById("coreInfoPop");
  var infoPopTitleEl = document.getElementById("coreInfoPopTitle");
  var infoPopBodyEl  = document.getElementById("coreInfoPopBody");
  var infoPopCloseEl = document.getElementById("coreInfoPopClose");
  // One literal per entry, resolved at paint time (openInfoPop) rather than at parse
  // time, so a language switch repaints the popover with the rest of the screen.
  var CORE_INFO = {
    hub: { title: function(){ return WavrT("Hub"); },
           body: function(){ return WavrT("This screen's link to your Wavr hub. A warning here just means the ambient face is reconnecting — sensing keeps running on the hub either way."); } },
    net: { title: function(){ return WavrT("Internet"); },
           body: function(){ return WavrT("Only affects cloud features, like sending a summary to an AI. It never affects local presence — that always runs on this hub, online or not."); } }
  };
  function closeInfoPop(){
    if(infoPopEl) infoPopEl.hidden = true;
    [coreHealthCoreEl, coreHealthNetEl].forEach(function(b){ if(b) b.setAttribute("aria-expanded", "false"); });
  }
  function openInfoPop(key, anchorBtn){
    if(!infoPopEl || !infoPopTitleEl || !infoPopBodyEl) return;
    var info = CORE_INFO[key];
    if(!info) return;
    infoPopTitleEl.textContent = info.title();
    infoPopBodyEl.textContent = info.body();
    infoPopEl.hidden = false;
    [coreHealthCoreEl, coreHealthNetEl].forEach(function(b){ if(b) b.setAttribute("aria-expanded", String(b === anchorBtn)); });
  }
  if(coreHealthCoreEl) coreHealthCoreEl.addEventListener("click", function(ev){
    ev.stopPropagation();
    if(infoPopEl && !infoPopEl.hidden && coreHealthCoreEl.getAttribute("aria-expanded") === "true") closeInfoPop();
    else openInfoPop("hub", coreHealthCoreEl);
  });
  if(coreHealthNetEl) coreHealthNetEl.addEventListener("click", function(ev){
    ev.stopPropagation();
    if(infoPopEl && !infoPopEl.hidden && coreHealthNetEl.getAttribute("aria-expanded") === "true") closeInfoPop();
    else openInfoPop("net", coreHealthNetEl);
  });
  if(infoPopCloseEl) infoPopCloseEl.addEventListener("click", function(ev){ ev.stopPropagation(); closeInfoPop(); });

  // ---------------- Bluetooth/BLE quick-control pill (READ-ONLY; no backend toggle exists
  // for it here) — sourced from the SAME /api/status payload as the Internet pill above,
  // via its .sources array ({name,active}, per GET /api/status in app.py). Honestly hidden
  // (not a fake "off") when the backend never registered a "ble" source at all. ----------
  var bleEl    = document.getElementById("coreBlePill");
  var bleTxtEl = document.getElementById("coreBleTxt");
  function renderBle(active){
    if(!bleEl) return;
    bleEl.hidden = false;
    bleEl.classList.toggle("on", !!active);
    if(bleTxtEl) bleTxtEl.textContent = WavrT(active ? "Bluetooth on" : "Bluetooth off");
  }

  var _statusCore = window.__wavrStatus;
  window.__wavrStatus = function(s){
    try{ if(_statusCore) _statusCore(s); }catch(e){}
    try{
      var inet = (s && s.internet && typeof s.internet === "object") ? s.internet : {};
      internetOkCore = inet.ok === true ? true : inet.ok === false ? false : null;
      var srcs = (s && Array.isArray(s.sources)) ? s.sources : [];
      var ble = null;
      for(var i = 0; i < srcs.length; i++){ if(srcs[i] && srcs[i].name === "ble"){ ble = srcs[i]; break; } }
      if(ble) renderBle(!!ble.active); else if(bleEl) bleEl.hidden = true;
      renderCoreHealth();
    }catch(e){}
  };

  // ---------------- Feature 1: wifi / bluetooth / battery (top-right, native-bridge only) ----------------
  // window.WavrNative only exists inside the launcher build; a plain browser/dev tab never has
  // it, so #coreSys stays [hidden] (its default HTML state) and this whole block is a no-op —
  // no errors, no placeholder polling, per the contract. Polled every ~5s, separate from every
  // other feed on this page (it isn't RoomState/`/api/status`), guarded end-to-end: a malformed
  // bridge payload just leaves the last good render instead of throwing.
  var sysEl         = document.getElementById("coreSys");
  var sysWifiEl     = document.getElementById("coreSysWifi");
  var sysBtEl       = document.getElementById("coreSysBt");
  var sysBattEl     = document.getElementById("coreSysBatt");
  var sysBattFillEl = document.getElementById("coreSysBattFill");
  var sysBattBoltEl = document.getElementById("coreSysBattBolt");
  var SYS_POLL_MS   = 5000;

  function renderCoreSys(status){
    if(!sysEl) return;
    if(!status || typeof status !== "object"){ sysEl.hidden = true; return; }
    sysEl.hidden = false;
    var labelParts = [];

    var wifi = (status.wifi && typeof status.wifi === "object") ? status.wifi : {};
    var level = Math.max(0, Math.min(4, parseInt(wifi.level, 10) || 0));
    var lvl = wifi.connected ? level : 0;
    if(sysWifiEl){
      sysWifiEl.hidden = false;
      sysWifiEl.setAttribute("data-level", String(lvl));
    }
    // Through the catalogue, in English, like every other string on this panel.
    // These three were written straight into the DOM in Portuguese, so an
    // English reader's screen reader announced the panel's system strip in a
    // language they had not chosen — and the catalogue already carried
    // "Bluetooth on", unused, because the code never asked for it. A hard-coded
    // translation is not localisation: it is one locale that cannot be changed
    // and one that got lucky.
    labelParts.push(wifi.connected ? WavrT("Wi-Fi: signal {n} of 4", {n: lvl})
                                   : WavrT("Wi-Fi: disconnected"));

    var bt = (status.bluetooth && typeof status.bluetooth === "object") ? status.bluetooth : {};
    if(sysBtEl) sysBtEl.hidden = !bt.on;
    if(bt.on) labelParts.push(WavrT("Bluetooth on"));

    var batt = (status.battery && typeof status.battery === "object") ? status.battery : null;
    if(sysBattEl){
      if(batt && typeof batt.level === "number"){
        sysBattEl.hidden = false;
        var pct = Math.max(0, Math.min(100, batt.level));
        if(sysBattFillEl) sysBattFillEl.style.width = pct + "%";
        sysBattEl.classList.toggle("core-batt-low", pct <= 15 && !batt.charging);
        if(sysBattBoltEl) sysBattBoltEl.hidden = !batt.charging;
        labelParts.push(batt.charging ? WavrT("Battery {pct}% (charging)", {pct: Math.round(pct)})
                              : WavrT("Battery {pct}%", {pct: Math.round(pct)}));
      } else {
        sysBattEl.hidden = true;
      }
    }
    sysEl.setAttribute("aria-label", labelParts.join(" · "));
  }

  function pollCoreSys(){
    if(!window.WavrNative || typeof window.WavrNative.getSystemStatus !== "function") return;
    try{ renderCoreSys(JSON.parse(window.WavrNative.getSystemStatus())); }
    catch(e){ /* malformed bridge payload: keep the last good render, never throw into the ambient loop */ }
  }
  if(window.WavrNative && typeof window.WavrNative.getSystemStatus === "function"){
    pollCoreSys();
    setInterval(pollCoreSys, SYS_POLL_MS);
  }

  // ---------------- Alert count (bottom-right): mirrors #alertList, zero new fetches ----------------
  var alertsEl = document.getElementById("coreAlerts");
  // Wave colour-by-state. Green is the default; the wave shifts to amber (unresolved alerts),
  // red (a critical/security alert), grey (sensing paused) or blue (watch/guard mode). Priority:
  // critical > alert > paused > watch > calm. Read from the SAME #alertList the count mirrors +
  // two global flags the (future) System/watch features set — no new fetches.
  function updateWaveMood(){
    if(typeof wave === "undefined" || !wave.setMood) return;
    var list = document.getElementById("alertList");
    var rows = list ? list.querySelectorAll(".alert-row") : [];
    var hasCritical = false;
    for(var i = 0; i < rows.length; i++){
      if(rows[i].classList.contains("sev-critical") || rows[i].classList.contains("sev-alert")){ hasCritical = true; break; }
    }
    // "Could not check" comes FIRST, before any reading of the rows: an empty
    // list because nothing is wrong and an empty list because nobody answered
    // look identical from here, and only one of them is calm.
    //
    // `haveFrame` joins it for the same reason, one step earlier. Before the
    // first RoomState frame of the session Wavr has not been told anything
    // about this Space yet, and "nobody is home" is not a thing it knows --
    // yet the alert list is legitimately empty at that moment, so every test
    // below fell through to calm. A panel on a wall painted the settled green
    // for "everything is fine" while it was still starting up, which is the
    // one confusion the comment above exists to prevent, on the only channel
    // somebody three metres away can read. The honest answer already existed —
    // `heroSrEl` says "Loading…" — and used to be .sr-only, which meant it was
    // told to screen readers and to nobody else. It is painted now, in the hero
    // stack, so the wave and the words finally agree about what is known.
    var unknown = !haveFrame || (list && list.dataset && list.dataset.unknown === "1");
    var mood = unknown ? "unknown"
      : hasCritical ? "critical"
      : rows.length ? "alert"
      : window.__wavrSensingPaused ? "paused"
      : window.__wavrWatchMode ? "watch"
      : "calm";
    wave.setMood(mood);
  }
  function renderCoreAlerts(){
    if(!alertsEl) return;
    var list = document.getElementById("alertList");
    var rows = list ? list.querySelectorAll(".alert-row") : [];
    var n = rows.length;
    // A count of zero over a list nobody could read is the same lie as a green
    // wave. Say which one it is.
    var unknown = list && list.dataset && list.dataset.unknown === "1";
    alertsEl.hidden = !n && !unknown;
    alertsEl.textContent = unknown ? WavrT("alerts: cannot check")
      : n ? WavrT("{n} alert|{n} alerts", {n: n}) : "";
    updateWaveMood();
    // Saliency fix (04 screen): count badge + severity-keyed ring on the Network tab's
    // Intrusion Alerts column, mirroring #alertList — zero new fetches, same pattern as the
    // coreAlerts count above. Hypothesis to re-check with saliency_run.py, not a proven fix.
    var badge = document.getElementById("netAlertCount");
    if(badge){
      var hasCritical = false, hasWatch = false;
      for(var i = 0; i < rows.length; i++){
        if(rows[i].classList.contains("sev-critical") || rows[i].classList.contains("sev-alert")) hasCritical = true;
        else if(rows[i].classList.contains("sev-watch")) hasWatch = true;
      }
      badge.textContent = n ? ("(" + n + ")") : "";
      badge.className = "net-alert-count" + (hasCritical ? " has-alert" : hasWatch ? " has-watch" : "");
      if(list){
        list.classList.toggle("has-alert", hasCritical);
        list.classList.toggle("has-watch", !hasCritical && hasWatch);
      }
    }
  }
  var alertListEl = document.getElementById("alertList");
  if(alertListEl) new MutationObserver(renderCoreAlerts).observe(alertListEl, {childList: true});
  renderCoreAlerts();
  // Let other modules retrigger the mood after flipping a flag (System pause, watch mode).
  window.__wavrRefreshWaveMood = updateWaveMood;
  // Voice "life": the AI-voice module calls this (0..1, live amplitude) so the wave surges to
  // life while Wavr talks — see [[project-wavr-voice-agent]]. Safe no-op until voice ships.
  window.__wavrWaveSpeak = function(level){ if(wave && wave.setSpeaking) wave.setSpeaking(level); };

  // dashboard-topbar: the quick-controls cluster (kill-switch/Watch/Bluetooth pills) —
  // referenced by wake() below so tapping any of them acts immediately instead of being
  // swallowed into the lock/reveal gate.
  var quickEl = document.getElementById("coreQuick");

  // ---------------- Alert glance-box: tap the count -> explain each alert -> deep-link to it ----
  var alertsPopEl   = document.getElementById("coreAlertsPop");
  var alertsPopList = document.getElementById("coreAlertsPopList");
  var alertsPopTtl  = document.getElementById("coreAlertsPopTitle");
  var alertsPopX    = document.getElementById("coreAlertsPopClose");
  var pendingAlertNav = null;   // an alert key to deep-link to right after the dashboard reveals
  // ALERT_EXPLAIN + buildAlertItem() are defined once, mode-agnostically, near renderNetwork()
  // above (they also drive the Network tab's own #netAlertsPop) -- reused here rather than
  // duplicated, so the two panels can never drift out of sync on what an alert kind means.
  function closeAlertsPop(){ if(alertsPopEl) alertsPopEl.hidden = true; }
  function openAlertsPop(){
    if(!alertsPopEl || !alertsPopList) return;
    var rows = document.querySelectorAll("#alertList .alert-row");
    alertsPopList.textContent = "";
    if(!rows.length){ closeAlertsPop(); return; }
    if(alertsPopTtl) alertsPopTtl.textContent = WavrT("{n} alert|{n} alerts", {n: rows.length});
    Array.prototype.forEach.call(rows, function(row){
      alertsPopList.appendChild(buildAlertItem(row, {
        onOpen: function(r){ navAlert(r.dataset.alertKey || ""); },
        onDismissed: function(){
          if(!alertsPopList.children.length){ closeAlertsPop(); return; }
          if(alertsPopTtl){
            var remaining = alertsPopList.children.length;
            alertsPopTtl.textContent = WavrT("{n} alert|{n} alerts", {n: remaining});
          }
        }
      }));
    });
    alertsPopEl.hidden = false;
  }
  function navAlert(key){
    pendingAlertNav = key || null;
    closeAlertsPop();
    wake();   // no event -> runs the unlock/reveal path; revealDashboard() consumes pendingAlertNav
  }
  function goToAlert(key){
    var tabBtn = document.getElementById("tab-rede");
    if(tabBtn) tabBtn.click();   // switch to the Network tab (its click handler is already wired)
    setTimeout(function(){
      var esc = (key && window.CSS && CSS.escape) ? CSS.escape(key) : key;
      var target = key ? document.querySelector('#alertList .alert-row[data-alert-key="' + esc + '"]') : null;
      target = target || document.querySelector("#alertList .alert-row");
      if(target){
        try{ target.scrollIntoView({behavior: "smooth", block: "center"}); }catch(e){ target.scrollIntoView(); }
        target.classList.remove("alert-jump"); void target.offsetWidth; target.classList.add("alert-jump");
      }
    }, 140);
  }
  if(alertsPopX) alertsPopX.addEventListener("click", closeAlertsPop);
  if(alertsEl){
    alertsEl.setAttribute("data-tappable", "1");
    alertsEl.setAttribute("role", "button");
    alertsEl.setAttribute("tabindex", "0");
    alertsEl.addEventListener("click", function(ev){
      ev.stopPropagation();
      if(alertsPopEl && !alertsPopEl.hidden) closeAlertsPop(); else openAlertsPop();
    });
  }

  // ---------------- Feature 2: lock (glance-free ambient face, control-gated wake) ----------------
  // The ambient face above (clock/wave/rooms/alerts/coreSys) is NEVER gated — only the reveal
  // of the dashboard underneath is. "Configured" means either a device biometric
  // (window.WavrNative.hasBiometric()) or a backend PIN (GET /api/core/pin/status ->
  // {pin_set:true}). With neither configured, wake() behaves exactly as it always did: touch ->
  // dashboard, no gate. Re-armed every time returnToAmbient() runs, so the kiosk re-locks after
  // every ~60s idle cycle, same moment the dashboard itself hides.
  var lockEl      = document.getElementById("coreLock");
  var biomEl      = document.getElementById("coreLockBiom");
  var biomTxtEl   = document.getElementById("coreLockBiomTxt");
  var biomRetryEl = document.getElementById("coreLockBiomRetry");
  var padEl       = document.getElementById("coreLockPad");
  var dotsEl      = document.getElementById("coreLockDots");
  var errEl       = document.getElementById("coreLockErr");
  var okKeyEl     = document.getElementById("coreLockOk");
  var backKeyEl   = document.getElementById("coreLockBack");
  var PIN_MIN = 4, PIN_MAX = 12, PIN_CACHE_MS = 4000;   // matches the backend's PIN length contract (4-12 digits)
  /* A deadline on the wake gate's only question.
   *
   * This is loopback, so a normal answer takes milliseconds — but a DARK host
   * (power cut, suspended machine, a Core wedged mid-request) does not refuse
   * the connection, it says nothing, and `fetch` then waits for minutes with no
   * rejection and no resolution. `fetchPinConfigured()` never settled, so
   * `gatingInProgress` was never cleared, so `wake()`'s own dedupe guard turned
   * into a permanent lock-out: every subsequent tap, touch and keypress on the
   * panel hit `if(gatingInProgress) return;` and did NOTHING AT ALL, silently,
   * for as long as the page stayed open. A kiosk that has stopped responding to
   * touch is indistinguishable from broken hardware.
   *
   * The refusal path right below already fails safe on purpose (a 401 reads as
   * "locked", never as "no lock"), and a hang must not end up LESS safe than a
   * refusal. With a deadline it does not: an aborted request lands in the same
   * `.catch` a refused connection lands in, and gets the same documented
   * verdict — closed if this Core has ever confirmed a PIN, open if it never
   * has. Same answer as a closed port, which is the case this already handled.
   */
  var PIN_STATUS_TIMEOUT_MS = 8000;

  var authenticated    = false;  // flips true only on a verified unlock; reset by returnToAmbient()
  var gatingInProgress = false;  // dedupes the pointerdown+touchstart+keydown triplet of one physical tap
  var pinBuf           = "";
  var pinBusy          = false;  // a verify POST is in flight — keys/OK ignored meanwhile
  var biomPending      = false;  // a requestAuth() call is outstanding — a late/duplicate result no-ops otherwise
  var pinStatusCache   = null;   // {value, at}: short cache so a rapid double-tap wake fires one loopback GET, not two
  var pinEverConfirmed = false;  // has this Core ever answered "a PIN is set"? decides the unreachable case — see fetchPinConfigured

  function hasBiometricLock(){
    try{
      return !!(window.WavrNative && typeof window.WavrNative.hasBiometric === "function"
                && window.WavrNative.hasBiometric());
    }catch(e){ return false; }
  }
  function fetchPinConfigured(){
    var now = Date.now();
    if(pinStatusCache && (now - pinStatusCache.at) < PIN_CACHE_MS) return Promise.resolve(pinStatusCache.value);
    return WavrAPI.fetch("/api/core/pin/status", {timeoutMs: PIN_STATUS_TIMEOUT_MS})
      .then(function(r){
        // A5.1 audit fix (P1): this route isn't in the backend's token-exempt list, so
        // turning on WAVR_LOCAL_TOKEN (which this page never sends — only the CSRF
        // header above) makes every call here 401. r.ok===false used to fall through to
        // {pin_set:false} exactly like an unreachable backend does below — reading our
        // OWN hardening flag's explicit DENY as "no PIN configured" revealed the kiosk
        // unlocked the moment the token was turned on. A 401 means "ask again once
        // authorized", not "no lock" — fail SAFE (assume locked) so the panel stays
        // behind the PIN screen instead of falling open.
        if(r.status === 401) return {pin_set:true};
        return r.ok ? r.json() : {pin_set:false};
      })
      .then(function(j){
        var v = !!(j && j.pin_set);
        pinStatusCache = {value:v, at:Date.now()};
        // The last answer we actually got, kept beyond the short cache window.
        // See the catch below for why it exists.
        if(v) pinEverConfirmed = true;
        return v;
      })
      // Backend unreachable — a network error, or the deadline above firing on
      // a host that answered nothing at all; not the explicit 401 handled
      // above. The old behaviour was a flat fail OPEN, with the reasoning that
      // a transient hiccup should not lock an owner out of their own kiosk.
      // That is right for a Core that has never reported a lock, and wrong for
      // one that has: this is loopback, so if the fetch fails the Core is down,
      // and the dashboard behind the lock is a SPA that keeps rendering the
      // household's last state to whoever walks up. Failing open there hands a
      // passer-by real data; failing closed costs nothing, because a panel
      // whose Core is down has nothing live to show anyway.
      //
      // So: closed if this Core has ever confirmed a PIN in this session, open
      // if it never has. A fresh or unlocked kiosk still cannot lock anybody
      // out over a blip, and a locked one stays locked.
      .catch(function(){ return pinEverConfirmed; });
  }
  fetchPinConfigured();   // eager warm-up: primes pinStatusCache so the very first real wake
                          // touch (seconds away, realistically) resolves synchronously below
                          // instead of waiting on a loopback round-trip.
  function lockPadOpen(){ return lockEl ? !lockEl.hidden : false; }

  function renderDots(){
    if(!dotsEl) return;
    dotsEl.textContent = "";
    var n = Math.max(pinBuf.length, PIN_MIN);
    for(var i = 0; i < n; i++){
      var d = document.createElement("i");
      d.className = "core-lock-dot" + (i < pinBuf.length ? " core-lock-dot-filled" : "");
      dotsEl.appendChild(d);
    }
    if(okKeyEl) okKeyEl.disabled = pinBuf.length < PIN_MIN || pinBusy;
  }
  function resetPinPad(){
    pinBuf = ""; pinBusy = false;
    if(errEl) errEl.textContent = "";
    if(padEl) padEl.classList.remove("core-lock-shake");
    renderDots();
  }
  function openLockPad(){
    if(!lockEl) return;
    if(biomEl) biomEl.hidden = true;
    if(padEl) padEl.hidden = false;
    resetPinPad();
    lockEl.hidden = false;
    lockEl.focus();
    armIdle();
  }
  function closeLockPad(){
    if(!lockEl) return;
    lockEl.hidden = true;
    biomPending = false;
    if(biomEl) biomEl.hidden = true;
    if(padEl) padEl.hidden = true;
    resetPinPad();
  }
  function pinKeydown(k){
    if(pinBusy || pinBuf.length >= PIN_MAX) return;
    pinBuf += k;
    if(errEl) errEl.textContent = "";
    renderDots();
  }
  function pinBackspace(){
    if(pinBusy) return;
    pinBuf = pinBuf.slice(0, -1);
    renderDots();
  }
  function pinShakeError(msg){
    if(errEl) errEl.textContent = msg || WavrT("Incorrect PIN");
    if(padEl && !REDUCED_MOTION.matches){
      padEl.classList.remove("core-lock-shake");
      void padEl.offsetWidth;   // restart the shake keyframes on repeated wrong entries
      padEl.classList.add("core-lock-shake");
    }
    pinBuf = "";
    renderDots();
  }
  function submitPin(){
    if(pinBusy || pinBuf.length < PIN_MIN) return;
    pinBusy = true;
    renderDots();
    var pin = pinBuf;
    WavrAPI.fetch("/api/core/pin/verify", {method: "POST", json: {pin: pin}}).then(function(r){
      if(!r.ok) throw new Error("http " + r.status);
      return r.json();
    }).then(function(j){
      pinBusy = false;
      if(j && j.ok === true){
        authenticated = true;
        closeLockPad();
        revealDashboard();
      } else {
        // A wrong PIN and a rate-limited attempt both degrade to {ok:false} by backend
        // design (wavr.pin_ratelimit: "a caller learns nothing beyond 'not unlocked'
        // either way") — same message either way, on purpose, never distinguished client-side.
        pinShakeError(WavrT("Incorrect PIN"));
      }
    }).catch(function(){
      pinBusy = false;
      pinShakeError(WavrT("Couldn't verify — try again"));
    });
  }
  if(padEl){
    padEl.querySelectorAll(".core-lock-key[data-k]").forEach(function(btn){
      btn.addEventListener("click", function(){ pinKeydown(btn.getAttribute("data-k")); });
    });
  }
  if(backKeyEl) backKeyEl.addEventListener("click", pinBackspace);
  if(okKeyEl) okKeyEl.addEventListener("click", submitPin);
  if(lockEl){
    // Kiosk hardware-keyboard fallback alongside the on-screen pad — harmless no-op on a
    // touch-only panel, since nothing ever dispatches keydown there in the first place.
    lockEl.addEventListener("keydown", function(e){
      if(!padEl || padEl.hidden) return;
      if(e.key >= "0" && e.key <= "9"){ pinKeydown(e.key); e.stopPropagation(); }
      else if(e.key === "Backspace"){ pinBackspace(); e.stopPropagation(); }
      else if(e.key === "Enter"){ submitPin(); e.stopPropagation(); }
    });
  }

  function startBiometricAuth(){
    if(!lockEl) return;
    if(padEl) padEl.hidden = true;
    if(biomEl){
      biomEl.hidden = false;
      if(biomTxtEl) biomTxtEl.textContent = WavrT("Authenticating…");
      if(biomRetryEl) biomRetryEl.hidden = true;
    }
    lockEl.hidden = false;
    lockEl.focus();
    armIdle();
    biomPending = true;
    window.wavrOnAuthResult = function(ok, method){
      if(!biomPending) return;   // stale/duplicate result after the flow already moved on (e.g. idle-timeout already closed the prompt)
      biomPending = false;
      if(ok){
        authenticated = true;
        closeLockPad();
        revealDashboard();
      } else {
        // Cancel/error: fall through to the PIN pad — but only if one is actually set on the
        // backend; a PIN pad with nothing to verify against would be a dead end, so that case
        // instead offers a retry of the biometric prompt (flagged assumption, see report).
        fetchPinConfigured().then(function(pinSet){
          if(pinSet){ openLockPad(); }
          else if(biomEl){
            if(biomTxtEl) biomTxtEl.textContent = WavrT("Authentication cancelled");
            if(biomRetryEl) biomRetryEl.hidden = false;
          }
        });
      }
    };
    try{ window.WavrNative.requestAuth(WavrT("Unlock Wavr Core")); }
    catch(e){
      biomPending = false;
      fetchPinConfigured().then(function(pinSet){ if(pinSet) openLockPad(); });
    }
  }
  if(biomRetryEl) biomRetryEl.addEventListener("click", startBiometricAuth);

  // ---------------- Wake / idle: touch, tap, click or keydown anywhere reveals the
  // dashboard (through the lock gate above, when one is configured); about 60s of no
  // interaction brings the ambient face back. One fade, no perpetual loop (respects
  // prefers-reduced-motion via the CSS media query above). The wave field is stopped for
  // the entire time the ambient face is covered by the DASHBOARD — no reason to keep
  // spending frames animating a canvas nobody can see. It keeps running behind the LOCK
  // overlay though (glance-free, not a blackout). ----------------
  var IDLE_MS = 60000;
  var idleTimer = null;
  var dissolved = false;

  function armIdle(){
    clearTimeout(idleTimer);
    idleTimer = setTimeout(returnToAmbient, IDLE_MS);
  }
  function returnToAmbient(){
    dissolved = false;
    authenticated = false;   // re-arm: the next wake needs auth again
    closeLockPad();          // in case a pad/biometric prompt was left open mid-entry
    panel.classList.remove("core-dissolved");
    panel.removeAttribute("aria-hidden");
    clearTimeout(idleTimer);
    closeAlertsPop();                    // don't leave the glance-box floating over the ambient face
    if(!document.hidden) wave.start();   // ambient face is back on top: resume the wave
  }
  // Tapping the Wavr brand mark (top-left) returns to the ambient wave face — the panel's
  // "home button". Exposed + wired to both the rail brand and the mobile top-bar brand.
  window.__wavrReturnToAmbient = returnToAmbient;
  ["nav-brand", "topbar-brand"].forEach(function(cls){
    var el = document.querySelector("." + cls);
    if(!el) return;
    el.style.cursor = "pointer";
    el.setAttribute("role", "button");
    el.setAttribute("tabindex", "0");
    el.setAttribute("aria-label", WavrT("Back to the ambient screen"));
    el.addEventListener("click", function(){ if(!dissolved) return; returnToAmbient(); });
    el.addEventListener("keydown", function(ev){ if((ev.key === "Enter" || ev.key === " ") && dissolved){ ev.preventDefault(); returnToAmbient(); } });
  });
  function revealDashboard(){
    if(!dissolved){
      dissolved = true;
      panel.classList.add("core-dissolved");
      panel.setAttribute("aria-hidden", "true");
      wave.stop();   // dashboard is now on top: stop spending frames on a hidden canvas
    }
    armIdle();
    // If a specific alert was tapped on the ambient face, land on it now (after any unlock).
    if(pendingAlertNav){ var _k = pendingAlertNav; pendingAlertNav = null; goToAlert(_k); }
  }
  function wake(e){
    if(dissolved){ armIdle(); return; }             // dashboard already up: just keep it alive
    // What's New takeover is up: a stray touch must NOT dissolve the panel (which would skip
    // the OK-acknowledge AND bypass the lock underneath). Only its OK button dismisses it.
    var _wn = document.getElementById("coreWhatsNew");
    if(_wn && !_wn.hidden){ armIdle(); return; }
    // Alert glance-box open: a tap inside it is handled by its buttons; a tap outside just closes
    // it — neither dissolves the panel. (navAlert() calls wake() with no event to go past this.)
    if(alertsPopEl && !alertsPopEl.hidden){
      if(e && alertsPopEl.contains(e.target)) return;
      closeAlertsPop(); armIdle(); return;
    }
    // Tapping the alert count opens the glance-box (its click handler) — never dissolves.
    if(e && alertsEl && (e.target === alertsEl || alertsEl.contains(e.target))){ armIdle(); return; }
    // dashboard-topbar: Hub/Internet explainer popover open — a tap inside it is handled by
    // its own close button; a tap outside just closes it (same shape as the alert glance-box).
    if(infoPopEl && !infoPopEl.hidden){
      if(e && infoPopEl.contains(e.target)) return;
      closeInfoPop(); armIdle(); return;
    }
    // dashboard-topbar: tapping the Hub/Internet pills or the quick-controls cluster (privacy
    // kill-switch, Watch mirror, Bluetooth status) acts immediately — it never dissolves the
    // ambient face or opens the lock, same "privacy actions stay low-friction" reasoning as
    // the camera trust receipt elsewhere; none of them reveal anything the ambient face
    // doesn't already show.
    if(e && e.target && (
      (coreHealthCoreEl && coreHealthCoreEl.contains(e.target)) ||
      (coreHealthNetEl && coreHealthNetEl.contains(e.target)) ||
      (quickEl && quickEl.contains(e.target))
    )){ armIdle(); return; }
    if(lockPadOpen()){ armIdle(); return; }          // pad/biometric prompt has its own handlers
    if(authenticated){ revealDashboard(); return; }
    if(gatingInProgress) return;
    if(hasBiometricLock()){ startBiometricAuth(); return; }
    // Fast path: a warm pinStatusCache (primed above, or refreshed by an earlier wake this
    // idle cycle) resolves the gate synchronously — the byte-for-byte-instant reveal the
    // no-lock-configured path always had, minus one genuine edge case: a touch within the
    // same tick as page load, before the eager warm-up above has resolved (falls to the
    // async branch below instead).
    if(pinStatusCache && (Date.now() - pinStatusCache.at) < PIN_CACHE_MS){
      if(pinStatusCache.value) openLockPad(); else { authenticated = true; revealDashboard(); }
      return;
    }
    gatingInProgress = true;
    fetchPinConfigured().then(function(pinSet){
      gatingInProgress = false;
      if(pinSet){ openLockPad(); }
      else { authenticated = true; revealDashboard(); }   // nothing configured: today's behavior, unchanged
    }, function(){
      // Belt to the deadline's braces. `fetchPinConfigured` resolves on every
      // path it knows about, so this only runs if something unforeseen threw —
      // and WHAT we do next matters far less than clearing the flag, because a
      // wake that leaves the gate armed makes every later tap on the panel a
      // silent no-op for the life of the page. Whatever went wrong, the next
      // touch gets to ask again.
      gatingInProgress = false;
    });
  }
  ["pointerdown", "touchstart", "keydown"].forEach(function(evt){
    document.addEventListener(evt, wake, true);
  });

  // Also pause while the document itself is hidden (screen off / app backgrounded) —
  // rAF may already throttle in that state on some browsers, but this makes the stop
  // explicit and instant rather than relying on that, and resumes on return only if the
  // ambient face is actually the one on top (not mid-dashboard-interaction).
  document.addEventListener("visibilitychange", function(){
    if(document.hidden) wave.stop();
    else if(!dissolved) wave.start();
  });
})();
