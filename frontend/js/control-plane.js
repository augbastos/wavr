// ==========================================================================
// control-plane.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ---- Control plane (Plano A / live only): global on/off + per-source on/off ----
async function renderControls(){
  // Fix D: an admin (central) companion also gets the System tab -- confirmed via the
  // authoritative GET /api/devices/me read (companionIsCentral), never the shim's role hint
  // alone. A 'user' companion keeps this whole tile hidden (companionIsCentral() is false).
  const companionCentral = MODE==="companion" && companionIsCentral();
  if(MODE!=="live" && !companionCentral) return;   // the control plane is a backend feature
  document.getElementById("controls").hidden = false;
  // Mobile: route through the native pinned fetch (base = stored central) so the companion
  // Bearer token never hits the app's own https://localhost -- same __nf pattern as
  // renderStatus/renderNetwork. Absent the hook, __nf is exactly the original same-origin fetch.
  const companionAuth = companionCentral ? {"Authorization":"Bearer "+companionToken()} : null;
  const __nf = (path, opt)=> window.WAVR_MOBILE
    ? window.WAVR_MOBILE.netFetch(window.WAVR_MOBILE.base + path, opt)
    : fetch(location.origin + path, opt);
  // Fix #1 (source toggles look dead): no longer swallows a failed POST into a silent
  // "success" — a network error or a 403 (missing local-admin scope) resolves to null so
  // every caller below can tell a real write from a no-op and surface it instead of lying.
  // Fix D: companion branch sends the Bearer instead of the loopback-only X-Wavr-Local header
  // (require_local admits root+X-Wavr-Local OR an authenticated central peer, see app.py).
  const post = (url,body)=> __nf(url,{method:"POST",
    headers: companionCentral
      ? Object.assign({"Content-Type":"application/json"}, companionAuth)
      : {"Content-Type":"application/json","X-Wavr-Local":"1"},
    body:JSON.stringify(body)})
    .catch(()=>null);
  // Fix #3 (honest unavailable state): /api/system already returns {enabled,active} per
  // source (sourcemanager.py) — active=false while enabled=true means the source's task
  // isn't actually running (e.g. a privileged bind failed under a non-root proot, or the
  // task crashed and self-terminated). A source only paints as the calm "unavailable"
  // chip once that reading has been STABLE for 2 consecutive 3s polls (never on a source
  // merely mid-restart).
  // Fix #1 (CRITICAL): the same {enabled:true, active:false} reading ALSO happens for
  // EVERY enabled source the instant the master switch pauses the whole system —
  // SourceManager.stop() kills all tasks but leaves _enabled untouched. So "stuck" must
  // also require s.running, or a deliberate pause falsely paints every source as
  // "unavailable — needs privileged mode" and the user loses the ability to pre-stage
  // sources while paused.
  // Fix #12: the old PROOT_BLOCKED instant-unavailable map is removed — dhcp_fp/
  // rogue_dhcp are netinventory collectors, never SourceManager sources, so they can
  // never appear in s.sources; the map was dead code. The env-unavailable signal for
  // those two now comes from GET /api/status's "availability" field (consumed by
  // renderSensing, not here).
  const unavailStreak = {};
  // Fix #3/#11: per-source POST-failure messages, persisted across the 3s
  // wrap.innerHTML rebuild below — without this, a failure either writes into a node
  // that's about to be detached, or gets wiped by the next poll tick before the user
  // can read it (the exact "toggles silently fail" complaint this is meant to fix).
  const srcError = {};
  async function refresh(){
    let s; try{
      const r = await __nf("/api/system", companionAuth?{headers:companionAuth}:undefined);
      if(companionAuth && (r.status===401||r.status===403)){ companionAuthFailed(); return; }
      s = await r.json();
    }catch{ return; }
    window.__wavrSystem?.(s);   // Stage-2 hook: Dispositivos→Ativos badge/rows share this payload
    const sys = document.getElementById("sysToggle");
    const sysFb = document.getElementById("sysFb");
    // Item 7: a real switch affordance ("System" + on/off track) instead of an ambiguous
    // "System: ON" state label — role=switch + aria-checked keep it accessible.
    sys.textContent = WavrT("System");
    sys.className = "ctl switch " + (s.running ? "on" : "off");
    sys.setAttribute("role", "switch");
    sys.setAttribute("aria-checked", s.running ? "true" : "false");
    sys.setAttribute("aria-label", WavrT(s.running ? "System: on" : "System: off"));
    // Fix #10/#13: the master switch now surfaces its own POST failures instead of doing
    // nothing (it was the one control the per-source failure-surfacing fix skipped).
    // #sysToggle is a fixed element (restyled every refresh() but never recreated, and
    // .disabled is never reset elsewhere) so BOTH branches must re-enable it, or one
    // failed POST leaves the master switch permanently disabled until page reload.
    sys.onclick = async ()=>{
      sys.disabled = true;
      const r = await post("/api/system/toggle",{on:!s.running});
      sys.disabled = false;
      if(r && r.ok){ refresh(); }
      else { actionFeedback(sysFb, false, WavrT("Couldn't change — this control needs local admin access")); }
    };
    const wrap = document.getElementById("srcToggles"); wrap.innerHTML = "";
    s.sources.forEach(src=>{
      const label = modalityLabel(src.name) || src.name;
      const row = document.createElement("div"); row.className = "src-toggle-row";
      // `healthy` when the Core supplies it (supervised SourceManager), `active`
      // as the fallback for an older Core. The distinction is load-bearing now:
      // sources restart themselves, so a permanently broken one has a live task
      // at nearly every instant — reading `!active` would have made this chip
      // quietly stop appearing at exactly the moment it became most useful.
      const working = ("healthy" in src) ? src.healthy : src.active;
      const stuck = s.running && src.enabled && !working;
      const streak = stuck ? (unavailStreak[src.name] || 0) + 1 : 0;
      unavailStreak[src.name] = streak;
      const unavailable = stuck && streak >= 2;
      if(unavailable){
        // Fix #4: enabled-but-not-working is NOT necessarily a privilege issue — a
        // crashed source (a camera feed dying, a serial device unplugged) looked
        // identical from {enabled,active} alone, so the copy stayed cause-neutral.
        // The supervisor now names the state, so the copy can say which of those
        // it is — with one exception kept deliberately vague: `retrying` may still
        // be a privileged-bind failure on a proot Android Core, and asserting a
        // cause we do not have would send somebody down the wrong path.
        const SRC_STATE_COPY = {
          retrying: () => WavrT("Not running — Wavr is retrying (may need privileged mode on this device)"),
          failed:   () => WavrT("Not running after several attempts — check the device, then tap to try again now"),
          silent:   () => WavrT("Connected but sending nothing — the feed has stalled"),
          completed:() => WavrT("Stopped on its own without an error — tap to start it again"),
        };
        const why = SRC_STATE_COPY[src.state] ? SRC_STATE_COPY[src.state]()
          : WavrT("Enabled but not running — tap to restart (may need privileged mode on this device)");
        const b = document.createElement("button"); b.type = "button";
        b.className = "ctl small src-unavail"; b.style.cursor = "pointer";
        b.textContent = WavrT("{source}: restart", {source: label});
        b.setAttribute("data-tip", why);
        const note = document.createElement("span"); note.className = "src-unavail-note";
        note.textContent = why;
        const fb = document.createElement("span"); fb.className = "action-fb"; fb.setAttribute("aria-live", "polite");
        if(srcError[src.name]){ fb.className = "action-fb err"; fb.textContent = srcError[src.name]; }
        b.onclick = async ()=>{
          b.disabled = true;
          // One call, not an off/on pair: the pair briefly published
          // enabled=false, and a coverage snapshot landing in that window
          // recorded a deliberate choice nobody made.
          const r = await post(`/api/sources/${src.name}/restart`, {});
          if(r && r.ok){ delete srcError[src.name]; refresh(); }
          else {
            b.disabled = false;
            srcError[src.name] = WavrT("Couldn't restart — this control needs local admin access");
            actionFeedback(fb, false, srcError[src.name]);
          }
        };
        row.appendChild(b); row.appendChild(note); row.appendChild(fb);
      } else {
        const b = document.createElement("button"); b.type = "button";
        // Item 2 (Changes list): the same .ctl.switch track/knob grammar as #sysToggle —
        // visually distinct from a read-only status chip — instead of a weak "ctl small" pill.
        b.className = "ctl switch " + (src.enabled ? "on" : "off");
        b.textContent = label;
        b.setAttribute("role", "switch");
        b.setAttribute("aria-checked", src.enabled ? "true" : "false");
        b.setAttribute("aria-label", WavrT(src.enabled ? "{source}: on" : "{source}: off", {source: label}));
        // v2 tooltips: per-source explainer (SOURCE_TIP map lives in the tooltip block;
        // its fallback string guarantees an unmapped name is never tooltip-less)
        b.setAttribute("data-tip", typeof window.__wavrSourceTip==="function" ? window.__wavrSourceTip(src.name) : "");
        const fb = document.createElement("span"); fb.className = "action-fb"; fb.setAttribute("aria-live", "polite");
        // Fix #3/#11: re-render any error persisted from a prior tick so the "couldn't
        // change" message survives the 3s wrap.innerHTML rebuild instead of vanishing
        // before the user reads it. Plain re-render (no actionFeedback call here) so
        // screen readers aren't re-announced every 3s — the onclick handler's own
        // actionFeedback call below handles the live announcement on the attempt itself.
        if(srcError[src.name]){ fb.className = "action-fb err"; fb.textContent = srcError[src.name]; }
        b.onclick = async ()=>{
          b.disabled = true;
          const r = await post(`/api/sources/${src.name}/toggle`, {enabled: !src.enabled});
          if(r && r.ok){ delete srcError[src.name]; refresh(); }
          else {
            b.disabled = false;
            srcError[src.name] = WavrT("Couldn't change — this control needs local admin access");
            actionFeedback(fb, false, srcError[src.name]);
          }
        };
        row.appendChild(b); row.appendChild(fb);
      }
      wrap.appendChild(row);
    });
  }
  // Status panel mirrors the same backend state — refresh it on the same cadence/trigger
  // as this system panel (per the spec: "refresh it when the system panel refreshes").
  refresh(); setInterval(()=>{ refresh(); refreshStatus?.(); }, 3000);
}
renderControls();

