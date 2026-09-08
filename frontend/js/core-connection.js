// ==========================================================================
// core-connection.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ---- Reconnection UI (live + companion): dim the house pill/hero + label "reconnecting…"
// while the WS is down; cleared on the very next onmessage frame (see handle() below).
// Looks elements up live (not via a closed-over const) so it works regardless of where
// the providers below are defined relative to the DOM-reference declarations further down.
function setReconnecting(on){
  const house = document.getElementById("house");
  const hero1 = document.getElementById("heroLine1");
  // The suffix is translated, so it is matched and stripped as a plain string —
  // a regex over the English words would never find the Portuguese one.
  const SUFFIX = " · " + WavrT("reconnecting…");
  [house, hero1].forEach(el=>{
    if(!el) return;
    el.classList.toggle("stale", on);
    const hasSuffix = el.textContent.slice(-SUFFIX.length) === SUFFIX;
    if(on && !hasSuffix) el.textContent += SUFFIX;
    else if(!on && hasSuffix) el.textContent = el.textContent.slice(0, -SUFFIX.length);
  });
  // Heartbeat badge pauses/ambers while the stream is down — "calm" and "broken" must
  // never look the same (§2.1), so the badge itself carries the degraded state.
  const lb = document.getElementById("heroLive");
  if(lb && !lb.hidden) lb.classList.toggle("stale", on);
  // The room cards ARE the screen, and they were the one part of it this call
  // did not touch: the pill above them dimmed and said "reconnecting…", while
  // every card underneath went on reading "Occupied · 92%" from a frame that
  // arrived ten minutes ago. Nothing here changes a verdict — a client that
  // decided a room was empty because the stream went quiet would be inventing
  // one — it says that what is on screen is the last thing that arrived.
  const rooms = document.getElementById("rooms");
  if(rooms) rooms.classList.toggle("stale", on);
  const roomsNote = document.getElementById("roomsStale");
  if(roomsNote) roomsNote.hidden = !on;
  // The map paints room presence from the SAME frames. Leaving it lit beside a
  // dimmed grid is worse than leaving both lit: two halves of one screen
  // disagreeing about one silence, and the reader has no way to tell which
  // half is right.
  const map = document.getElementById("radarWrap");
  if(map) map.classList.toggle("stale", on);
  // "Who's here" (whoshome.js's qcTile) reads the SAME roomOcc/roomConf accumulator
  // this call already freezes for the room grid, but nothing here used to touch it —
  // so a dead Core left a big, confidently-coloured "Someone is here" (or a green
  // "live" badge two lines above it) on screen with nothing behind either claim. Same
  // treatment as #rooms: dim, add the honest note, change no verdict. whoshome.js's own
  // render() is untouched — it still owns what the headline SAYS; this only says
  // whether that sentence can still be trusted right now.
  const qc = document.getElementById("qcTile");
  if(qc) qc.classList.toggle("stale", on);
  const qcNote = document.getElementById("qcStaleNote");
  if(qcNote) qcNote.hidden = !on;
}
/* A socket that is OPEN but SILENT is the dark-Core case for the live stream.
 *
 * A host that dies — power cut, suspended laptop, dropped Wi-Fi — does not
 * close the socket. Nothing arrives, `onclose` never fires, `handle()` in
 * render.js is never called, and every presence verdict on the screen freezes
 * at whatever it last said while the kiosk goes on painting "Hub ✓". Anything
 * hung off `onclose` is blind to this by construction: the event it waits for
 * is the one a dark host never sends. It takes a timer of its own, measuring
 * silence against the clock — the same shape `runtime.js` uses, and for the
 * same reason.
 *
 * ONE pair of constants for BOTH providers: the companion and the loopback
 * dashboard read the same `/ws/live` fan-out, so "how long is too quiet" is one
 * fact about one stream and must not be two numbers that drift apart.
 */
