// ==========================================================================
// status-panel.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ---- Status panel (Plano A / live only, read-only): GET /api/status ----
// version, per-source active/inactive, feature on/off chips, house floor/room counts.
// Read-only mirror of backend state — built with createElement/textContent only (no
// innerHTML with server data), same XSS discipline as the Wavr Net panel above.
let refreshStatus = null;
// One literal per entry so every chip word is a visible `WavrT("...")` call
// site, resolved at paint time. Protocol names map to themselves — "checked,
// and it stays" rather than looking like a string somebody forgot.
const STATUS_FEATURE_LABEL = {
  multidevice: () => WavrT("multi-device"), mqtt: () => WavrT("MQTT"),
  ha_discovery: () => WavrT("Home Assistant"), mcp_control: () => WavrT("MCP control"),
  narrate: () => WavrT("narration"), net_inventory: () => WavrT("network inventory"),
  tls: () => WavrT("TLS"), ntfy: () => WavrT("notifications"),
};
function renderStatus(){
  // live (loopback) OR companion viewer (Bearer token) — both read the same read-only /api/status.
  // Feeding statusInfo (via window.__wavrStatus) is what populates the System tab's egress/sensing/
  // sensing-level surfaces for a paired viewer (fixes "no hub connected" there). Demo never runs this.
  if(MODE!=="live" && !(MODE==="companion" && companionToken())) return;
  document.getElementById("statusPanel").hidden = false;
  const versionEl  = document.getElementById("statusVersion");
  const sourcesEl  = document.getElementById("statusSources");
  const featuresEl = document.getElementById("statusFeatures");
  const houseEl    = document.getElementById("statusHouse");
  const internetEl = document.getElementById("statusInternet");
  // Mobile: route the read through the native pinned fetch (base = stored central) with a Bearer, so
  // the token never hits the app's own https://localhost. Absent the hook, __nf is exactly the
  // original same-origin fetch — live behavior verbatim. 401/403 → drop back to pairing.
  const auth = MODE==="companion" ? {"Authorization":"Bearer "+companionToken()} : null;
  const __nf = (path, opt)=> window.WAVR_MOBILE
    ? window.WAVR_MOBILE.netFetch(window.WAVR_MOBILE.base + path, opt)
    : fetch(location.origin + path, opt);
  // No answer is a PAYLOAD, not a silence.
  //
  // This `return`ed on failure, so `window.__wavrStatus` was simply never
  // called and every consumer kept its last verdict — including the Core
  // Panel's internet pill, which is the ambient face on somebody's wall. And
  // a failure was the easy case: a Core that goes DARK never settles the fetch
  // at all, so the poll never even reached the catch.
  //
  // The deadline turns silence into a failure. This shape turns the failure
  // into an answer every consumer already handles correctly: `s.internet` is
  // absent, so the pill resolves to "cannot tell" rather than to false, and
  // `s.sources` is absent, so per-source chips hide. Passing `null` would have
  // been the smaller edit and would have thrown in any consumer that reads a
  // field without guarding first.
  const NO_ANSWER = {unavailable: true};
  const REQ_TIMEOUT_MS = 8000;

  /* The mobile companion's read is a bridge call, and it needs a deadline too.
   *
   * `WAVR_MOBILE.netFetch` is the native pinned fetch — not `WavrAPI.fetch` —
   * so the 8s deadline the web path carries stopped at the branch above and the
   * phone kept the pre-fix behaviour in full: a bridge that never settles
   * freezes this poll exactly the way a dark host freezes the browser one. That
   * matters more here than almost anywhere, because this poll is the PRODUCER
   * for `window.__wavrStatus`, and the Core Panel's internet pill on somebody's
   * wall is one of its consumers.
   *
   * A race rather than an `AbortSignal`: nothing here can promise a bridge
   * implementation honours `signal`, and a race needs no cooperation from the
   * thing it is racing. The underlying request may well still be in flight
   * afterwards; what matters is that the CALLER stops waiting on it and renders
   * an answer.
   */
  function withDeadline(p, ms){
    let timer;
    return Promise.race([
      p.then(function(v){ clearTimeout(timer); return v; },
             function(e){ clearTimeout(timer); throw e; }),
      new Promise(function(_, reject){
        timer = setTimeout(function(){ reject(new Error("deadline")); }, ms);
      })
    ]);
  }

  /* No answer, drawn into the panel's OWN DOM.
   *
   * `NO_ANSWER` reached every `window.__wavrStatus` consumer and then the
   * function `return`ed, so the panel these ids belong to — System's own Status
   * tile — was the one surface the payload never touched. It kept its last
   * paint: a green "Internet: OK", a row of active sensors and a feature list,
   * all describing a Core that had stopped answering. The Core Panel's pill
   * correctly fell back to "…" at the same moment, so the product was saying
   * two different things about one Core on two screens.
   *
   * Blanked rather than dimmed: every one of these lines is a specific claim
   * (a version, which sensors are live, how many rooms), and a stale specific
   * claim is worse than none. The version slot carries the sentence because it
   * is the first line of the tile.
   */
  function renderNoAnswer(){
    window.__wavrStatus?.(NO_ANSWER);
    versionEl.textContent = WavrT("Wavr is not answering, so this cannot be checked. Check that the Core is running.");
    if(internetEl){
      internetEl.hidden = false;
      internetEl.className = "status-internet off";
      internetEl.textContent = WavrT("Not responding");
    }
    sourcesEl.textContent = "";
    featuresEl.textContent = "";
    houseEl.textContent = "";
  }

  async function refresh(){
    let s; try{
      const r = window.WAVR_MOBILE
        ? await withDeadline(__nf("/api/status", auth?{headers:auth}:undefined),
                             REQ_TIMEOUT_MS)
        : await WavrAPI.fetch("/api/status",
                              {headers: auth || undefined, timeoutMs: REQ_TIMEOUT_MS});
      if(auth && (r.status===401||r.status===403)){ companionAuthFailed(); return; }
      if(!r.ok){ renderNoAnswer(); return; }
      s = await r.json();
    }catch{ renderNoAnswer(); return; }
    window.__wavrStatus?.(s);   // Stage-3b hook: egress dashboard + Sobre reuse this SAME payload
    // WHICH Space is this? On loopback the first-run probe already answered,
    // but that route is loopback-root-gated, so on a paired phone it 403s and
    // the wordmark stays generic -- on the one device most likely to be
    // attached to two Cores. /api/status carries presence:read, which every
    // companion holds, so this is the answer that reaches everybody.
    if(window.__wavrShowSpace) window.__wavrShowSpace(s.space);
    versionEl.textContent = WavrT("version {version}", {version: s.version ?? "?"});
    // Internet up/down (item: internet monitor) — ok:null means the monitor is off or the
    // first check hasn't run yet, so show a muted hint instead of a red/green verdict.
    if(internetEl){
      const inet = s.internet && typeof s.internet==="object" ? s.internet : {};
      const monitorOn = !!(s.features && s.features.internet_monitor);
      internetEl.hidden = false;
      if(inet.ok === true){
        internetEl.className = "status-internet ok";
        internetEl.textContent = WavrT("Internet: OK");
      } else if(inet.ok === false){
        internetEl.className = "status-internet down";
        const since = fmtDateTime(inet.since);
        internetEl.textContent = since
          ? WavrT("Internet: down · since {when}", {when: since})
          : WavrT("Internet: down");
      } else {
        internetEl.className = "status-internet off";
        internetEl.textContent = monitorOn ? WavrT("internet: checking connection…")
                                           : WavrT("internet monitor off");
      }
    }
    sourcesEl.textContent = "";
    (Array.isArray(s.sources) ? s.sources : []).forEach(src=>{
      const row = document.createElement("div"); row.className = "status-src-row";
      const dot = document.createElement("i"); dot.className = "h" + (src.active ? " active" : "");
      const name = document.createElement("span"); name.textContent = modalityLabel(src.name) || src.name;
      row.appendChild(dot); row.appendChild(name);
      sourcesEl.appendChild(row);
    });
    featuresEl.textContent = "";
    const feats = s.features && typeof s.features==="object" ? s.features : {};
    Object.keys(feats).forEach(k=>{
      const chip = document.createElement("span");
      chip.className = "status-chip" + (feats[k] ? " on" : "");
      // Whole clause with a slot: the feature name is one word of a sentence,
      // not a label with ": on" stuck to the end of it.
      const fLabel = STATUS_FEATURE_LABEL[k] ? STATUS_FEATURE_LABEL[k]() : k;
      chip.textContent = feats[k] ? WavrT("{feature}: on", {feature: fLabel})
                                  : WavrT("{feature}: off", {feature: fLabel});
      featuresEl.appendChild(chip);
    });
    const h = s.house && typeof s.house==="object" ? s.house : {};
    houseEl.textContent = WavrT("plan: {floors} floor(s) · {rooms} room(s)",
                                {floors: h.floors ?? "?", rooms: h.rooms ?? "?"});
  }
  refreshStatus = refresh;
  refresh();
  // Live drives refreshStatus from renderControls' 3s poll; companion (renderControls is live-only)
  // needs its own cadence to keep statusInfo fresh for the System-tab surfaces.
  if(MODE==="companion") setInterval(refresh, 3000);
}
renderStatus();

