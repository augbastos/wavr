/* Wavr Mobile shim - the ONLY mobile-specific JS file.
 *
 * Loaded as a plain <script> BEFORE frontend/index.html's inline script (injected into the
 * generated bundle by `npm run sync-frontend`, which copies index.html + vendor + this file into
 * mobile/www/). It:
 *   1. Populates window.WAVR_MOBILE (CONTRACT A) so index.html boots as a companion viewer whose
 *      base URL is the stored central and whose network I/O goes through the WavrNet plugin over a
 *      pinned, out-of-band-verified TLS connection (the Android System WebView cannot validate a
 *      self-signed cert, so ALL fetch/WS is delegated to native).
 *   2. Loads {centralUrl, pinnedFp, token} from Keystore-backed secure storage into synchronous
 *      caches and resolves `ready` - the BOOT GATE index.html awaits before its deferred boot runs.
 *   3. Renders the native pairing/trust UX over the bundled page: setup, out-of-band fingerprint
 *      verify, and the pin-mismatch HARD-FAIL + deliberate re-verify. The 8-digit rotating-code
 *      entry reuses index.html's #companionPair (patched to POST via netFetch).
 *
 * INVARIANTS enforced here: token + pinnedFp live in Keystore only (never localStorage, never a
 * URL, never rendered as a credential, NEVER console.log'd). Local-only: the sole peer is the
 * stored central. No analytics/crash/third-party SDK. No camera in Phase 1.
 */