const WS_SILENCE_MS = 60000;      // open-but-silent for longer than this: stop claiming live
const WS_SILENCE_CHECK_MS = 15000; // the watchdog's own cadence, independent of any frame
// ---- DataProvider contract: { start(onEvent), history() } — items are RoomState dicts ----
function WebSocketProvider(){
  const base = location.origin;
  // Watchdog state, per provider (see the note above WS_SILENCE_MS). The
  // provider contract has no stop/teardown, so the interval is created once and
  // guarded — reconnects call start() again and must never stack intervals.
  let lastFrameTs = 0;      // ms timestamp of the last received frame (0 = none yet)
  let wsOpen = false;       // true from socket creation until its onclose fires
  let staleFlagged = false; // watchdog raised the reconnecting indicator; cleared on next frame
  let staleTimer = null;    // the single per-provider watchdog interval
  return {
    // A DEADLINE, because render.js AWAITS this before it calls start(). A dark
    // host never settles the request, so the live socket would never be opened
    // at all and the page would sit on its boot paint for ever — a freeze one
    // step upstream of the one the watchdog below catches. Timing out yields
    // the same empty history a refused request already did, and the socket
    // still gets its chance.
    async history(){
      try{ const r = await WavrAPI.fetch("/api/history?limit=100", {timeoutMs: 8000}); return await r.json(); }
      catch{ return []; }
    },
    start(onEvent){
      const ws = new WebSocket(base.replace(/^http/,"ws")+"/ws/live");
      wsOpen = true; lastFrameTs = Date.now();   // fresh socket: a full silence window before flagging
      ws.onmessage = (m)=> {
        lastFrameTs = Date.now();
        if(staleFlagged){ staleFlagged = false; setReconnecting(false); }   // frames flow again — drop the watchdog's claim
        onEvent(JSON.parse(m.data));
      };
      ws.onclose = ()=> { wsOpen = false; setReconnecting(true); setTimeout(()=> this.start(onEvent), 1500); };
      // Flip the same reconnecting indicator a real close flips — the verdict
      // resolves to "cannot tell", never to the last good one. Do NOT close and
      // reopen the socket here: `onclose` owns real reconnects, and a watchdog
      // that also reconnects is a second reconnect loop nobody is reading.
      if(staleTimer === null){
        staleTimer = setInterval(()=>{
          if(wsOpen && !staleFlagged && (Date.now() - lastFrameTs) > WS_SILENCE_MS){
            staleFlagged = true;
            setReconnecting(true);
          }
        }, WS_SILENCE_CHECK_MS);
      }
    },
  };
}
function SimulatorProvider(){
  // Fictional multi-modal apartment producing RoomState directly (no backend).
  const rooms = {
    "whole home":  [["network"]],
    "living room": [["wifi_csi"]],
    bedroom:       [["wifi_csi"],["camera"]],
    backyard:      [["camera"]],
  };
  // Trust weights per modality — mirror backend FusionEngine DEFAULT_WEIGHTS.
  const W = {camera:1.0, wifi_csi:0.85, network:0.5, sim:0.6};
  let tick = 0;
  function stateFor(room){
    const mods = rooms[room];
    // Mirror the real engine: confidence = agreement × strength, so a lone coarse
    // source (e.g. network) can never read 100% — same math the backend uses.
    let num=0, den=0, strength=0; const sources=[]; let vitals={}; let hasTarget=false;
    mods.forEach(([m],i)=>{
      const present = ((tick+i)%7)<4;
      const conf = present ? 0.9 : 0.2;      // detector's own confidence (independent of trust weight)
      const mass = W[m]*conf;
      den += mass;
      if(present){ num += mass; strength = Math.max(strength, mass); }
      // Demo-only source freshness so the health/age indicator is visible without a
      // backend — mirrors fusion.py's fresh(<=30s)/stale/dead(>=90s) bands. Display
      // only: age doesn't feed the confidence math above (same as the real engine's
      // decay is applied to trust, not shown here).
      const age_s = ((tick + i*5) % 15) * 8;   // 0..112s, cycles through the bands
      const health = age_s <= 30 ? "fresh" : age_s >= 90 ? "dead" : "stale";
      sources.push({modality:m, presence:present, confidence:+conf.toFixed(3), age_s, health});
      if(present && m==="wifi_csi") vitals={breathing_bpm:+(12+3*Math.sin(tick/5)).toFixed(1), heart_bpm:+(60+10*Math.sin(tick/4)).toFixed(0)};
      if(present && (m==="wifi_csi"||m==="camera")) hasTarget = true;
    });
    const agreement = den>0 ? num/den : 0;
    const confidence = +(agreement*strength).toFixed(3);
    const parts = sources.map(s=> `${s.modality}: ${s.presence?"present":"empty"}`);
    // Same deterministic ellipse walk + posture cycle as the Python SimulatedSource,
    // so the public demo shows a moving radar target (id=1) with no real data.
    const posture = ["walking","standing","sitting"][Math.floor(tick/5)%3];
    const targets = hasTarget ? [{
      id:1, x:+(2.0+1.6*Math.sin(tick/4)).toFixed(2), y:+(1.5+1.1*Math.cos(tick/4)).toFixed(2),
      z:null, posture, velocity: posture==="walking" ? 0.5 : 0.0, confidence:0.9,
    }] : [];
    return {room, occupied:confidence>=0.5, confidence, vitals, sources, targets,
            explanation: parts.join(" · ")+` → ${Math.round(confidence*100)}% occupied`,
            ts:new Date().toISOString()};
  }
  const all = ()=> Object.keys(rooms).map(stateFor);
  return {
    async history(){ const out=[]; for(let t=0;t<12;t++){ tick=t; out.push(...all()); } tick=12; return out; },
    start(onEvent){ setInterval(()=>{ all().forEach(onEvent); tick++; }, 1500); },
  };
}

// ---- Mode selection (PRIVACY: never "live" off localhost) ----
// 3-way detection (Phase-3 multi-device):
//   loopback host        -> "live"       central: full dashboard, X-Wavr-Local, no token — UNCHANGED
//   private-LAN IP host   -> "companion" phone/2nd PC: pair once, then token-authed read-only viewer
//   anything else        -> "simulated"  public off-LAN demo — UNCHANGED
// Central and demo run NONE of the companion/token code below (every branch is gated on MODE).
function isLoopbackHost(h){ return h==="localhost" || h==="127.0.0.1"; }
function isPrivateLanHost(h){        // RFC1918 literals only; names like lvh.me are NOT private literals -> demo
  return /^10\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(h)
      || /^192\.168\.\d{1,3}\.\d{1,3}$/.test(h)
      || /^172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}$/.test(h);
}
const COMPANION_TOKEN_KEY = "wavr.token." + location.origin;   // per-origin (works under http & https)
// Mobile (Capacitor) opt-in: when window.WAVR_MOBILE is present the token lives in native
// Keystore-backed storage (never localStorage), read via a synchronous in-memory cache the shim
// populated during WAVR_MOBILE.ready. When the hook is ABSENT the original localStorage path runs.
function companionToken(){ if(window.WAVR_MOBILE) return window.WAVR_MOBILE.tokenGet(); try{ return localStorage.getItem(COMPANION_TOKEN_KEY) || null; }catch{ return null; } }
function companionSetToken(t){ if(window.WAVR_MOBILE){ window.WAVR_MOBILE.tokenSet(t); return; } try{ localStorage.setItem(COMPANION_TOKEN_KEY, t); }catch{} }
function companionClearToken(){ if(window.WAVR_MOBILE){ window.WAVR_MOBILE.tokenSet(null); return; } try{ localStorage.removeItem(COMPANION_TOKEN_KEY); }catch{} }
function companionDeviceName(){ try{ return "Companion · " + (navigator.platform || "web"); }catch{ return "Companion"; } }
let companionAuthFailedOnce = false;
function companionAuthFailed(){        // token revoked/expired (401/403) -> clear token + drop back to pairing
  if(companionAuthFailedOnce) return;
  companionAuthFailedOnce = true;
  companionClearToken();
  try{ location.reload(); }catch{}
}
// ---- Capability Manifest: this device describing ITSELF --------------------
// The Core scans its own host (capabilities.scan_host); a companion has to say
// what it is, or the Devices screen and get_device_context stay empty forever.
//
// HONESTY RULE, same as the backend's: a key we cannot determine is OMITTED, so
// it reads as "unknown". Never `false`. `navigator.bluetooth` missing means this
// BROWSER has no Web Bluetooth (Safari, Firefox), not that the phone has no
// radio -- writing `ble: false` there would be a confident wrong answer about
// someone's hardware.
async function describeThisDevice(){
  const caps = {};

  // enumerateDevices() lists input KINDS without permission, so this is a real
  // probe: an answer of "no videoinput" is a genuine false, not a shrug.
  try{
    if(navigator.mediaDevices && navigator.mediaDevices.enumerateDevices){
      const list = await navigator.mediaDevices.enumerateDevices();
      caps.camera = list.some(d => d.kind === "videoinput");
      caps.microphone = list.some(d => d.kind === "audioinput");
    }
  }catch{ /* the API refused -> stays unknown, which is the truth */ }

  // Presence of the API is evidence of capability; absence is evidence of
  // nothing, so there is no `else`.
  if(navigator.bluetooth) caps.ble = true;
  if(typeof navigator.getBattery === "function") caps.battery = true;

  const conn = navigator.connection || null;
  if(conn && typeof conn.type === "string"){
    if(conn.type === "wifi") caps.wifi = true;
    else if(conn.type === "ethernet") caps.ethernet = true;
  }

  const ua = (navigator.userAgent || "").toLowerCase();
  const platform =
    /android/.test(ua) ? "android" :
    /iphone|ipad|ipod/.test(ua) ? "ios" :
    /windows/.test(ua) ? "windows" :
    /mac os/.test(ua) ? "macos" :
    /linux/.test(ua) ? "linux" : "unknown";

  // What this device could technically DO. `client` always -- it is running the
  // dashboard. `node` only when it has something that actually senses; claiming
  // otherwise would put a "could be a Node" suggestion in front of an admin for
  // a device that cannot sense anything.
  const functions = ["client"];
  if(caps.camera === true || caps.ble === true) functions.push("node");

  const body = {platform: platform, capabilities: caps,
                functions_supported: functions, protocol_version: 1};
  if(typeof navigator.hardwareConcurrency === "number")
    body.cpu_count = navigator.hardwareConcurrency;
  if(typeof navigator.deviceMemory === "number")
    body.ram_mb = Math.round(navigator.deviceMemory * 1024);
  return body;
}