(function(){
  "use strict";

  // ---------- Capacitor + plugin handles (CONTRACT B) ----------
  var Cap = window.Capacitor || null;
  // Inert on web/dev: if we are not running inside the native Capacitor shell we do NOT install
  // window.WAVR_MOBILE, so index.html keeps its exact loopback/companion/demo web behavior.
  var isNative = !!(Cap && (Cap.isNativePlatform ? Cap.isNativePlatform()
                    : (Cap.getPlatform && Cap.getPlatform() !== "web")));
  if(!isNative) return;

  function plugin(name){
    try{ if(typeof Cap.registerPlugin === "function") return Cap.registerPlugin(name); }catch(_){}
    return (Cap.Plugins && Cap.Plugins[name]) || null;
  }
  // ASSUMPTION (reconcile with capacitor-shell-engineer): the pinned-network plugin registers as
  // "WavrNet" and the Keystore-backed store as "WavrSecureStorage".
  var WavrNet = plugin("WavrNet");
  var SecureStorage = plugin("WavrSecureStorage");
  // Sensor-node plugin (blueprint §2). Native owns the sample+POST loop, the foreground service, and
  // the Bearer token (read straight from Keystore -- it never crosses into JS). JS only start/stop/
  // getStatus/permission-intents + a 'status' event. ABSENT on a viewer-only build -> every call site
  // is null-guarded and the sensor UI degrades to disabled rather than throwing.
  var WavrSensor = plugin("WavrSensor");
  // mDNS/DNS-SD browse for Cores advertising `_wavr._tcp` (capacitor-zeroconf@4.0.0). Registered
  // native plugin name is "ZeroConf" (see node_modules/capacitor-zeroconf/dist/esm/index.js ->
  // registerPlugin('ZeroConf', ...); android/.../ZeroConfPlugin.java -> @CapacitorPlugin(name =
  // "ZeroConf")) -- capitalization matters, it is the bridge lookup key. ABSENT on a build without
  // the native plugin -> discovery is skipped and the user falls to manual entry, never throws.
  var Zeroconf = plugin("ZeroConf");
  function zeroconfAvailable(){ return !!(Zeroconf && typeof Zeroconf.watch === "function"); }

  // ---------- Secure-storage adapter (capacitor-shell-engineer implements the plugin) ----------
  // Expected API (async, EncryptedSharedPreferences/Keystore-backed, DURABLE commit before resolve):
  //   SecureStorage.get({key})           -> { value: string|null }
  //   SecureStorage.set({key, value})    -> {}   MUST persist durably before the promise resolves
  //   SecureStorage.remove({key})        -> {}
  // Durability note: index.html calls location.reload() right after a successful pair (to re-boot
  // into the viewer). Capacitor posts the bridge message for SecureStorage.set synchronously during
  // the call, so the native write reaches the platform before the WebView tears down; the store
  // should still commit() (not apply()) so the value survives the reload. Verify on-device.
  function secureGet(key){
    if(!SecureStorage) return Promise.resolve(null);
    return SecureStorage.get({ key: key }).then(function(r){ return (r && r.value) || null; })
                                          .catch(function(){ return null; });
  }
  // FIX-E3: PROPAGATE rejections (do NOT swallow) so a failed durable Keystore write is visible to
  // callers. The PAIR / RE-PIN paths surface an explicit UI error on rejection instead of silently
  // pretending the pairing was saved; tokenSet attaches its own no-op handler (index.html can't await).
  function secureSet(key, val){
    if(!SecureStorage) return Promise.resolve();
    return SecureStorage.set({ key: key, value: val });
  }
  function secureDel(key){
    if(!SecureStorage) return Promise.resolve();
    return SecureStorage.remove({ key: key });
  }
  var K_URL = "wavr.centralUrl", K_FP = "wavr.pinnedFp", K_TOKEN = "wavr.token";
  // Multi-device admin parity (Pass 1: SIGNAL only). Our own device_id (captured at pair time via
  // onPaired) and last-known role are cached in Keystore so boot reads them SYNCHRONOUSLY (like the
  // token), and detectRole() can find our own row in GET /api/devices. Neither is ever logged.
  var K_DEVICE_ID = "wavr.deviceId", K_ROLE = "wavr.role";
  // wavr.caps: a purely-LOCAL capability declaration {sensor,viewer,admin} chosen at first launch. It
  // NEVER implies a backend capability (the token's granted role does). Read SYNCHRONOUSLY at boot like
  // the token so decideScreen() branches without a flash. wavr.onboarded: a one-time flag so the sensor
  // permission wizard (notifications/battery/autostart) is shown ONCE before the first Start -- required
  // because the 8-digit pair success reloads the page (index.html:1904), so the "pairing -> wizard ->
  // node" sequence crosses a reboot and cannot rely on the pre-token render alone.
  var K_CAPS = "wavr.caps", K_ONBOARDED = "wavr.onboarded";
  // wavr.consent: this device's OWN consent level ("green"|"yellow"|"red"), the control surface for the
  // hub-side consent gate (RED telemetry is dropped server-side; the column is the enforcement). Read
  // SYNCHRONOUSLY at boot like the token so the toggle paints the correct colour with no flash. Never logged.
  var K_CONSENT = "wavr.consent";

  // FIX-E3(b): durable-write helper for the PAIR / RE-PIN paths. Resolves only once every write of the
  // pairing state has committed; rejects if ANY write fails, so the caller surfaces a save error and
  // does NOT proceed with a silently-broken (unpersisted) pairing. The token is written only when
  // provided (first-pair obtains it later via index.html's 8-digit exchange; re-pin keeps the existing
  // token untouched). Never logs base/fp/token.
  function persistPairing(base, fp, token){
    var writes = [ secureSet(K_URL, base), secureSet(K_FP, fp) ];
    if(token != null) writes.push(secureSet(K_TOKEN, token));
    return Promise.all(writes);
  }

  // ---------- Synchronous caches (populated during `ready`) ----------
  var _base = "";      // "https://<ip>:<port>", "" until paired
  var _pinnedFp = null;
  var _token = null;
  var _deviceId = null;   // our own device_id (from the pair response), persisted to Keystore
  var _role = null;       // "central" | "user" | null  (null is treated as viewer everywhere)
  var _caps = null;       // {sensor,viewer,admin} | null   (null = true first launch -> chooser)
  var _onboarded = false; // sensor permission wizard completed at least once
  // Consent level, read from Keystore at boot. ASSUMPTION (surfaced to owner): the hub defaults a freshly
  // paired device to full participation, so an absent/first-launch value defaults to "green"; there is no
  // GET /api/consent yet, so we do NOT reconcile against the hub at boot (a GET reconciliation is a NEXT).
  var _consent = "green";
  // Friendly name of the Core the user picked in showChooseCore() (mDNS `name` field). Informational
  // only -- never persisted, never used for anything security-relevant (the pinned fingerprint is).
  var _pendingCoreName = null;

  // caps helpers. A sensor-ONLY device must never boot the index.html viewer: its sensor-role token
  // 403s every read, so CompanionProvider would self-wipe the token in a reload loop (companionAuthFailed
  // -> reload). tokenGet() hides the token from index.html in that one case so it falls to the inert
  // NullProvider; the native sensor loop reads its OWN Keystore token independently. Combo devices
  // (sensor+viewer) keep the token visible so the dashboard boots exactly as today.
  function capsHasSensor(){ return !!(_caps && _caps.sensor); }
  function capsSensorOnly(){ return !!(_caps && _caps.sensor && !_caps.viewer && !_caps.admin); }
  function parseCaps(raw){
    if(!raw) return null;
    try{
      var c = JSON.parse(raw);
      if(c && typeof c === "object" && (c.sensor || c.viewer || c.admin))
        return { sensor: !!c.sensor, viewer: !!c.viewer, admin: !!c.admin };
    }catch(_){}
    return null;   // empty / malformed -> treat as first launch (show the chooser)
  }

  // ---------- ready gate ----------
  var _resolveReady;
  var ready = new Promise(function(res){ _resolveReady = res; });

  // ================= NET BRIDGE (CONTRACT A: netFetch / netWebSocket) =================
  function headersView(h){
    var lower = {};
    if(h){ for(var k in h){ if(Object.prototype.hasOwnProperty.call(h, k)) lower[String(k).toLowerCase()] = h[k]; } }
    return { get: function(name){ var v = lower[String(name).toLowerCase()]; return (v == null) ? null : v; } };
  }
  // Pinned HTTPS -> a minimal fetch-Response-like. On a pin mismatch WavrNet.request rejects with
  // code "PIN_MISMATCH"; we raise the hard-fail screen AND re-throw so index.html's own try/catch
  // treats it as a failed read (never proceeds with unverified data).
  function netFetch(url, opts){
    opts = opts || {};
    if(!/^https?:\/\//i.test(String(url))){
      return Promise.reject(new Error("wavr: refusing non-absolute URL (base not set)"));
    }
    if(!WavrNet || typeof WavrNet.request !== "function"){
      return Promise.reject(new Error("wavr: WavrNet unavailable"));
    }
    return WavrNet.request({
      url: url, method: opts.method || "GET", headers: opts.headers || {},
      body: (opts.body != null ? opts.body : undefined), pinnedFp: _pinnedFp
    }).then(function(res){
      var body = (res && res.body != null) ? res.body : "";
      var status = (res && typeof res.status === "number") ? res.status : 0;
      return {
        ok: status >= 200 && status < 300,
        status: status,
        headers: headersView(res && res.headers),
        text: function(){ return Promise.resolve(String(body)); },
        json: function(){ return Promise.resolve(JSON.parse(String(body))); }
      };
    }).catch(function(err){
      // FIX-M1: Capacitor surfaces call.reject(msg, code, data) as err.code + err.data.<field>
      // (WavrNetPlugin.kt:271,274,363,364; d.ts:24-27), so presentedFp lives at err.data.presentedFp,
      // not err.presentedFp -- read BOTH so the "now presented" fp on the initial mismatch card is not
      // blank. err.code stays top-level, so the hard-fail still fires regardless. (The wavrNetError
      // EVENT path below is top-level presentedFp and is already correct.) Rejection shape confirmed
      // on-device (test row M-DRILL-B).
      if(err && err.code === "PIN_MISMATCH"){ onPinMismatch(err.presentedFp || (err.data && err.data.presentedFp) || null); }
      throw err;
    });
  }

  // Pinned WSS -> a WebSocket-shaped bridge wired to the plugin's message/close/error events.
  var _socks = {};
  function netWebSocket(url){
    var sock = {
      onmessage: null, onclose: null, onerror: null, __id: null,
      send: function(data){ if(this.__id != null && WavrNet) WavrNet.sendSocket({ socketId: this.__id, data: data }); },
      close: function(){ if(this.__id != null && WavrNet) WavrNet.closeSocket({ socketId: this.__id }); }
    };
    if(!WavrNet || typeof WavrNet.openSocket !== "function"){
      setTimeout(function(){ if(typeof sock.onclose === "function") sock.onclose({ code: 1006, reason: "no bridge" }); }, 0);
      return sock;
    }
    WavrNet.openSocket({ url: url, pinnedFp: _pinnedFp }).then(function(r){
      sock.__id = r.socketId; _socks[r.socketId] = sock;
      detectRole();   // socket (re)connect -> re-check role (fire-and-forget; self-guards + coalesces)
    }).catch(function(err){
      // FIX-M1: Capacitor surfaces call.reject(msg, code, data) as err.code + err.data.<field>
      // (WavrNetPlugin.kt:271,274,363,364; d.ts:24-27), so presentedFp lives at err.data.presentedFp,
      // not err.presentedFp -- read BOTH so the "now presented" fp on the initial mismatch card is not
      // blank. err.code stays top-level, so the hard-fail still fires regardless. (The wavrNetError
      // EVENT path below is top-level presentedFp and is already correct.) Rejection shape confirmed
      // on-device (test row M-DRILL-B).
      if(err && err.code === "PIN_MISMATCH"){ onPinMismatch(err.presentedFp || (err.data && err.data.presentedFp) || null); }
      if(typeof sock.onerror === "function") sock.onerror(err);
      if(typeof sock.onclose === "function") sock.onclose({ code: 1006, reason: "open failed" });
    });
    return sock;
  }
  if(WavrNet && typeof WavrNet.addListener === "function"){
    WavrNet.addListener("wavrNetMessage", function(e){
      var s = _socks[e && e.socketId]; if(s && typeof s.onmessage === "function") s.onmessage({ data: e.data });
    });
    WavrNet.addListener("wavrNetClose", function(e){
      var s = _socks[e && e.socketId];
      if(s){ if(typeof s.onclose === "function") s.onclose({ code: e.code, reason: e.reason }); delete _socks[e.socketId]; }
    });
    WavrNet.addListener("wavrNetError", function(e){
      if(e && e.code === "PIN_MISMATCH"){ onPinMismatch(e.presentedFp || null); }
      var s = _socks[e && e.socketId]; if(s && typeof s.onerror === "function") s.onerror(e);
    });
  }

  // ---------- token cache API (CONTRACT A) ----------
  // FIX-E2: tokenGet MUST NOT throw. index.html reads it synchronously at parse time under mobile, so a
  // throw there would halt boot. Reading a local var cannot throw today, but wrap defensively so any
  // future change can only ever yield null (treated as "not paired") rather than a boot-halting error.
  function tokenGet(){
    try{
      // Sensor-ONLY node: hide the token from index.html so it boots the inert NullProvider (no authed
      // reads that would 403 under sensor-role confinement and trigger a token-wiping reload loop). The
      // native sensor loop still reads its own Keystore token. Viewer/admin/combo/migrated are unaffected.
      if(capsSensorOnly()) return null;
      return _token;
    }catch(_){ return null; }
  }
  // FIX-E3(a): update the in-memory cache SYNCHRONOUSLY first (so tokenGet is correct immediately for
  // index.html's synchronous read), then persist. Returns the durable-write promise so a caller MAY
  // await it; index.html does not, so we also attach a no-op handler to suppress unhandled-rejection
  // noise while still letting an awaiter observe failure. Never logs the token.
  function tokenSet(t){
    var p;
    if(t == null){ _token = null; p = secureDel(K_TOKEN); }   // 401/403 revoke, or explicit disconnect
    else { _token = t; p = secureSet(K_TOKEN, t); }           // cache first, then persist to Keystore
    p.catch(function(){});
    return p;
  }

  // ---------- role / device-id signal (multi-device admin parity, Pass 1: SIGNAL ONLY) ----------
  // Pass 1 populates WAVR_MOBILE.role but flips NO gate: a mobile viewer stays exactly today's
  // viewer. The only observable effect is that a genuine promotion/demotion re-boots once so a
  // future pass can build the page with the right capability set from the synchronous role cache.

  // onPaired(pairResponse): index.html calls this right after a successful POST /api/pair with the
  // parsed {device_id, token}. We persist OUR device_id so a later detectRole() can locate our own
  // row in GET /api/devices. Guards missing fields; NEVER throws back to the caller (it is mid-pair);
  // NEVER logs the id/token.
  function onPaired(pairResponse){
    try{
      var id = pairResponse && pairResponse.device_id;
      if(typeof id === "string" && id){
        _deviceId = id;
        secureSet(K_DEVICE_ID, id).catch(function(){});   // durable; failure is non-fatal to pairing
      }
    }catch(_){}
  }

  // detectRole(): ask the central who we are, fire-and-forget. GET /api/devices (Bearer) -> find our
  // own row by device_id -> read its role. Definitiveness rules:
  //   200 + our row present   -> that row's role ("central" if central, else "user")
  //   200 + our id absent     -> "user"  (we are not an admin-listed device)
  //   403                     -> "user"  (rejected as a peer; role only -- token wipe stays the
  //                                       read/ws path's job via companionAuthFailed, untouched here)
  //   network / other / parse -> UNDEFINED = keep the cached role (NEVER downgrade on a transient blip)
  // A CAPABILITY flip (viewer<->admin) persists the new role then reloads (reuse the existing reload
  // pattern), and only AFTER the durable write resolves so a failed Keystore write can never spin a
  // reload loop (memory is updated first, so a re-trigger sees no change). A null->"user" first-detect
  // is viewer->viewer: it persists the baseline but does NOT reload (keeps the mobile viewer flash-free).
  // Never blocks boot; never throws; never logs id/role/token.
  var _roleInFlight = false;
  function detectRole(){
    if(_roleInFlight) return;                 // coalesce overlapping triggers (boot / reconnect / resume)
    if(!_token || !_base || !WavrNet) return; // nothing to ask without a token+base+bridge
    _roleInFlight = true;
    var next;   // stays UNDEFINED unless we get a DEFINITIVE answer -> undefined means "keep cache"
    netFetch(_base + "/api/devices", { method: "GET", headers: { "Authorization": "Bearer " + _token } })
      .then(function(r){
        if(r && r.status === 403){ next = "user"; return; }
        if(!r || !r.ok) return;                                  // other status -> keep cache
        return r.json().then(function(body){
          var list = (body && body.devices);
          if(!Array.isArray(list)) return;                       // malformed -> keep cache
          var mine = null;
          for(var i = 0; i < list.length; i++){
            if(list[i] && list[i].device_id === _deviceId){ mine = list[i]; break; }
          }
          next = mine ? (mine.role === "central" ? "central" : "user") : "user";
        }, function(){ /* parse failure -> keep cache */ });
      })
      .catch(function(){ /* network / PIN_MISMATCH (hard-fail already raised) / other -> keep cache */ })
      .then(function(){
        _roleInFlight = false;
        if(next === undefined || next === _role) return;         // no definitive change -> no reload
        var wasAdmin = (_role === "central");
        _role = next;                                            // memory-first: blocks a re-trigger loop
        var capFlip = ((next === "central") !== wasAdmin);        // viewer<->admin only; null~user = viewer
        secureSet(K_ROLE, next).then(function(){
          if(capFlip){ try{ location.reload(); }catch(_){} }      // re-boot with the new capability set
        }, function(){ /* durable write failed: do NOT reload (avoids a loop); keep role in memory */ });
      });
  }

  // ---------- CONTRACT A object (installed synchronously, before index.html parses) ----------
  window.WAVR_MOBILE = {
    mode: "companion",
    get base(){ return _base; },        // getter: reflects the address chosen at setup time
    netFetch: netFetch,
    netWebSocket: netWebSocket,
    tokenGet: tokenGet,
    tokenSet: tokenSet,
    onPaired: onPaired,                 // capture our device_id from the /api/pair response
    get role(){ return _role; },        // "central" | "user" | null  (null treated as viewer)
    ready: ready
  };

  // Foreground/resume trigger for detectRole: re-check our role when the app returns to the fore.
  // visibilitychange is the zero-dependency choice (no @capacitor/app); it fires on WebView
  // resume in Chrome/Android. detectRole self-guards on token/base and coalesces, so an extra
  // fire is harmless. FALLBACK (only if on-device proves visibilitychange does NOT fire on native
  // resume): add @capacitor/app's appStateChange. Do NOT add that dependency pre-emptively.
  try{
    document.addEventListener("visibilitychange", function(){
      if(document.visibilityState === "visible") detectRole();
    });
  }catch(_){}

  // ================= PAIRING / TRUST UX =================
  var overlay = null, _screen = null, _styled = false;
  // FIX-H1 re-entrancy latch: true iff a PIN-MISMATCH hard-fail screen (the "certificate changed"
  // card OR the deliberate re-verify card) is the one currently mounted. index.html's CompanionProvider
  // reconnect loop RE-FIRES PIN_MISMATCH on a persistent cert change every ~1.5-2s (ws-ticket POST
  // catch -> setTimeout(...,2000) @index.html:1835; ws.onclose -> setTimeout(...,1500) @1840; neither
  // latches for PIN_MISMATCH -- only 401/403 latches). Without a guard each re-fire re-ran showMismatch
  // -> ensureOverlay -> overlay.textContent="" (~L255), WIPING an in-progress re-verify (the user
  // typing the last-6 got bounced back to the mismatch card every ~2s). The latch makes those re-fires
  // no-ops while the card is up so the deliberate re-verify can actually complete (drill A must SUCCEED).
  // It is UI-churn only: the pin decision is unchanged, still fail-closed, and no "trust anyway" exists.
  // Managed centrally at the two screen choke points -- ensureOverlay() sets it by screen name,
  // hideOverlay() clears it -- so navigating to ANY normal screen (connecting/viewer, incl. the
  // post-re-pin "Reconnecting..." on success) clears it, letting future legitimate mismatches show.
  var _mismatchActive = false;

  function el(tag, cls, text){
    var e = document.createElement(tag);
    if(cls) e.className = cls;
    if(text != null) e.textContent = text;   // textContent only - never innerHTML with cert/user data
    return e;
  }
  function whenDom(fn){
    if(document.body) fn();
    else document.addEventListener("DOMContentLoaded", fn, { once: true });
  }
  function isPort(p){ return /^\d{1,5}$/.test(p) && (+p) >= 1 && (+p) <= 65535; }
  function isHost(h){
    return /^(\d{1,3})(\.\d{1,3}){3}$/.test(h) || /^[a-zA-Z0-9][a-zA-Z0-9.\-]{0,253}$/.test(h);
  }

  function injectStyle(){
    if(_styled) return; _styled = true;
    var css =
      "#wavrm-overlay{position:fixed;inset:0;z-index:2147483000;display:flex;align-items:center;" +
      "justify-content:center;background:var(--bg,#0B0E12);color:var(--text,#EAECEE);" +
      "padding:calc(28px + env(safe-area-inset-top)) calc(20px + env(safe-area-inset-right)) " +
      "calc(28px + env(safe-area-inset-bottom)) calc(20px + env(safe-area-inset-left));" +
      "font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;overflow-y:auto;}" +
      "#wavrm-overlay[hidden]{display:none!important;}" +
      "#wavrm-overlay.danger{background:#160e0e;}" +
      ".wavrm-card{width:100%;max-width:420px;background:var(--surface,#141920);" +
      "border:1px solid var(--line,rgba(255,255,255,.07));border-radius:var(--radius,12px);" +
      "padding:22px;display:flex;flex-direction:column;gap:13px;}" +
      ".wavrm-h{margin:0;font-size:1.15rem;letter-spacing:-0.01em;}" +
      ".wavrm-h.danger{color:var(--danger,#e8726a);}" +
      ".wavrm-sub{margin:0;color:var(--dim,#9AA4AD);font-size:.86rem;}" +
      ".wavrm-warn{margin:0;color:var(--warn,#e8a13a);font-size:.82rem;}" +
      ".wavrm-field{display:flex;flex-direction:column;gap:5px;}" +
      ".wavrm-lab{font-size:.72rem;font-weight:600;text-transform:uppercase;letter-spacing:.06em;" +
      "color:var(--dim,#9AA4AD);}" +
      ".wavrm-input{background:var(--bg,#0B0E12);color:var(--text,#EAECEE);" +
      "border:1px solid var(--line,rgba(255,255,255,.07));border-radius:10px;padding:13px 14px;" +
      "font-size:16px;width:100%;box-sizing:border-box;}" +
      ".wavrm-input:focus{outline:2px solid var(--accent,#3db54a);outline-offset:1px;}" +
      ".wavrm-fp{display:block;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;" +
      "font-size:.82rem;line-height:1.5;word-break:break-all;background:var(--elevated,#1C232C);" +
      "border:1px solid var(--line,rgba(255,255,255,.07));border-radius:8px;padding:10px 12px;" +
      "color:var(--text,#EAECEE);}" +
      ".wavrm-btn{min-height:48px;padding:12px 16px;font-size:.95rem;font-weight:560;text-align:center;" +
      "border-radius:999px;border:1px solid transparent;background:var(--accent,#3db54a);color:#08130a;" +
      "cursor:pointer;}" +
      ".wavrm-btn:disabled{opacity:.5;cursor:default;}" +
      ".wavrm-btn.ghost{background:transparent;color:var(--dim,#9AA4AD);" +
      "border-color:var(--line,rgba(255,255,255,.07));}" +
      ".wavrm-msg{margin:0;font-size:.83rem;color:var(--dim,#9AA4AD);min-height:1.1em;}" +
      ".wavrm-msg.err{color:var(--danger,#e8726a);}" +
      ".wavrm-spin{width:34px;height:34px;border-radius:50%;margin:6px auto;" +
      "border:3px solid var(--line,rgba(255,255,255,.12));border-top-color:var(--accent,#3db54a);" +
      "animation:wavrm-rot .8s linear infinite;}" +
      "@keyframes wavrm-rot{to{transform:rotate(360deg);}}" +
      // capability chooser cards
      ".wavrm-choice{width:100%;text-align:left;background:var(--bg,#0B0E12);color:var(--text,#EAECEE);" +
      "border:1px solid var(--line,rgba(255,255,255,.07));border-radius:12px;padding:14px 16px;" +
      "display:grid;grid-template-columns:24px 1fr;gap:3px 12px;cursor:pointer;}" +
      ".wavrm-choice.primary{border-color:var(--accent,#3db54a);}" +
      ".wavrm-choice.on{outline:2px solid var(--accent,#3db54a);outline-offset:1px;}" +
      ".wavrm-choice-mark{grid-row:1/3;grid-column:1;align-self:start;margin-top:1px;width:24px;height:24px;" +
      "border-radius:7px;border:1px solid var(--line,rgba(255,255,255,.18));color:var(--accent,#3db54a);" +
      "font-weight:700;font-size:.9rem;display:flex;align-items:center;justify-content:center;}" +
      ".wavrm-choice.on .wavrm-choice-mark{border-color:var(--accent,#3db54a);}" +
      ".wavrm-choice-t{grid-column:2;font-weight:600;}" +
      ".wavrm-choice-s{grid-column:2;color:var(--dim,#9AA4AD);font-size:.82rem;}" +
      // node screen status row
      ".wavrm-node-stat{display:flex;align-items:center;justify-content:space-between;gap:10px;" +
      "background:var(--elevated,#1C232C);border:1px solid var(--line,rgba(255,255,255,.07));" +
      "border-radius:10px;padding:12px 14px;}" +
      ".wavrm-node-state{font-weight:700;letter-spacing:.06em;font-size:.8rem;color:var(--dim,#9AA4AD);}" +
      ".wavrm-node-state.streaming{color:var(--accent,#3db54a);}" +
      ".wavrm-node-state.error{color:var(--danger,#e8726a);}" +
      ".wavrm-node-counts{color:var(--dim,#9AA4AD);font-size:.82rem;font-variant-numeric:tabular-nums;}" +
      // sensor pill injected into index.html's header .status-pills
      "#wavrm-pill{cursor:pointer;}" +
      // consent toggle: the control's OWN border+dot = the consent level colour (green/yellow/red).
      // border !important overrides .tpill's default so the level colour is unmistakable on the control.
      ".wavrm-consent{cursor:pointer;position:relative;overflow:hidden;touch-action:none;" +
      "border:1px solid var(--consent-color,rgba(255,255,255,.14))!important;}" +
      ".wavrm-consent.pending{opacity:.8;}" +
      // 2s hold progress fill (withdrawal affordance): danger-tinted, animates 0->100% while held.
      ".wavrm-consent .wavrm-cprog{position:absolute;left:0;bottom:0;height:2px;width:0;" +
      "background:var(--danger,#e8726a);pointer-events:none;transition:width .15s ease;}" +
      ".wavrm-consent.holding .wavrm-cprog{width:100%;transition:width 2s linear;}";
    var s = el("style"); s.id = "wavrm-style"; s.textContent = css;
    (document.head || document.documentElement).appendChild(s);
  }

  function ensureOverlay(screen){
    injectStyle();
    if(!overlay){
      overlay = el("div"); overlay.id = "wavrm-overlay";
      overlay.setAttribute("role", "dialog"); overlay.setAttribute("aria-modal", "true");
      (document.body || document.documentElement).appendChild(overlay);
    }
    overlay.className = ""; overlay.hidden = false; overlay.textContent = "";
    _screen = screen || null;
    // FIX-H1: the latch reflects whether the screen now being mounted is a hard-fail one. Mounting any
    // other screen (setup/verify/connecting) here clears it -- that is how a successful re-pin, which
    // routes through showConnecting("Reconnecting..."), releases the guard for future mismatches.
    _mismatchActive = (screen === "mismatch" || screen === "reverify");
    var card = el("div", "wavrm-card");
    overlay.appendChild(card);
    return card;
  }
  function hideOverlay(){ if(overlay){ overlay.hidden = true; overlay.textContent = ""; } _screen = null; _mismatchActive = false; }
  function fpBlock(text){ return el("code", "wavrm-fp", text == null ? "" : text); }

  // FIX-E1 fingerprint helpers. The Phase-1 out-of-band anchor is the LAST 6 hex chars of the PROBED
  // cert fingerprint (24 bits), typed by the user from the hub's own screen. This defeats the passive
  // "I compared" rubber-stamp: a MitM's probed cert has a different fingerprint, so the hub's real
  // last-6 will NOT match the attacker's cert last-6 and the pin button cannot enable.
  // Phase-2 upgrade (NOT here -- needs the camera): replace this typed last-6 with a QR full-hash
  // machine-compare (scan the hub's QR, compare the entire SHA-256 on-device).
  function normHex(s){ return String(s == null ? "" : s).replace(/[^0-9a-fA-F]/g, "").toUpperCase(); }
  function last6(fp){ var h = normHex(fp); return h.length >= 6 ? h.slice(-6) : ""; }
  function fpDisplay(fp){ return String(fp == null ? "" : fp).toUpperCase(); }   // full, uppercase colon-hex

  // Reveal index.html's own 8-digit code form (#companionPair). initCompanion() also does this on
  // the post-ready boot; this is idempotent belt-and-suspenders against boot ordering.
  function revealCodeEntry(){
    var pair = document.getElementById("companionPair"); if(pair) pair.hidden = false;
    var main = document.querySelector("main"); if(main) main.hidden = true;
    var radar = document.getElementById("radarWrap"); if(radar) radar.hidden = true;
    var hero = document.getElementById("homeHero"); if(hero) hero.hidden = true;
    try{ document.body.classList.add("pairing-mode"); }catch(_){}
  }

  // ----- Screen 0: discovery. Browse `_wavr._tcp` and let the user pick a Core; manual entry
  // (Screen 1 below) is always one tap away and is what an absent/broken plugin falls back to. -----
  var _coreWatchActive = false;
  function showChooseCore(){
    var card = ensureOverlay("chooseCore");
    card.appendChild(el("h2", "wavrm-h", "Find your Wavr hub"));
    card.appendChild(el("p", "wavrm-sub", "Looking for hubs on your Wi-Fi…"));
    var list = el("div", "wavrm-field"); card.appendChild(list);
    var msg = el("p", "wavrm-msg", "");
    var manual = el("button", "wavrm-btn ghost", "Enter address manually"); manual.type = "button";
    manual.onclick = function(){ stopCoreWatch(); showSetup(); };
    var seen = {};
    function addCore(svc){
      var core = WavrLib.parseCoreService(svc); if(!core) return;
      var key = core.host + ":" + core.port; if(seen[key]) return; seen[key] = true;
      var b = el("button", "wavrm-choice primary"); b.type = "button";
      b.appendChild(el("span", "wavrm-choice-mark", ""));
      b.appendChild(el("div", "wavrm-choice-t", core.name));
      b.appendChild(el("div", "wavrm-choice-s", core.host + ":" + core.port));
      b.onclick = function(){
        stopCoreWatch();
        _base = "https://" + core.host + ":" + core.port;
        _pendingCoreName = core.name;
        showVerify();
      };
      list.appendChild(b);
    }
    card.appendChild(manual); card.appendChild(msg);
    if(!zeroconfAvailable()){
      msg.className = "wavrm-msg err";
      msg.textContent = "Automatic discovery isn't available on this device — enter the address.";
      return;
    }
    startCoreWatch(addCore, function(){
      msg.className = "wavrm-msg err";
      msg.textContent = "Couldn't search for hubs on this network — enter the address.";
    });
  }
  // Only "resolved" events carry a usable address (jmdns fires "added" before resolution completes);
  // parseCoreService's own host/port validation is a second line of defence against a half-resolved
  // record slipping through.
  //
  // FAILURE PATH -- `watch` is a CALLBACK-type Capacitor method (@PluginMethod(returnType =
  // RETURN_CALLBACK) in ZeroConfPlugin.java): the promise it returns resolves IMMEDIATELY with a
  // callbackId and is DECOUPLED from later native results, so a native failure (no multicast lock,
  // Wi-Fi off, IOException in watchService) is delivered THROUGH THE CALLBACK, NOT via promise
  // rejection. Per @capacitor/android native-bridge.js returnResult(): on error a callback-type call
  // is invoked as `callback(null, result.error)` (success is `callback(result.data)`). So the failure
  // is read from the callback's OWN arguments: a falsy first arg or a truthy second (error) arg means
  // the watch failed -> surface the visible manual-entry fallback instead of hanging on "Looking…".
  // The promise `.catch` is kept only as belt-and-suspenders for a JS-level rejection (plugin
  // unimplemented / load failure); the synchronous try/catch covers the proxy throwing on the call.
  function startCoreWatch(onFound, onFailure){
    if(_coreWatchActive) return; _coreWatchActive = true;
    function fail(){                       // report a native failure at most once
      if(!_coreWatchActive) return;
      _coreWatchActive = false;
      if(onFailure) onFailure();
    }
    try{
      var p = Zeroconf.watch({ type: "_wavr._tcp.", domain: "local." }, function(res, err){
        // Key the failure off `err` ONLY. ZeroConfPlugin.watch() fires a priming
        // `call.setKeepAlive(true); call.resolve()` with NO data right after scheduling the browse
        // (ZeroConfPlugin.java:126-127) -> the bridge delivers callback(undefined) (res=undefined,
        // err=undefined) on every HEALTHY start. A real native reject always carries a truthy error
        // object (callback(null, error)); the priming resolve never does. So `err` alone is the
        // failure signal -- a `!res` clause here would false-fail every normal session.
        if(err){ fail(); return; }                                // native reject -> callback(null, error)
        if(res && res.action === "resolved" && res.service) onFound(res.service);
      });
      if(p && typeof p.catch === "function") p.catch(function(){ fail(); });
    }catch(_){ fail(); }
  }
  function stopCoreWatch(){
    _coreWatchActive = false;
    if(!Zeroconf) return;
    try{ if(typeof Zeroconf.unwatch === "function") Zeroconf.unwatch({ type: "_wavr._tcp.", domain: "local." }); }catch(_){}
    try{ if(typeof Zeroconf.stop === "function") Zeroconf.stop(); }catch(_){}
  }

  // ----- Screen 1: setup (IP:port). No camera in Phase 1. -----
  function showSetup(){
    var card = ensureOverlay("setup");
    card.appendChild(el("h2", "wavrm-h", "Connect to your Wavr hub"));
    card.appendChild(el("p", "wavrm-sub",
      "Enter the address shown on the hub's own dashboard, under Settings, Pair device."));
    var f1 = el("label", "wavrm-field"); f1.appendChild(el("span", "wavrm-lab", "Hub IP address"));
    var ip = el("input", "wavrm-input"); ip.type = "text"; ip.inputMode = "decimal";
    ip.autocomplete = "off"; ip.placeholder = "192.168.1.50"; ip.setAttribute("aria-label", "hub IP address");
    f1.appendChild(ip); card.appendChild(f1);
    var f2 = el("label", "wavrm-field"); f2.appendChild(el("span", "wavrm-lab", "Port"));
    var port = el("input", "wavrm-input"); port.type = "text"; port.inputMode = "numeric";
    port.value = "8000"; port.setAttribute("aria-label", "port");
    f2.appendChild(port); card.appendChild(f2);
    if(_base){ var m = /^https?:\/\/([^:/]+)(?::(\d+))?/i.exec(_base); if(m){ ip.value = m[1]; if(m[2]) port.value = m[2]; } }
    var msg = el("p", "wavrm-msg", "");
    var btn = el("button", "wavrm-btn", "Continue"); btn.type = "button";
    btn.onclick = function(){
      var host = (ip.value || "").trim(), p = (port.value || "").trim() || "8000";
      if(!isHost(host)){ msg.className = "wavrm-msg err"; msg.textContent = "Enter a valid IP address."; return; }
      if(!isPort(p)){ msg.className = "wavrm-msg err"; msg.textContent = "Enter a valid port (1 to 65535)."; return; }
      _base = "https://" + host + ":" + p;
      showVerify();
    };
    card.appendChild(btn); card.appendChild(msg);
  }

  // ----- Screen 2: verify (out-of-band fingerprint compare). Pins only on an ACTIVE last-6 match. -----
  function showVerify(){
    var card = ensureOverlay("verify");
    card.appendChild(el("h2", "wavrm-h", "Verify the hub's certificate"));
    card.appendChild(el("p", "wavrm-sub",
      "This is the SHA-256 fingerprint of the certificate presented by " + _base + ". It has not been trusted yet."));
    var fpEl = fpBlock("reading…"); card.appendChild(fpEl);
    // FIX-E4: point the user at where the hub actually shows this value -- its own Pair device panel.
    // (App-side copy only; do not reference the web flow's browser security warning.)
    card.appendChild(el("p", "wavrm-sub",
      "On your Wavr hub's own dashboard open Settings, then Pair device. That panel shows the hub's " +
      "certificate fingerprint. Check it matches the value above, character for character."));
    card.appendChild(el("p", "wavrm-warn",
      "If they do not match, stop. Someone may be intercepting your network. Do not continue."));
    // FIX-E1: ACTIVE challenge replaces the passive checkbox. The user must type the hub's real last-6;
    // it is compared to the PROBED cert's own last-6, so a MitM cert cannot be pinned by a rushed user.
    var f = el("label", "wavrm-field");
    f.appendChild(el("span", "wavrm-lab",
      "Type the last 6 characters of the fingerprint shown on your Wavr hub (Settings, Pair device)"));
    var codeIn = el("input", "wavrm-input"); codeIn.type = "text"; codeIn.autocomplete = "off";
    codeIn.spellcheck = false; codeIn.autocapitalize = "characters"; codeIn.maxLength = 12;
    codeIn.placeholder = "3F9A2C"; codeIn.setAttribute("aria-label", "last 6 fingerprint characters");
    f.appendChild(codeIn); card.appendChild(f);
    var pinBtn = el("button", "wavrm-btn", "Pin & continue"); pinBtn.type = "button"; pinBtn.disabled = true;
    var backBtn = el("button", "wavrm-btn ghost", "Back"); backBtn.type = "button";
    var msg = el("p", "wavrm-msg", "");
    var fp = null, expect = "";
    function recompute(){ pinBtn.disabled = !(fp && expect.length === 6 && normHex(codeIn.value) === expect); }
    codeIn.oninput = recompute;
    pinBtn.onclick = function(){
      if(pinBtn.disabled) return;
      if(!fp || expect.length !== 6 || normHex(codeIn.value) !== expect) return;   // defence in depth
      _pinnedFp = fp;
      pinBtn.disabled = true; msg.className = "wavrm-msg"; msg.textContent = "Saving…";
      persistPairing(_base, fp, null).then(function(){
        revealCodeEntry(); hideOverlay();
      }, function(){
        _pinnedFp = null;                                    // durable write failed: do NOT pretend paired
        msg.className = "wavrm-msg err";
        msg.textContent = "Couldn't save the pairing securely. Try again.";
        recompute();
      });
    };
    backBtn.onclick = function(){ showSetup(); };
    card.appendChild(pinBtn); card.appendChild(backBtn); card.appendChild(msg);
    if(!WavrNet || typeof WavrNet.probe !== "function"){
      fpEl.textContent = "(unavailable)"; msg.className = "wavrm-msg err"; msg.textContent = "Native networking is not available.";
      return;
    }
    WavrNet.probe({ url: _base }).then(function(r){
      fp = (r && r.fingerprint) || null;
      expect = last6(fp);
      fpEl.textContent = fp ? fpDisplay(fp) : "(no certificate presented)";   // textContent, never logged
      recompute();
    }).catch(function(){
      fpEl.textContent = "(unreachable)"; msg.className = "wavrm-msg err";
      msg.textContent = "Could not reach " + _base + ". Check the address and that the hub is on.";
    });
  }

  // ----- Connecting / reconnecting -----
  function showConnecting(text){
    var card = ensureOverlay("connecting");
    card.appendChild(el("div", "wavrm-spin", ""));
    card.appendChild(el("p", "wavrm-sub", text || "Connecting to your home…"));
  }

  // ----- PIN-MISMATCH hard-fail. Only exit is a deliberate re-verify. NEVER a one-tap trust. -----
  function onPinMismatch(presentedFp){
    // FIX-H1: while the hard-fail (mismatch OR re-verify) card is already mounted, reconnect-loop
    // re-fires must be NO-OPs -- returning here avoids ensureOverlay's textContent wipe that would
    // bounce a user mid-re-verify and erase the last-6 they are typing. The FIRST mismatch still
    // shows: the latch is false during connecting/viewer (we deliberately do NOT string-match
    // _screen==="connecting", which would swallow a mismatch that arrives inside the 1.2s connect).
    if(_mismatchActive) return;
    whenDom(function(){ showMismatch(presentedFp); });
  }
  function showMismatch(presentedFp){
    var card = ensureOverlay("mismatch"); overlay.className = "danger";
    card.appendChild(el("h2", "wavrm-h danger", "Certificate changed"));
    card.appendChild(el("p", "wavrm-sub",
      "The certificate presented by your hub no longer matches the one you verified. This can mean the " +
      "certificate was rotated, or that someone is intercepting your connection. Wavr stopped to keep you safe."));
    card.appendChild(el("span", "wavrm-lab", "Fingerprint you pinned"));
    card.appendChild(fpBlock(fpDisplay(_pinnedFp)));
    card.appendChild(el("span", "wavrm-lab", "Fingerprint now presented"));
    card.appendChild(fpBlock(fpDisplay(presentedFp)));
    var btn = el("button", "wavrm-btn", "Re-verify the certificate"); btn.type = "button";
    btn.onclick = function(){ showReVerify(); };
    card.appendChild(btn);
    // Deliberately NO "trust anyway" / "proceed" control.
  }

  // ----- Deliberate re-verify: re-probe, show OLD vs NEW, require the ACTIVE last-6 challenge again. -----
  function showReVerify(){
    var card = ensureOverlay("reverify"); overlay.className = "danger";
    card.appendChild(el("h2", "wavrm-h", "Re-verify the certificate"));
    card.appendChild(el("p", "wavrm-sub",
      "Only continue if you can confirm the NEW fingerprint out of band, on your Wavr hub's own " +
      "dashboard (Settings, Pair device). Compare every character."));
    card.appendChild(el("span", "wavrm-lab", "Old (pinned)"));
    card.appendChild(fpBlock(fpDisplay(_pinnedFp)));
    card.appendChild(el("span", "wavrm-lab", "New (presented now)"));
    var newEl = fpBlock("reading…"); card.appendChild(newEl);
    // FIX-E1: same ACTIVE challenge as first pairing. The typed last-6 is compared to the NEW probed
    // cert's last-6, so a MitM presenting its own cert cannot be re-pinned by a rushed user.
    var f = el("label", "wavrm-field");
    f.appendChild(el("span", "wavrm-lab",
      "Type the last 6 characters of the new fingerprint shown on your Wavr hub (Settings, Pair device)"));
    var codeIn = el("input", "wavrm-input"); codeIn.type = "text"; codeIn.autocomplete = "off";
    codeIn.spellcheck = false; codeIn.autocapitalize = "characters"; codeIn.maxLength = 12;
    codeIn.placeholder = "3F9A2C"; codeIn.setAttribute("aria-label", "last 6 fingerprint characters");
    f.appendChild(codeIn); card.appendChild(f);
    var repin = el("button", "wavrm-btn", "Re-pin and reconnect"); repin.type = "button"; repin.disabled = true;
    var cancel = el("button", "wavrm-btn ghost", "Cancel"); cancel.type = "button";
    var msg = el("p", "wavrm-msg", "");
    var newFp = null, expect = "";
    function recompute(){ repin.disabled = !(newFp && expect.length === 6 && normHex(codeIn.value) === expect); }
    codeIn.oninput = recompute;
    repin.onclick = function(){
      if(repin.disabled) return;
      if(!newFp || expect.length !== 6 || normHex(codeIn.value) !== expect) return;   // defence in depth
      var prev = _pinnedFp; _pinnedFp = newFp;
      repin.disabled = true; msg.className = "wavrm-msg"; msg.textContent = "Saving…";
      persistPairing(_base, newFp, null).then(function(){
        showConnecting("Reconnecting…"); try{ location.reload(); }catch(_){}
      }, function(){
        _pinnedFp = prev;                                    // durable write failed: keep the trusted pin
        msg.className = "wavrm-msg err";
        msg.textContent = "Couldn't save the pairing securely. Try again.";
        recompute();
      });
    };
    cancel.onclick = function(){ showMismatch(newFp); };
    card.appendChild(repin); card.appendChild(cancel); card.appendChild(msg);
    if(!WavrNet || typeof WavrNet.probe !== "function"){ newEl.textContent = "(unavailable)"; return; }
    WavrNet.probe({ url: _base }).then(function(r){
      newFp = (r && r.fingerprint) || null;
      expect = last6(newFp);
      newEl.textContent = newFp ? fpDisplay(newFp) : "(no certificate presented)";
      recompute();
    }).catch(function(){
      newEl.textContent = "(unreachable)"; msg.className = "wavrm-msg err"; msg.textContent = "Could not reach the hub.";
    });
  }

  // ================= SENSOR-NODE UX (blueprint §3; shim overlay chrome only, ZERO index.html edits) ====
  // wavr.caps is LOCAL only: choosing "sensor" here never grants a backend capability -- the token's
  // role does. Nothing below ever logs the token, caps, or role. All heavy logic (sampling, POST, FGS)
  // lives in the native WavrSensor plugin; JS stays thin -- start/stop + render status.

  function persistCaps(caps){ _caps = caps; return secureSet(K_CAPS, JSON.stringify(caps)); }
  function markOnboarded(){ _onboarded = true; secureSet(K_ONBOARDED, "1").catch(function(){}); }

  // A best-effort, LOCAL default node name (editable on the node screen). Derived from the WebView UA
  // model token -- never a network call. The native plugin may override with Build.MODEL. Falls back
  // to "phone". (NOT VERIFIED: exact model formatting on-device; the value is user-editable anyway.)
  function defaultNodeName(){
    try{
      var m = /Android[^;]*;\s*([^;)]+?)\s*(?:Build\/|\))/.exec(navigator.userAgent || "");
      var name = ((m && m[1]) || "").trim().toLowerCase().replace(/\s+/g, "-").replace(/[^a-z0-9.\-]/g, "");
      return name || "phone";
    }catch(_){ return "phone"; }
  }

  // ----- Sensor plugin bridge: start/stop + live status (STREAMING/IDLE/sent/err) -----
  var _sensorReady = false, _lastStatus = null, _nodeUi = null, _pillUi = null, _statusTimer = null;
  var _sensorAuthFailedOnce = false;
  function sensorAvailable(){ return !!(WavrSensor && typeof WavrSensor.start === "function"); }
  function callSensor(fn, arg){
    if(!(WavrSensor && typeof WavrSensor[fn] === "function")) return Promise.resolve(null);
    try{ return Promise.resolve(WavrSensor[fn](arg)).catch(function(){ return null; }); }
    catch(_){ return Promise.resolve(null); }
  }

  // Mirror companionAuthFailed (index.html:1815): a 401/403 on the pinned telemetry POST -> stop the
  // service, wipe the token (cache + Keystore), reload -> the reboot lands on caps.sensor && !token ->
  // pairing. Latched so a burst of ERROR events fires it once. Never logs the token.
  function sensorAuthFailed(){
    if(_sensorAuthFailedOnce) return; _sensorAuthFailedOnce = true;
    try{ if(WavrSensor && typeof WavrSensor.stop === "function") WavrSensor.stop(); }catch(_){}
    tokenSet(null);
    try{ location.reload(); }catch(_){}
  }

  // Fail-closed classification against the FROZEN WavrSensor contract (wavr-sensor.d.ts): the 'status'
  // event / getStatus() snapshot is { running, state:'STREAMING'|'IDLE'|'ERROR', sent, err, lastError?,
  // presentedFp? }. lastError is a MACHINE CODE -> exact string equality only, no regex/heuristics:
  //   TERMINAL  (state==='ERROR'): PIN_MISMATCH | AUTH_REVOKED | NOT_PAIRED | NO_TLS | FGS_TIMEOUT |
  //                                START_FAILED
  //   TRANSIENT (streaming continues, err++):  NETWORK | HTTP_<code>  (e.g. HTTP_404, HTTP_429)
  // Only two codes drive control flow here. EVERY other code -- incl. unknown FUTURE ones -- is a
  // non-terminal ERROR: surfaced by renderSensor(), token + pin KEPT, Start may retry. Fail-closed but
  // never fail-destructive.
  function lastErr(ev){ return (ev && ev.lastError) || ""; }
  // Hard-fail "certificate changed" screen -> deliberate re-verify ONLY (same UX as WavrNet's
  // PIN_MISMATCH). Fires solely on the terminal PIN_MISMATCH; never auto-recovers, never a silent re-pin.
  function isPinMismatch(ev){ return !!ev && ev.state === "ERROR" && lastErr(ev) === "PIN_MISMATCH"; }
  // Token wipe -> pairing (mirrors companionAuthFailed): the terminal AUTH_REVOKED (native has ALREADY
  // wiped the token), plus a defensive HTTP_401/HTTP_403 should a build surface the raw HTTP code. A
  // transient NETWORK / HTTP_5xx / FGS_TIMEOUT is availability, NOT auth -> it must NEVER wipe the token.
  function isAuthFail(ev){
    var c = lastErr(ev);
    return c === "AUTH_REVOKED" || c === "HTTP_401" || c === "HTTP_403";
  }
  function handleStatus(ev){
    ev = ev || {}; _lastStatus = ev;
    // presentedFp is a TOP-LEVEL field of WavrSensorStatus (addListener delivers the status object
    // directly -- unlike a WavrNet.request rejection, there is no Capacitor err.data wrapper here).
    if(isPinMismatch(ev)){ onPinMismatch(ev.presentedFp || null); }
    else if(isAuthFail(ev)){ sensorAuthFailed(); }
    renderSensor();
  }
  function ensureSensor(){
    if(_sensorReady || !sensorAvailable()) return;
    _sensorReady = true;
    try{ if(typeof WavrSensor.addListener === "function") WavrSensor.addListener("status", handleStatus); }catch(_){}
    // Low-frequency, visible-only getStatus poll so counters recover even if a push event is missed.
    _statusTimer = setInterval(function(){
      if(document.visibilityState !== "visible" || typeof WavrSensor.getStatus !== "function") return;
      WavrSensor.getStatus().then(function(s){ if(s){ _lastStatus = s; renderSensor(); } }).catch(function(){});
    }, 3000);
  }
  function pollStatusOnce(){
    if(!(sensorAvailable() && typeof WavrSensor.getStatus === "function")) return;
    WavrSensor.getStatus().then(function(s){ if(s){ _lastStatus = s; renderSensor(); } }).catch(function(){});
  }
  function startSensor(name){
    if(!sensorAvailable()) return; ensureSensor();
    callSensor("start", { name: name || defaultNodeName() }).then(function(s){ if(s){ _lastStatus = s; renderSensor(); } });
  }
  function stopSensor(){
    if(!sensorAvailable()) return;
    callSensor("stop").then(function(s){ if(s){ _lastStatus = s; renderSensor(); } });
  }
  function isRunning(){ return !!(_lastStatus && (_lastStatus.running || _lastStatus.state === "STREAMING")); }
  function stateLabel(){
    if(_lastStatus && (_lastStatus.state === "STREAMING" || _lastStatus.running)) return "streaming";
    if(_lastStatus && _lastStatus.state === "ERROR") return "error";
    return "idle";
  }
  function renderSensor(){
    var running = isRunning(), lab = stateLabel();
    var sent = (_lastStatus && _lastStatus.sent) || 0, err = (_lastStatus && _lastStatus.err) || 0;
    if(_nodeUi){
      _nodeUi.state.textContent = lab.toUpperCase();
      _nodeUi.state.className = "wavrm-node-state " + lab;
      _nodeUi.sent.textContent = String(sent);
      _nodeUi.err.textContent = String(err);
      _nodeUi.btn.textContent = running ? "Stop" : "Start";
      _nodeUi.btn.className = running ? "wavrm-btn ghost" : "wavrm-btn";
    }
    if(_pillUi){
      _pillUi.dot.style.background = running ? "var(--accent,#3db54a)"
        : (lab === "error" ? "var(--danger,#e8726a)" : "var(--dim,#9AA4AD)");
      _pillUi.txt.textContent = running ? ("streaming · " + sent) : (lab === "error" ? "sensor error" : "contribute");
    }
  }
  // Gate any Start behind the one-time onboarding wizard ("gated before the first Start").
  function ensureOnboardedThen(proceed){
    if(_onboarded || !capsHasSensor()){ proceed(); return; }
    showWizard(proceed);
  }

  // ----- Capability chooser (true first launch). Sensing is the BASE capability -> presented first
  // and emphasized. >=1 selection required (Continue disabled until then); Continue persists caps then
  // routes into the UNCHANGED pinned setup flow. -----
  function showChooser(){
    var card = ensureOverlay("chooser");
    card.appendChild(el("h2", "wavrm-h", "What will this device do?"));
    card.appendChild(el("p", "wavrm-sub", "Choose one or more. You'll connect this device to your hub next."));
    var sel = { sensor: false, viewer: false, admin: false };
    var opts = [
      { key: "sensor", primary: true,  h: "Contribute presence",  s: "Use this device's sensors to help your home know who's in. This is the core Wavr node." },
      { key: "viewer", primary: false, h: "Watch the home",        s: "See live presence and rooms on this device." },
      { key: "admin",  primary: false, h: "Manage the home",       s: "Edit rooms and settings. Needs an admin code from the hub." }
    ];
    var cont = el("button", "wavrm-btn", "Continue"); cont.type = "button"; cont.disabled = true;
    var msg = el("p", "wavrm-msg", "");
    function refresh(){ cont.disabled = !(sel.sensor || sel.viewer || sel.admin); }
    opts.forEach(function(o){
      var b = el("button", "wavrm-choice" + (o.primary ? " primary" : "")); b.type = "button";
      b.setAttribute("aria-pressed", "false");
      var mark = el("span", "wavrm-choice-mark", "");
      b.appendChild(mark);
      b.appendChild(el("div", "wavrm-choice-t", o.h));
      b.appendChild(el("div", "wavrm-choice-s", o.s));
      b.onclick = function(){
        sel[o.key] = !sel[o.key];
        b.classList.toggle("on", sel[o.key]);
        b.setAttribute("aria-pressed", sel[o.key] ? "true" : "false");
        mark.textContent = sel[o.key] ? "✓" : "";
        refresh();
      };
      card.appendChild(b);
    });
    cont.onclick = function(){
      if(cont.disabled) return;
      cont.disabled = true; msg.className = "wavrm-msg"; msg.textContent = "";
      persistCaps({ sensor: sel.sensor, viewer: sel.viewer, admin: sel.admin }).then(function(){
        showChooseCore();
      }, function(){
        cont.disabled = false; msg.className = "wavrm-msg err";
        msg.textContent = "Couldn't save your choice securely. Try again.";
      });
    };
    card.appendChild(cont); card.appendChild(msg);
  }

  // ----- Onboarding wizard (sensor devices only, once, before the first Start). Each step fires a
  // native intent through WavrSensor; a denied/unavailable intent DEGRADES gracefully -> it NEVER
  // blocks Start. OEM detection (Samsung/Xiaomi) is native (Build.MANUFACTURER). onDone re-enters the
  // caller's flow (node screen, or start for the combo pill). -----
  function showWizard(onDone){
    var steps = [
      { h: "Allow notifications", s: "Wavr keeps a quiet ongoing notification while this device contributes, so Android lets it run in the background.",
        cta: "Allow", run: function(){ return callSensor("requestPermissions", {}); } },
      { h: "Keep Wavr running", s: "Let Wavr run without battery limits, so presence keeps flowing when the screen is off.",
        cta: "Open battery settings", run: function(){ return callSensor("openBatteryExemption"); } },
      { h: "Stop the system killing it", s: "Some phones (Samsung, Xiaomi) close background apps. If prompted, allow auto-start for Wavr. You can skip this.",
        cta: "Open auto-start settings", run: function(){ return callSensor("openOemAutostart"); } },
      { h: "Wi-Fi presence (optional)", s: "Optionally use Wi-Fi signal to improve presence. This needs location permission and never leaves your home. You can skip.",
        cta: "Enable Wi-Fi presence", run: function(){ return callSensor("requestPermissions", { wifiIdentity: true }); } }
    ];
    var i = 0;
    function render(){
      var st = steps[i];
      var card = ensureOverlay("wizard");
      card.appendChild(el("p", "wavrm-sub", "Set up " + (i + 1) + " of " + steps.length));
      card.appendChild(el("h2", "wavrm-h", st.h));
      card.appendChild(el("p", "wavrm-sub", st.s));
      var act = el("button", "wavrm-btn", st.cta); act.type = "button";
      act.onclick = function(){ try{ st.run(); }catch(_){} };   // fire intent; best-effort, never blocks
      var next = el("button", "wavrm-btn ghost", i < steps.length - 1 ? "Next" : "Done"); next.type = "button";
      next.onclick = function(){
        i++;
        if(i < steps.length){ render(); }
        else { markOnboarded(); if(typeof onDone === "function") onDone(); }
      };
      card.appendChild(act); card.appendChild(next);
    }
    render();
  }

  // ----- Node screen (sensor-only). Name prefilled + editable, big Start/Stop, live STREAMING/IDLE +
  // sent/err from status events + getStatus poll. -----
  function showNode(){
    ensureSensor();
    var card = ensureOverlay("node");
    card.appendChild(el("h2", "wavrm-h", "Sensor node"));
    card.appendChild(el("p", "wavrm-sub",
      "This device contributes presence to your home. Nothing leaves your local network."));
    var f = el("label", "wavrm-field"); f.appendChild(el("span", "wavrm-lab", "Device name"));
    var nameIn = el("input", "wavrm-input"); nameIn.type = "text"; nameIn.autocomplete = "off";
    nameIn.value = defaultNodeName(); nameIn.setAttribute("aria-label", "device name");
    f.appendChild(nameIn); card.appendChild(f);
    var stat = el("div", "wavrm-node-stat");
    var stateEl = el("span", "wavrm-node-state idle", "IDLE");
    var counts = el("span", "wavrm-node-counts", "");
    var sentEl = el("b", null, "0"), errEl = el("b", null, "0");
    counts.appendChild(document.createTextNode("sent ")); counts.appendChild(sentEl);
    counts.appendChild(document.createTextNode("  ·  err ")); counts.appendChild(errEl);
    stat.appendChild(stateEl); stat.appendChild(counts); card.appendChild(stat);
    var btn = el("button", "wavrm-btn", "Start"); btn.type = "button";
    _nodeUi = { state: stateEl, sent: sentEl, err: errEl, btn: btn };
    if(!sensorAvailable()){
      btn.disabled = true;
      card.appendChild(btn);
      card.appendChild(el("p", "wavrm-msg err", "Sensor service is not available on this build."));
    } else {
      btn.onclick = function(){
        if(isRunning()) stopSensor();
        else startSensor((nameIn.value || "").trim() || defaultNodeName());
      };
      card.appendChild(btn);
    }
    // Consent toggle on the dedicated node screen too, so a sensor-ONLY device shows it (item 4: every
    // paired device regardless of role). Same control, registered in _consentUis; the boot cache gives it
    // the right colour immediately. postConsent uses the internal _token (present even though tokenGet
    // hides it from index.html on a sensor-only node).
    card.appendChild(el("span", "wavrm-lab", "Your consent on this device"));
    var crow = el("div", "wavrm-node-stat");
    crow.appendChild(el("span", "wavrm-node-counts", "Tap to reduce · hold to withdraw"));
    crow.appendChild(makeConsentControl().btn);
    card.appendChild(crow);
    renderConsent();
    pollStatusOnce();   // reflect an already-running FGS on relaunch (one-tap resume)
    renderSensor();
  }

  // ----- Sensor pill (sensor+viewer). A compact "this device" toggle injected into index.html's
  // existing header pill row (.status-pills) as shim overlay chrome -- NOT an index.html edit. -----
  function injectSensorPill(){
    ensureSensor();
    if(document.getElementById("wavrm-pill")) return;
    var row = document.querySelector(".status-pills"); if(!row) return;
    var b = el("button", "tpill"); b.id = "wavrm-pill"; b.type = "button";
    b.title = "This device — contribute presence";
    var dot = el("i", "p-dot"); var txt = el("span", "p-txt", "contribute");
    b.appendChild(dot); b.appendChild(txt);
    b.onclick = function(){
      if(isRunning()) stopSensor();
      else ensureOnboardedThen(function(){ hideOverlay(); startSensor(defaultNodeName()); });
    };
    row.appendChild(b);
    _pillUi = { dot: dot, txt: txt, btn: b };
    pollStatusOnce();
    renderSensor();
  }

  // ================= CONSENT TOGGLE UX (shim overlay chrome; ZERO index.html edits) =====================
  // A single control whose COLOUR is the consent level. It is the CONTROL surface; the hub's consent
  // column is the ENFORCEMENT (RED telemetry is dropped server-side). Injected into index.html's header
  // .status-pills EXACTLY like the sensor pill (no-op if the row is absent), and also onto the dedicated
  // sensor-node screen so it shows on EVERY paired device regardless of role/caps (participation !=
  // permission: a device can be "admin · red"). Never logs the token or the level.

  // Honest, non-overclaiming copy (blueprint item 5). GREEN does NOT claim network reach over the phone.
  var CONSENT = {
    green:  { next: "yellow", color: "var(--accent,#3db54a)", label: "Full participation",
              tip: "Full participation — Wavr uses this phone's presence and names it at home." },
    yellow: { next: "red",    color: "var(--warn,#e8a13a)",   label: "Limited",
              tip: "Limited — present but anonymous; minimal data." },
    // TAP wraps red -> green: a single visible control must be able to RE-ENGAGE, and re-granting one's
    // OWN consent is legitimate. The deliberate 2s-hold is the easy-withdrawal path, so an accidental
    // single tap can only ever step the level (never hold-to-off), and every tap changes colour+label
    // and POSTs -- so a mistaken tap is instantly visible and reversible with another tap.
    red:    { next: "green",  color: "var(--danger,#e8726a)", label: "Off",
              tip: "Off — you've left Wavr; this device contributes nothing." }
  };
  function normConsent(v){ return (v === "green" || v === "yellow" || v === "red") ? v : "green"; }

  var _consentUis = [];        // every rendered consent control (header pill + node-screen copy)
  var _consentPending = false; // true while the CURRENT level is NOT yet confirmed by a 2xx from the hub
  var _consentGen = 0;         // bumped on every change so stale retries/responses are ignored
  var _consentRetry = null;
  function clearConsentRetry(){ if(_consentRetry){ clearTimeout(_consentRetry); _consentRetry = null; } }

  // POST /api/consent {level} over the PINNED transport (netFetch -> WavrNet pinned HTTPS), Bearer our
  // OWN token. Resolves TRUE only on a 2xx (hub accepted). CRITICAL self-DoS guard: a 422 (invalid) /
  // 429 (rate-limited) / 5xx / even a 401/403 here resolves FALSE (-> retry), and a network error REJECTS
  // (-> retry). It NEVER wipes the token or forces re-pair: that stays the job of companionAuthFailed /
  // the sensor auth-fail handler on THEIR OWN read/ws/telemetry paths, bound to 401/403 only. A failed
  // consent POST surfaces a retry, never a re-pair. Never logs the token or the level.
  function postConsent(level){
    if(!_token || !_base) return Promise.reject(new Error("not paired"));
    return netFetch(_base + "/api/consent", {
      method: "POST",
      headers: { "Authorization": "Bearer " + _token, "Content-Type": "application/json" },
      body: JSON.stringify({ level: level })
    }).then(function(r){ return !!(r && r.ok); });   // 200 {device_id, level} -> true; else -> false (retry, NEVER wipe)
  }

  // In-memory + UI + durable Keystore; on RED best-effort stop the native sensor loop. Returns the
  // Keystore write promise (a durable-write failure is non-fatal here -- the hub column is the enforcement).
  function applyConsentLocal(level){
    _consent = level;
    if(level === "red"){ try{ stopSensor(); }catch(_){} }   // guarded no-op on a viewer with no sensor
    renderConsent();
    return secureSet(K_CONSENT, level).catch(function(){});
  }

  function scheduleConsentRetry(gen){
    clearConsentRetry();
    _consentRetry = setTimeout(function(){
      _consentRetry = null;
      if(gen !== _consentGen) return;                          // a newer change owns the state now
      postConsent(_consent).then(function(ok){
        if(gen !== _consentGen) return;
        if(ok){ _consentPending = false; renderConsent(); }    // hub confirmed on retry
        else { scheduleConsentRetry(gen); }
      }, function(){ if(gen === _consentGen) scheduleConsentRetry(gen); });
    }, 5000);
  }

  // The single mutation entry point for both gestures.
  function changeConsent(target){
    target = normConsent(target);
    var gen = ++_consentGen; clearConsentRetry();
    if(target === "red"){
      // WITHDRAWAL: POST FIRST -- the hub gate is the guarantee against a replaying token. Show red as
      // UNCONFIRMED until the hub accepts; never present a confirmed "withdrawn" the hub hasn't accepted.
      _consent = "red"; _consentPending = true; renderConsent();
      postConsent("red").then(function(ok){
        if(gen !== _consentGen) return;
        if(ok){ _consentPending = false; applyConsentLocal("red"); }              // confirmed: persist + stop loop
        else { applyConsentLocal("red"); _consentPending = true; renderConsent(); scheduleConsentRetry(gen); }
      }, function(){
        if(gen !== _consentGen) return;
        // offline: enact locally anyway (persist red + stop loop so the phone stops contributing NOW),
        // but keep it UNCONFIRMED and retry until the hub accepts the withdrawal.
        applyConsentLocal("red"); _consentPending = true; renderConsent(); scheduleConsentRetry(gen);
      });
    } else {
      // green / yellow: optimistic local apply, then POST to record + confirm; retry on failure. Erring
      // toward the phone showing MORE participation than the hub has granted is the privacy-safe direction
      // (it never under-states data flow). Keystore persisted immediately so the boot colour is durable.
      _consentPending = true; applyConsentLocal(target);
      postConsent(target).then(function(ok){
        if(gen !== _consentGen) return;
        if(ok){ _consentPending = false; renderConsent(); }
        else { scheduleConsentRetry(gen); }
      }, function(){ if(gen === _consentGen) scheduleConsentRetry(gen); });
    }
  }

  function renderConsent(){
    // Prune controls whose DOM node was removed (e.g. a rebuilt overlay card) so we never keep updating
    // detached nodes.
    if(document.body){ _consentUis = _consentUis.filter(function(u){ return document.body.contains(u.btn); }); }
    var meta = CONSENT[_consent] || CONSENT.green;
    var label = meta.label, tip = meta.tip;
    if(_consentPending){
      if(_consent === "red"){ label = "Off · confirming"; tip = "Withdrawal not yet confirmed by the hub — retrying."; }
      else { label = meta.label + " · confirming"; tip = meta.tip + " (not yet confirmed by the hub)"; }
    }
    for(var i = 0; i < _consentUis.length; i++){
      var u = _consentUis[i];
      u.btn.style.setProperty("--consent-color", meta.color);
      u.dot.style.background = meta.color;
      u.txt.textContent = label;
      u.btn.title = tip;
      u.btn.setAttribute("aria-label", "Consent: " + label + ". Tap to reduce, hold two seconds to withdraw.");
      u.btn.classList.toggle("pending", !!_consentPending);
    }
  }

  // TAP = decrease one step (green->yellow->red, wraps red->green). HOLD 2s = jump straight to RED (GDPR
  // "withdraw as easy as give") with a visible 2s progress fill. Pointer events only (no click) so a tap
  // and a hold never double-fire.
  function attachConsentGestures(btn){
    var holdTimer = null, held = false;
    function clearHold(){ if(holdTimer){ clearTimeout(holdTimer); holdTimer = null; } btn.classList.remove("holding"); }
    btn.addEventListener("pointerdown", function(ev){
      held = false; btn.classList.add("holding");             // CSS fills the progress bar to 100% over 2s
      holdTimer = setTimeout(function(){
        held = true; holdTimer = null; btn.classList.remove("holding");
        changeConsent("red");                                  // withdrawal shortcut
      }, 2000);
      try{ btn.setPointerCapture(ev.pointerId); }catch(_){}
    });
    btn.addEventListener("pointerup", function(){
      var wasHeld = held; clearHold();
      if(!wasHeld) changeConsent(CONSENT[_consent].next);      // TAP = one step down (wraps at red)
    });
    btn.addEventListener("pointercancel", clearHold);
    btn.addEventListener("lostpointercapture", clearHold);
  }

  function makeConsentControl(){
    injectStyle();
    var b = el("button", "tpill wavrm-consent"); b.type = "button";
    var dot = el("i", "p-dot"); var txt = el("span", "p-txt", "");
    b.appendChild(dot); b.appendChild(txt); b.appendChild(el("i", "wavrm-cprog"));
    attachConsentGestures(b);
    var ui = { btn: b, dot: dot, txt: txt };
    _consentUis.push(ui);
    return ui;
  }

  // Header placement (viewer/admin/central/combo): inject into .status-pills exactly like the sensor
  // pill. No-op if the host row is absent. Only once paired.
  function injectConsentPill(){
    if(!_token) return;
    var row = document.querySelector(".status-pills"); if(!row) return;
    if(row.querySelector(".wavrm-consent")) return;            // idempotent
    row.appendChild(makeConsentControl().btn);
    renderConsent();
  }

  // ----- Boot: load caches, resolve the gate, then decide which screen to show -----
  function decideScreen(){
    whenDom(function(){
      injectStyle();
      if(_token){
        // Already paired -> never show the chooser. Clear the pairing chrome class index.html sets at
        // parse time (token cache was empty then) and mask the boot flash.
        try{ document.body.classList.remove("pairing-mode"); }catch(_){}
        if(capsSensorOnly()){
          // Dedicated sensor node: no dashboard (its token is hidden from index.html -> NullProvider).
          // Show the permission wizard once before the first Start, then the node screen.
          if(!_onboarded) showWizard(showNode); else showNode();
        } else if(capsHasSensor()){
          // sensor + viewer/admin: the dashboard is PRIMARY (boots exactly as the viewer path below);
          // inject the compact "this device" sensor pill into the header chrome. The wizard is deferred
          // to the pill's first Start (ensureOnboardedThen) so viewing is never blocked.
          showConnecting("Connecting to your home…");
          setTimeout(function(){ if(_screen === "connecting") hideOverlay(); }, 1200);
          injectSensorPill();
          injectConsentPill();   // consent toggle in the header chrome (combo device)
        } else {
          // viewer / admin / migrated (token, no caps): EXACTLY today's boot, byte-for-byte UNCHANGED,
          // plus the consent toggle injected into the header chrome (participation != permission -- a
          // viewer/admin/central device shows it too).
          showConnecting("Connecting to your home…");
          setTimeout(function(){ if(_screen === "connecting") hideOverlay(); }, 1200);
          injectConsentPill();
        }
      } else if(!_caps && !_base){
        // True first launch (nothing chosen AND nothing entered) -> capability chooser before setup.
        showChooser();
      } else if(_base && _pinnedFp){
        // Verified central but no token (resume after verify, a 401/403 revoke, or a pre-caps mid-pair
        // upgrade): jump straight to the 8-digit code entry. No setup/verify re-prompt.
        revealCodeEntry(); hideOverlay();
      } else {
        // caps chosen but not yet verified, or verify incomplete: an already-entered address resumes
        // straight to verify; otherwise offer discovery first (manual entry stays one tap away).
        if(_base) showVerify(); else showChooseCore();
      }
    });
  }

  Promise.all([secureGet(K_URL), secureGet(K_FP), secureGet(K_TOKEN),
               secureGet(K_DEVICE_ID), secureGet(K_ROLE),
               secureGet(K_CAPS), secureGet(K_ONBOARDED), secureGet(K_CONSENT)]).then(function(v){
    _base = v[0] || ""; _pinnedFp = v[1] || null; _token = v[2] || null;
    _deviceId = v[3] || null; _role = v[4] || null;   // role read synchronously at boot, like the token
    _caps = parseCaps(v[5]); _onboarded = (v[6] === "1");   // caps + onboarded read synchronously too
    _consent = normConsent(v[7]);   // consent level read synchronously -> the toggle's boot colour is correct
  }).catch(function(){}).then(function(){
    _resolveReady();   // caches populated: index.html's deferred boot may run now
    // detectRole drives the DASHBOARD admin surface only; a sensor-ONLY node has no dashboard and its
    // token 403s /api/devices, so skip it there. Viewer/admin/combo/migrated behave exactly as before.
    if(_token && !capsSensorOnly()) detectRole();
    decideScreen();
  });
})();