let __manifestSent = false;
async function reportOwnManifest(){
  if(__manifestSent || MODE !== "companion") return;   // the Core scans itself
  const tok = companionToken();
  if(!tok) return;
  __manifestSent = true;          // once per session, whatever the outcome
  try{
    const body = await describeThisDevice();
    const nf = (path, opt)=> window.WAVR_MOBILE
      ? window.WAVR_MOBILE.netFetch(window.WAVR_MOBILE.base + path, opt)
      : fetch(location.origin + path, opt);
    await nf("/api/devices/me/manifest", {
      method: "PUT",
      headers: {"Authorization": "Bearer " + tok,
                "Content-Type": "application/json"},
      body: JSON.stringify(body)
    });
  }catch{ /* describing itself is a nicety; failing must not break the app */ }
}

// Cluster D (admin-companion System tab): the shim's window.WAVR_MOBILE.role hint is a UX
// signal only -- the backend's GET /api/devices/me (Bearer-authed) is the ONE authoritative
// read of this companion's role, same as every other write path in this file. Resolved lazily
// and cached (not polled): null = not checked yet, "central"/"user" = confirmed, false =
// checked and not central (or no token yet). A 'user' companion NEVER sees these controls --
// only require_local+control backend routes ever run for it, and those already 403 a non-
// central peer, so this is UI-honesty, not the security boundary.
let companionRoleCache = null;
function companionIsCentral(){
  if(companionRoleCache === null){
    if(MODE!=="companion" || !companionToken()) return false;   // nothing to resolve yet
    companionRoleCache = false;      // provisional -- guards against re-entrant fetches while in flight
    const __nf = (path, opt)=> window.WAVR_MOBILE
      ? window.WAVR_MOBILE.netFetch(window.WAVR_MOBILE.base + path, opt)
      : fetch(location.origin + path, opt);
    __nf("/api/devices/me", {headers:{"Authorization":"Bearer "+companionToken()}})
      .then(async r=>{
        if(r.status===401||r.status===403){ companionAuthFailed(); return; }
        const j = await r.json();
        companionRoleCache = (j && j.role) || false;
        // The token is proven good at this point, so describe ourselves once.
        reportOwnManifest();
        // Repaint now that the role is known: renderControls (gated, sets up its own poll)
        // + a fresh /api/status fetch, whose window.__wavrStatus hook repaints the paint-only
        // Health/Doctor tiles (same "refresh it when the system panel refreshes" contract the
        // 3s renderControls poll already uses at the bottom of this function).
        renderControls();
        if(typeof refreshStatus === "function") refreshStatus();
        // Fix C-front (a): this doubles as the "role SETTLE" hook for the bonded-Bluetooth
        // tile -- renderMobileBonded() itself is idempotent (rebuilds its list from scratch,
        // no listeners left dangling) and still gates on the shim's own m.role==='central' +
        // listBondedDevices capability check, so a plain-web companion or a non-central device
        // stays a no-op here; this only surfaces the tile in-session for a mobile admin whose
        // backend-confirmed role just resolved.
        if(typeof renderMobileBonded === "function") renderMobileBonded();
        // Rotinas tab (same live/central-only tier as System): re-run now that the role has
        // resolved, mirroring the renderControls() re-run just above.
        if(typeof renderRoutines === "function") renderRoutines();
      })
      .catch(()=>{ companionRoleCache = null; });   // transient failure -- allow a retry on next paint
  }
  return companionRoleCache === "central";
}
// Companion data provider: Bearer-authed history + ticketed WS (same contract as WebSocketProvider).
function CompanionProvider(token){
  // Mobile: base = stored central, fetch = native pinned fetch, WS = native pinned socket (the
  // WebView cannot validate a self-signed wss). Absent the hook, every branch below is unchanged.
  const __m = window.WAVR_MOBILE || null;
  const base = __m ? __m.base : location.origin;
  const __fetch = (u,o)=> __m ? __m.netFetch(u,o) : fetch(u,o);
  const auth = { "Authorization": "Bearer " + token };
  // Staleness watchdog state: a socket can sit "open" while frames silently stop (dropped
  // route, sleeping Core) — track the last frame time and stop claiming "live" after
  // WS_SILENCE_MS of it. The provider contract has no stop/teardown, so the interval is
  // created once per provider (guarded) and duplicate start() calls never stack intervals.
  // WebSocketProvider carries the identical watchdog off the identical constants — see the
  // note above them; this is no longer companion-only.
  let lastFrameTs = 0;      // ms timestamp of the last received frame (0 = none yet)
  let wsOpen = false;       // true from socket creation until its onclose fires
  let staleFlagged = false; // watchdog raised the reconnecting indicator; cleared on next frame
  let staleTimer = null;    // the single per-provider watchdog interval
  return {
    async history(){
      try{
        const r = await __fetch(base+"/api/history?limit=100",{headers:auth});
        if(r.status===401||r.status===403){ companionAuthFailed(); return []; }
        return await r.json();
      }catch{ return []; }
    },
    async start(onEvent){
      let ticket;
      try{
        const r = await __fetch(base+"/api/ws-ticket",{method:"POST",headers:auth});   // mint a short-lived WS ticket
        if(r.status===401||r.status===403){ companionAuthFailed(); return; }
        // Retry paths must surface the degraded state (same setter ws.onclose uses) — a silent
        // retry leaves stale data presented as live while the ticket mint keeps failing.
        if(!r.ok){ setReconnecting(true); setTimeout(()=>this.start(onEvent), 2000); return; }
        ticket = (await r.json()).ticket;
      }catch{ setReconnecting(true); setTimeout(()=>this.start(onEvent), 2000); return; }
      const proto = location.protocol==="https:" ? "wss://" : "ws://";     // ws/wss chosen from page protocol
      const ws = __m ? __m.netWebSocket(__m.base.replace(/^http/,"ws") + "/ws/live?ticket=" + encodeURIComponent(ticket))
                     : new WebSocket(proto + location.host + "/ws/live?ticket=" + encodeURIComponent(ticket));
      wsOpen = true; lastFrameTs = Date.now();   // fresh socket: give it a full silence window before flagging
      ws.onmessage = (m)=> {
        lastFrameTs = Date.now();
        if(staleFlagged){ staleFlagged = false; setReconnecting(false); }   // frames flow again — drop the watchdog's claim
        onEvent(JSON.parse(m.data));
      };
      ws.onclose = ()=> { wsOpen = false; if(!companionAuthFailedOnce){ setReconnecting(true); setTimeout(()=> this.start(onEvent), 1500); } };
      // Staleness watchdog: a socket that is "open" but silent for longer than WS_SILENCE_MS
      // must stop claiming "live". Flip the same reconnecting indicator on — do NOT close/reopen
      // the socket (onclose owns real reconnects); the next received frame clears it above.
      if(staleTimer === null){
        staleTimer = setInterval(()=>{
          if(wsOpen && !staleFlagged && (Date.now() - lastFrameTs) > WS_SILENCE_MS){
            staleFlagged = true;
            setReconnecting(true);
          }
        }, WS_SILENCE_CHECK_MS);
      }
    },
  };
}
function NullProvider(){ return { async history(){ return []; }, start(){} }; }   // pairing state: no data yet

// Companion UI bootstrap: pairing screen (no token) OR read-only viewer chrome (token present).
function initCompanion(){
  if(MODE!=="companion") return;      // central + demo never touch companion UI
  const pairEl  = document.getElementById("companionPair");
  const mainEl  = document.querySelector("main");
  const radarEl = document.getElementById("radarWrap");
  const heroEl  = document.getElementById("homeHero");
  if(!companionToken()){
    // --- PAIRING SCREEN: hide the whole dashboard, show only the pairing card ---
    if(mainEl)  mainEl.hidden  = true;
    if(radarEl) radarEl.hidden = true;
    if(heroEl)  heroEl.hidden  = true;    // no RoomState yet — nothing glanceable to show
    pairEl.hidden = false;
    const form  = document.getElementById("cpairForm");
    const input = document.getElementById("cpairCode");
    const msg   = document.getElementById("cpairMsg");
    const submitBtn = form.querySelector('button[type="submit"]');
    function updatePairSubmitState(){
      const valid = /^\d{8}$/.test((input.value||"").replace(/\D+/g,""));
      submitBtn.disabled = !valid;
      submitBtn.classList.toggle("on", valid);
    }
    input.addEventListener("input", updatePairSubmitState);
    updatePairSubmitState();
    form.onsubmit = async (e)=>{
      e.preventDefault();
      const code = (input.value||"").replace(/\D+/g,"");
      if(!/^\d{8}$/.test(code)){ msg.className="cpair-msg err"; msg.textContent=WavrT("enter the 8 digits of the code"); return; }
      msg.className="cpair-msg"; msg.textContent=WavrT("pairing…");
      // Say which phone this is, when the phone can say it. Re-pairing then
      // retires this device's earlier credentials instead of leaving another
      // live key to the home behind — and this form is REACHED by re-trying,
      // so it is the path that accumulates them fastest. A browser has no key
      // and omits the field; an older hub ignores it either way.
      let chave = null;
      try{
        if(window.WAVR_MOBILE && typeof window.WAVR_MOBILE.deviceKey === "function"){
          chave = await window.WAVR_MOBILE.deviceKey();
        }
      }catch{ chave = null; }        // never fail a pairing over this
      const corpo = { code, device_name: companionDeviceName() };
      if(chave) corpo.device_key = chave;
      let r;
      try{
        r = await (window.WAVR_MOBILE
          ? window.WAVR_MOBILE.netFetch(window.WAVR_MOBILE.base+"/api/pair",{method:"POST",
              headers:{"Content-Type":"application/json"},
              body:JSON.stringify(corpo)})
          : fetch(location.origin+"/api/pair",{method:"POST",
              headers:{"Content-Type":"application/json"},
              body:JSON.stringify(corpo)}));
      }catch{ msg.className="cpair-msg err"; msg.textContent=WavrT("connection failed"); return; }
      if(!r.ok){
        msg.className="cpair-msg err";
        msg.textContent = (r.status===400||r.status===401||r.status===403||r.status===404)
          ? WavrT("invalid or expired code") : WavrT("pairing failed ({status})", {status: r.status});
        return;
      }
      let tok=null; try{ tok=(await r.json()).token; }catch{}
      if(!tok){ msg.className="cpair-msg err"; msg.textContent=WavrT("invalid server response"); return; }
      companionSetToken(tok);
      msg.className="cpair-msg ok"; msg.textContent=WavrT("paired — loading…");
      try{ location.reload(); }catch{}   // reload boots straight into the token-authed viewer
    };
    return;
  }
  // --- VIEWER MODE: reveal the read-only "disconnect" control (network panel is rendered by renderNetwork) ---
  // Fix F6: relocated into the Rede tab panel (was a bare topbar button) -- the wrapping
  // #companionConn tile carries the hidden state now; the button itself has none of its own.
  const exit = document.getElementById("companionExit");
  const exitTile = document.getElementById("companionConn");
  if(exit){
    if(exitTile) exitTile.hidden = false;
    exit.onclick = ()=>{ companionClearToken(); try{ location.reload(); }catch{} };
  }
  // Stage-1 tab shell: #network now lives permanently inside the "Rede" tab panel, so the
  // old single-scroll-page reorder (moving it to after <main>) no longer applies and was
  // removed — renderNetwork()'s own MODE gating and hidden-toggling are untouched.
}

const MODE = (window.WAVR_MOBILE && window.WAVR_MOBILE.mode)
           || (isLoopbackHost(location.hostname) ? "live"
              : isPrivateLanHost(location.hostname) ? "companion"
              : "simulated");
// Provider construction is factored into __wavrMakeProvider() so the mobile BOOT GATE can defer
// it until after WAVR_MOBILE.ready (the Keystore-backed token is not readable synchronously at
// parse time). Absent the hook, it is built here immediately, exactly as before.
let provider;
function __wavrMakeProvider(){
  provider = MODE==="live" ? WebSocketProvider()
           : MODE==="companion" ? (companionToken() ? CompanionProvider(companionToken()) : NullProvider())
           : SimulatorProvider();
  return provider;
}
if(!window.WAVR_MOBILE) __wavrMakeProvider();
// Fix F7: the standalone topbar #mode span is gone — this exact "source: real home/viewer/
// demo" text is already shown in Settings → Sobre (sobreModeEl, see the gear-panel init
// below); pairing state itself is still fully conveyed by the existing pairing-mode body
// class (hides nav chrome), which never depended on this element.
// Content-first reorder (item 2): the config panels below the divider are all live-only
// (or companion-only for #network), so only reveal the divider itself in live mode.
if(MODE==="live"){
} else if(MODE==="simulated"){
  // First-run onboarding (item 5) → P4 fix 4: a compact "demo" header pill instead of the
  // full-width strip. Still unmistakably a demo — the pill is always visible; tapping it
  // opens the same honest explanation in a popover (privacy-chip pattern).
  const dp = document.getElementById("demoPill");
  const di = document.getElementById("demoIntro");
  if(dp && di){
    dp.hidden = false;
    dp.addEventListener("click", (e)=>{
      e.stopPropagation();
      const opening = di.hidden;
      di.hidden = !opening;
      dp.setAttribute("aria-expanded", opening ? "true" : "false");
    });
    document.addEventListener("click", (e)=>{
      if(!di.hidden && !di.contains(e.target)){ di.hidden = true; dp.setAttribute("aria-expanded", "false"); }
    });
    document.addEventListener("keydown", (e)=>{
      if(e.key === "Escape" && !di.hidden){ di.hidden = true; dp.setAttribute("aria-expanded", "false"); }
    });
  }
}
// Visible action feedback (item 4): "✓ done" next to the button/form that triggered a
// successful write, or a plain error message on failure. Shared by cameras/pairing/house-editor.
function actionFeedback(el, ok, msg, okMsg){
  if(!el) return;
  clearTimeout(el._fbTimer);
  el.className = "action-fb " + (ok ? "ok" : "err");
  el.textContent = ok ? (okMsg || WavrT("✓ done")) : (msg || WavrT("failed — try again"));
  if(ok) el._fbTimer = setTimeout(()=>{ el.textContent = ""; el.className = "action-fb"; }, 2000);
}

// Two failures that look identical to a person and are not the same thing.
//
//   * The fetch THREW (`r` is undefined) — the Core was not reachable at all.
//     Nothing was attempted, so nothing changed, and pressing the button again
//     right now does exactly the same. Check the Core is running.
//   * The Core ANSWERED and refused (`r` exists, `r.ok` false). Something was
//     attempted and declined. If it said why, that reason IS the message — it
//     is always better than anything written here.
//
// Call sites had been collapsing both into one noun ("couldn't save"), which
// tells somebody that a thing did not happen and nothing about what to do
// next. `verb` is the site's own words for what was attempted, so the sentence
// stays specific: failureText(r, "save the Space name").
async function failureText(r, verb){
  if(!r){
    return WavrT("Couldn't {verb} — Wavr could not reach the Core, so nothing "
         + "changed. Check it is still running, then try again.", {verb: verb});
  }
  try{
    const j = await r.json();
    // `detail` is FastAPI's field; `error` is what a couple of the older
    // handlers return. A site reading only one of them threw the other away,
    // so the Core HAD said why and the person was told "update failed".
    if(j && (j.detail || j.error)) return String(j.detail || j.error);
  }catch(e){}
  return WavrT("Couldn't {verb} — the Core refused it ({status}) and "
       + "did not say why. Nothing changed.", {verb: verb, status: r.status});
}

