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
  // Task 6: this device's presence label (shown at home) + the friendly name of the paired Core. Both
  // are user-facing display strings, never security-relevant (the pinned fingerprint is), never logged.
  // Read at boot so the status chip paints without a flash.
  var K_PRESENCE_LABEL = "wavr.presenceLabel", K_CORE_NAME = "wavr.coreName";

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
  // Task 6 caches: this device's presence label + the paired Core's friendly name, loaded from Keystore
  // at boot. Display-only; never logged. Empty until set (label captured at pair time; coreName from the
  // mDNS pick or the base URL fallback).
  var _presenceLabel = "", _coreName = "";

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
  // CONTRACT: `ready` resolves after the Keystore read SETTLES (success OR failure) and NEVER waits on
  // discovery/network — discovery/connection state is expressed by shim OVERLAYS on top of the inert
  // NullProvider page, never by hanging the gate. resolveReady() is idempotent (a _readyDone latch), and
  // a defensive 4s timer force-resolves it so that even if the SecureStorage plugin itself WEDGES (the
  // Promise.all at the bottom never settling) index.html's boot is never blocked on a broken plugin.
  var _resolveReady, _readyDone = false;
  var ready = new Promise(function(res){ _resolveReady = res; });
  function resolveReady(){ if(_readyDone) return; _readyDone = true; try{ _resolveReady(); }catch(_){} }
  setTimeout(resolveReady, 4000);

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
    // Task 4: while detached (RED / "left Wavr"), REFUSE the ws-ticket streaming path. index.html's
    // CompanionProvider caches the bearer in its own closure, so even though tokenGet() returns null it
    // keeps POSTing /api/ws-ticket (Bearer) every ~2s -- the token would keep leaving the device and the
    // Core would keep seeing liveness. Reject as a NETWORK error (not 401/403), so its reconnect loop just
    // retries on its 2s pace and NEVER wipes the token. Does NOT touch /api/consent (re-enter/withdraw) or
    // /api/presence/register-companion (Task 5 withdrawal DELETE) -- only the ws-ticket streaming path.
    if(!_attached && /\/api\/ws-ticket(\?|$)/.test(String(url))){
      return Promise.reject(new Error("wavr: detached"));   // RED/out: no ws-ticket, no token leaves the device
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
    // Task 4 connect lever: while detached (RED / "left Wavr") REFUSE to open a socket. Return a dead
    // WebSocket-shaped stub (mirrors the "no bridge" dead-sock pattern below) whose onclose fires async
    // with a clean 1000 so index.html's reconnect loop keeps receiving dead sockets and stays DOWN --
    // no live socket exists while the user has left. Re-enter (green/yellow) flips _attached and reloads.
    if(!_attached){
      var dead = { onmessage: null, onclose: null, onerror: null, __id: null, send: function(){}, close: function(){} };
      setTimeout(function(){ if(typeof dead.onclose === "function") dead.onclose({ code: 1000, reason: "detached" }); }, 0);
      return dead;
    }
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
      // Task 4 RACE guard: the user may have gone RED during this async open window. closeLiveSockets()
      // already ran on an EMPTY _socks, so registering this now-LIVE socket would leave a live orphan
      // streaming while "You've left Wavr" is shown. Fail-closed: close it natively and never register.
      if(!_attached){
        try{ if(WavrNet && typeof WavrNet.closeSocket === "function") WavrNet.closeSocket({ socketId: r.socketId }); }catch(_){}
        return;
      }
      sock.__id = r.socketId; _socks[r.socketId] = sock;
      detectRole();        // socket (re)connect -> re-check role (fire-and-forget; self-guards + coalesces)
      reassertPresence();  // Task 5: socket (re)connect -> re-assert presence (self-guards on level/token)
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
      // Task 4: RED/out -> hide the token so a detached reload lands on index.html's inert NullProvider
      // (no authed reads, no WS). The native sensor loop still reads its own Keystore token independently.
      if(!_attached) return null;
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
      if(document.visibilityState === "visible"){ detectRole(); reassertPresence(); }   // Task 5: re-assert presence on resume
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

  // Approve-on-Core pairing poll state. Every screen mount (ensureOverlay) calls stopPairPoll(), so
  // navigating anywhere INVALIDATES an in-flight approval poll via the generation bump -- no orphan
  // timers, no stale terminal transitions. The request_id and the minted token stay in closures only:
  // never module-global, never rendered, never localStorage, never console.log'd.
  var _pairGen = 0, _pairTimer = null;
  function stopPairPoll(){ _pairGen++; if(_pairTimer){ clearTimeout(_pairTimer); _pairTimer = null; } }

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
      // FIX-B2: block the native Android WebView text-selection/callout that fired on a long-press and
      // stole the pointer BEFORE the 2s GDPR-withdrawal hold timer could complete (label highlighted +
      // Copy/Share menu appeared). Without this the hold gesture was unreachable on-device.
      "user-select:none;-webkit-user-select:none;-webkit-touch-callout:none;" +
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
    stopPairPoll();   // any screen switch cancels an in-flight approve-on-Core poll (waiting-screen re-arms after)
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
    var sub = el("p", "wavrm-sub", "Looking for hubs on your Wi-Fi…"); card.appendChild(sub);
    var spin = el("div", "wavrm-spin", ""); card.appendChild(spin);
    var list = el("div", "wavrm-field"); card.appendChild(list);

    // Manual entry is always a LAST-RESORT secondary, never a co-equal primary.
    function appendManual(label){
      var m = el("button", "wavrm-btn ghost", label || "Enter address manually"); m.type = "button";
      m.onclick = function(){ stopCoreWatch(); showSetup(); };
      card.appendChild(m);
    }
    // Pick a Core -> straight to the out-of-band fingerprint verify (the SHA-256 is shown there after a
    // probe; mDNS TXT carries NO fingerprint so we never print an unverified fp on a discovery card).
    function connectTo(core){
      stopCoreWatch();
      _base = "https://" + core.host + ":" + core.port;
      _pendingCoreName = core.name;
      showVerify();
    }
    function coreRow(core){
      var b = el("button", "wavrm-choice primary"); b.type = "button";
      b.appendChild(el("span", "wavrm-choice-mark", ""));
      b.appendChild(el("div", "wavrm-choice-t", core.name));
      b.appendChild(el("div", "wavrm-choice-s", core.host + ":" + core.port));
      b.onclick = function(){ connectTo(core); };
      return b;
    }

    if(!zeroconfAvailable()){
      try{ spin.remove(); }catch(_){}
      sub.textContent = "Automatic discovery isn't available on this device.";
      appendManual("Enter address manually");
      return;
    }

    var found = [], seen = {}, decideTimer = null, noHubTimer = null, settled = false;

    // EXACTLY ONE hub -> a prominent 1-tap hero row. MULTIPLE -> the tappable list. Rendered ONCE after
    // a short debounce, then the watch is stopped so no late arrival can duplicate the UI.
    function decide(){
      if(settled || !found.length) return;
      settled = true; stopCoreWatch();
      clearTimeout(noHubTimer);
      try{ spin.remove(); }catch(_){}
      if(found.length === 1){
        sub.textContent = "Found your Wavr hub.";
        list.appendChild(coreRow(found[0]));
        appendManual("Enter a different address");
      } else {
        sub.textContent = "Choose your Wavr hub.";
        found.forEach(function(core){ list.appendChild(coreRow(core)); });
        appendManual("Enter address manually");
      }
    }
    function onFound(svc){
      if(settled) return;
      var core = WavrLib.parseCoreService(svc); if(!core) return;
      var key = core.host + ":" + core.port; if(seen[key]) return; seen[key] = true;
      found.push(core);
      clearTimeout(noHubTimer);           // at least one hub -> cancel the terminal no-hub timeout
      clearTimeout(decideTimer);
      decideTimer = setTimeout(decide, 1500);   // debounce ~1.5s after the latest hit, then render
    }

    // Terminal "no hub found yet" state -> NEVER an infinite "Looking…". Offer Search again + manual.
    noHubTimer = setTimeout(function(){
      if(settled || found.length) return;
      settled = true; stopCoreWatch();
      try{ spin.remove(); }catch(_){}
      sub.textContent = "";
      card.appendChild(el("p", "wavrm-sub",
        "We couldn't find a Wavr hub on this Wi-Fi yet. Make sure your hub is on and this phone is on " +
        "the same network."));
      var again = el("button", "wavrm-btn", "Search again"); again.type = "button";
      again.onclick = function(){ showChooseCore(); };
      card.appendChild(again);
      appendManual("Enter address manually");
    }, 7000);

    startCoreWatch(onFound, function(){
      clearTimeout(noHubTimer); clearTimeout(decideTimer);
      if(settled) return; settled = true;
      try{ spin.remove(); }catch(_){}
      sub.textContent = "Couldn't search for hubs on this network.";
      appendManual("Enter address manually");
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
    // Task 6: capture this device's presence label at pair time (shown as "home" presence). REQUIRED
    // per F-EMPTYLABEL: the Core 400s an empty register-companion, so pairing with a blank label leaves
    // auto-presence permanently inert. Display-only, never a credential, never logged. Persisted with
    // the pairing on a successful pin.
    var lf = el("label", "wavrm-field");
    lf.appendChild(el("span", "wavrm-lab", "Your name on this device (shown as your presence at home)"));
    var labelIn = el("input", "wavrm-input"); labelIn.type = "text"; labelIn.autocomplete = "off";
    labelIn.maxLength = 48;   // FIX-C3: cap the display label (textContent + JSON.stringify already injection-safe)
    labelIn.placeholder = "e.g., Augusto"; labelIn.setAttribute("aria-label", "your name on this device");
    lf.appendChild(labelIn); card.appendChild(lf);
    var pinBtn = el("button", "wavrm-btn", "Pin & continue"); pinBtn.type = "button"; pinBtn.disabled = true;
    var backBtn = el("button", "wavrm-btn ghost", "Back"); backBtn.type = "button";
    var msg = el("p", "wavrm-msg", "");
    var fp = null, expect = "";
    function labelOk(){ return (labelIn.value || "").trim().length > 0; }   // F-EMPTYLABEL: name required
    function recompute(){ pinBtn.disabled = !(fp && expect.length === 6 && normHex(codeIn.value) === expect && labelOk()); }
    codeIn.oninput = recompute;
    labelIn.oninput = recompute;
    pinBtn.onclick = function(){
      if(pinBtn.disabled) return;
      if(!fp || expect.length !== 6 || normHex(codeIn.value) !== expect || !labelOk()) return;   // defence in depth
      _pinnedFp = fp;
      _presenceLabel = (labelIn.value || "").trim();          // Task 6: label captured at pair
      _coreName = _pendingCoreName || _base;                  // Task 6: friendly Core name (mDNS pick or base)
      pinBtn.disabled = true; msg.className = "wavrm-msg"; msg.textContent = "Saving…";
      Promise.all([ persistPairing(_base, fp, null),
                    secureSet(K_PRESENCE_LABEL, _presenceLabel),
                    secureSet(K_CORE_NAME, _coreName) ]).then(function(){
        // Gate A done (cert pinned via the out-of-band last-6). Gate B (authorization) is now the
        // approve-on-Core flow -- the operator taps Approve on the hub instead of the user typing a code.
        // The 8-digit code path stays one tap away inside showRequestPairing() as a fallback.
        showRequestPairing();
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

  // ================= APPROVE-ON-CORE PAIRING (Gate B: authorization) =================
  // Reached AFTER showVerify() has pinned the hub's certificate (Gate A: the out-of-band last-6 compare).
  // This step only changes how the token is DELIVERED: instead of the user typing an 8-digit code, the
  // hub's operator taps Approve on the hub's own screen and the phone picks up the minted token by polling.
  //   POST /api/pair-request  -> opens a PENDING record, mints NOTHING (unauth, in-subnet, over the pinned
  //                              transport so a mid-flow cert swap hard-fails on PIN_MISMATCH).
  //   POST /api/pair-request/status -> pending | approved{device_id,token} | denied | expired.
  // The token arrives ONLY on the already-pinned channel, so a MitM that failed Gate A can never read it.
  // The manual 8-digit /api/pair path stays reachable here as a fallback. request_id/token live in closures
  // only, never rendered, never logged. NEVER hangs: bounded by the request TTL, with clean deny/timeout.

  // ----- Screen 3: ask the hub to approve this device (PRIMARY; replaces the 8-digit entry) -----
  function showRequestPairing(){
    var card = ensureOverlay("requestPairing");   // ensureOverlay() cancels any prior poll
    card.appendChild(el("h2", "wavrm-h", "Ask your hub to let this device in"));
    card.appendChild(el("p", "wavrm-sub",
      "The hub's owner will see this request on the hub's own screen and tap Approve. No code to type."));
    var f = el("label", "wavrm-field");
    f.appendChild(el("span", "wavrm-lab", "Your name on this device (shown as your presence at home)"));
    var nameIn = el("input", "wavrm-input"); nameIn.type = "text"; nameIn.autocomplete = "off";
    nameIn.maxLength = 48;   // display label only; textContent + JSON.stringify keep it injection-safe
    nameIn.placeholder = "e.g., Augusto"; nameIn.value = _presenceLabel || "";
    nameIn.setAttribute("aria-label", "your name on this device");
    f.appendChild(nameIn); card.appendChild(f);
    var msg = el("p", "wavrm-msg", "");
    var askBtn = el("button", "wavrm-btn", "Ask to connect"); askBtn.type = "button";
    askBtn.onclick = function(){
      var name = (nameIn.value || "").trim();
      if(!name){ msg.className = "wavrm-msg err"; msg.textContent = "Enter your name on this device."; return; }
      if(!WavrNet || typeof WavrNet.request !== "function"){
        msg.className = "wavrm-msg err"; msg.textContent = "Native networking is not available."; return;
      }
      askBtn.disabled = true; msg.className = "wavrm-msg"; msg.textContent = "Contacting your hub…";
      _presenceLabel = name;
      secureSet(K_PRESENCE_LABEL, name).catch(function(){});   // best-effort; display-only, not a credential
      // POST over the PINNED transport (netFetch uses _pinnedFp set by showVerify). A cert swap since the
      // pin rejects with PIN_MISMATCH -> netFetch raises the hard-fail card and re-throws (handled below).
      netFetch(_base + "/api/pair-request", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ requester_name: name, platform: "Android" })
      }).then(function(r){
        if(!r || !r.ok) throw new Error("pair-request status " + (r ? r.status : "?"));
        return r.json();
      }).then(function(body){
        var reqId = body && body.request_id;                       // 192-bit id, kept in the closure only
        var compareCode = body && body.compare_code;               // per-request numeric-compare value, closure only
        var pollMs = (body && +body.poll_after_ms) || 1500;
        if(!reqId) throw new Error("no request_id");
        // NB: body.cert_fingerprint is DELIBERATELY IGNORED as a trust anchor (F1) -- over a MitM'd channel
        // the attacker controls that body. The pin anchor is the probed cert already verified at showVerify.
        // compare_code is likewise NOT a trust anchor and NOT a credential: it's the number the operator
        // eyeballs against the hub's screen to pick THIS request. It's authenticated by the PINNED channel
        // (a MitM that failed Gate A can't read/alter it), so it composes with the TOFU pin.
        showWaitingApproval(reqId, pollMs, name, compareCode);
      }).catch(function(err){
        if(err && err.code === "PIN_MISMATCH") return;             // hard-fail card already raised by netFetch
        askBtn.disabled = false; msg.className = "wavrm-msg err";
        msg.textContent = "Couldn't reach " + _base + ". Check the hub is on and try again.";
      });
    };
    card.appendChild(askBtn);
    var fb = el("button", "wavrm-btn ghost", "Enter an 8-digit code instead"); fb.type = "button";
    fb.onclick = function(){ revealCodeEntry(); hideOverlay(); };   // fallback: index.html's #companionPair
    card.appendChild(fb);
    var back = el("button", "wavrm-btn ghost", "Back"); back.type = "button";
    back.onclick = function(){ showChooseCore(); };
    card.appendChild(back);
    card.appendChild(msg);
  }

  // ----- Screen 4: waiting for the hub operator to approve. Shows the pinned fingerprint for a 1-time
  // eyeball against the hub's screen, polls status until a clean terminal state. NEVER hangs. -----
  function showWaitingApproval(requestId, pollMs, name, compareCode){
    var card = ensureOverlay("waitingApproval");   // cancels the prior poll; we re-arm a fresh gen below
    var gen = ++_pairGen;
    var started = Date.now();
    var TTL_MS = 180000;   // mirrors backend REQUEST_TTL (180s); client-side hard stop so we never hang
    card.appendChild(el("div", "wavrm-spin", ""));
    card.appendChild(el("h2", "wavrm-h", "Approve this device on your Wavr hub"));
    card.appendChild(el("p", "wavrm-sub",
      "On your Wavr hub's screen you'll see a request from “" + name + "”. Check the number below " +
      "matches the one shown on the hub, then tap Approve there."));
    // PRIMARY match target: the per-request number the operator compares to pick THIS request out of any
    // racing ones. Numeric string -> fpBlock uses textContent (XSS-safe). Never logged, never persisted.
    card.appendChild(el("span", "wavrm-lab", "Confirmation number — check it matches your Wavr hub's screen"));
    card.appendChild(fpBlock(compareCode || "——————"));
    // SECONDARY transport-MitM check: the pinned/probed cert fingerprint (the hub shows the same value).
    card.appendChild(el("span", "wavrm-lab", "This device's certificate fingerprint"));
    card.appendChild(fpBlock(fpDisplay(_pinnedFp)));   // the pinned/probed cert; the hub shows the same value
    card.appendChild(el("p", "wavrm-warn",
      "If the hub shows a different number or fingerprint, someone may be intercepting your network. Tap Deny on the hub."));
    var msg = el("p", "wavrm-msg", "Waiting for approval…");
    card.appendChild(msg);
    var fb = el("button", "wavrm-btn ghost", "Enter an 8-digit code instead"); fb.type = "button";
    fb.onclick = function(){ revealCodeEntry(); hideOverlay(); };
    card.appendChild(fb);
    var cancel = el("button", "wavrm-btn ghost", "Cancel"); cancel.type = "button";
    cancel.onclick = function(){ showRequestPairing(); };
    card.appendChild(cancel);

    function onApproved(body){
      stopPairPoll();   // no further polls; commit exactly once
      var token = body && body.token, deviceId = body && body.device_id;
      if(!token){ msg.className = "wavrm-msg err"; msg.textContent = "The hub approved but sent no token. Try again."; return; }
      msg.className = "wavrm-msg"; msg.textContent = "Approved. Saving…";
      // Persist base+fp+token atomically (fp already pinned at showVerify; the re-write is idempotent). The
      // token is never rendered/logged. onPaired captures our device_id. A durable-write FAILURE surfaces an
      // error and does NOT pretend paired (mirrors showVerify / showReVerify).
      persistPairing(_base, _pinnedFp, token).then(function(){
        _token = token;                                     // sync cache; the reload re-reads it from Keystore
        try{ onPaired({ device_id: deviceId }); }catch(_){}
        showConnecting("Connecting to " + (_coreName || "your home") + "…");
        try{ location.reload(); }catch(_){}                 // reboot straight into the dashboard viewer
      }, function(){
        msg.className = "wavrm-msg err";
        msg.textContent = "Couldn't save the pairing securely. Try again.";   // Cancel -> Ask to connect retries
      });
    }
    function handle(res){
      if(gen !== _pairGen) return;                          // superseded by a newer screen/flow
      res.json().then(function(body){
        if(gen !== _pairGen) return;
        var status = body && body.status;
        if(status === "approved"){ onApproved(body); return; }
        if(status === "denied"){ showDeclined(); return; }
        if(status === "expired"){ showTimedOut(false); return; }
        scheduleNext();                                     // pending / unknown -> keep polling within the TTL
      }, function(){ scheduleNext(); });                    // parse failure -> transient, keep polling
    }
    function netErr(err){
      if(gen !== _pairGen) return;
      if(err && err.code === "PIN_MISMATCH"){ stopPairPoll(); return; }   // hard-fail already shown; stop polling
      scheduleNext();                                       // transient network -> keep trying until the TTL
    }
    function poll(){
      if(gen !== _pairGen) return;
      if(Date.now() - started > TTL_MS){ showTimedOut(true); return; }
      netFetch(_base + "/api/pair-request/status", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request_id: requestId })
      }).then(handle, netErr);
    }
    function scheduleNext(){
      if(gen !== _pairGen) return;
      if(Date.now() - started > TTL_MS){ showTimedOut(true); return; }
      _pairTimer = setTimeout(poll, pollMs);
    }
    scheduleNext();
  }

  // ----- Screen 5: the hub declined (operator tapped Deny). Never hangs -- always an actionable exit. -----
  function showDeclined(){
    var card = ensureOverlay("declined");
    card.appendChild(el("h2", "wavrm-h", "Your hub declined this device"));
    card.appendChild(el("p", "wavrm-sub",
      "The request was denied on the hub. If that wasn't expected, check you asked the right person, then " +
      "try again."));
    var again = el("button", "wavrm-btn", "Ask again"); again.type = "button";
    again.onclick = function(){ showRequestPairing(); };
    card.appendChild(again);
    var back = el("button", "wavrm-btn ghost", "Back"); back.type = "button";
    back.onclick = function(){ showChooseCore(); };
    card.appendChild(back);
  }

  // ----- Screen 6: timed out (no approval within the TTL, or the hub stopped answering). -----
  function showTimedOut(networkFailed){
    var card = ensureOverlay("timedOut");
    card.appendChild(el("h2", "wavrm-h", "The request timed out"));
    card.appendChild(el("p", "wavrm-sub", networkFailed
      ? "We couldn't reach your hub while waiting. Check it's on and on the same Wi-Fi, then try again."
      : "No one approved this device in time (about three minutes). You can ask again."));
    var again = el("button", "wavrm-btn", "Ask again"); again.type = "button";
    again.onclick = function(){ showRequestPairing(); };
    card.appendChild(again);
    var fb = el("button", "wavrm-btn ghost", "Enter an 8-digit code instead"); fb.type = "button";
    fb.onclick = function(){ revealCodeEntry(); hideOverlay(); };
    card.appendChild(fb);
    var back = el("button", "wavrm-btn ghost", "Back"); back.type = "button";
    back.onclick = function(){ showChooseCore(); };
    card.appendChild(back);
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

  // ================= SILENT RECONNECT of a remembered hub (mobile auto-connect) =================
  // Replaces the old blind "show Connecting… for 1200ms then hideOverlay unconditionally" for a PAIRED
  // viewer. The overlay stays up until a base is CONFIRMED reachable, so index.html's empty default house
  // is NEVER shown as a dead end while CompanionProvider silently retries a dead address.
  //   connectPinned(): probe the STORED base first.
  //     * reachable + cert byte-identical to the pinned fp -> hand off to CompanionProvider (silent, today).
  //     * reachable + DIFFERENT cert -> hard-fail (onPinMismatch). Never silent, never "trust anyway".
  //     * unreachable (network / no cert) -> the hub likely MOVED (DHCP) -> rediscoverPinned().
  //   rediscoverPinned(): browse _wavr._tcp, probe each candidate, and reconnect ONLY to a FULL-fp match
  //     (same out-of-band-verified cert at a new IP: changing only the address is NOT a trust decision, so
  //     it is legitimately silent). Bounded by a candidate cap + timeout to avoid a probe storm.
  // Never logs base/fp. A non-matching candidate is simply "not our hub" (we keep looking) — mDNS names
  // are unauthenticated/spoofable, so a same-name-different-cert node is NOT treated as an attack on our
  // pin (that would let anyone advertising our Core name DoS us into the scary mismatch screen); only the
  // STORED base presenting a changed cert is a genuine interception signal, and that hard-fails above.
  var _reconnecting = false;
  function connectPinned(onConnected){
    if(!WavrNet || typeof WavrNet.probe !== "function"){ onConnected(); return; }   // no probe -> today's blind handoff
    showConnecting("Connecting to " + (_coreName || "your home") + "…");
    WavrNet.probe({ url: _base }).then(function(r){
      var fp = (r && r.fingerprint) || null;
      if(fp && _pinnedFp && normHex(fp) === normHex(_pinnedFp)){ onConnected(); return; }   // same verified cert -> silent
      if(fp){ onPinMismatch(fp); return; }                    // reachable but DIFFERENT cert -> hard-fail
      rediscoverPinned(onConnected);                          // no cert presented -> treat as moved/unreachable
    }).catch(function(){
      rediscoverPinned(onConnected);                          // network error -> hub may have moved
    });
  }
  function rediscoverPinned(onConnected){
    if(_reconnecting) return; _reconnecting = true;
    if(!zeroconfAvailable() || !WavrNet || typeof WavrNet.probe !== "function"){
      _reconnecting = false; showUnreachable(onConnected); return;
    }
    showConnecting("Reconnecting to " + (_coreName || "your home") + "…");
    var done = false, probed = {}, cap = 12;
    function finish(fn){ if(done) return; done = true; _reconnecting = false; clearTimeout(to); stopCoreWatch(); fn(); }
    var to = setTimeout(function(){ finish(function(){ showUnreachable(onConnected); }); }, 8000);
    startCoreWatch(function(svc){
      if(done) return;
      var core = WavrLib.parseCoreService(svc); if(!core) return;
      var key = core.host + ":" + core.port; if(probed[key]) return;
      if(Object.keys(probed).length >= cap) return;           // probe-storm guard: bound candidates probed
      probed[key] = true;
      var cand = "https://" + core.host + ":" + core.port;
      WavrNet.probe({ url: cand }).then(function(r){
        if(done) return;
        var fp = (r && r.fingerprint) || null;
        if(fp && _pinnedFp && normHex(fp) === normHex(_pinnedFp)){
          _base = cand;                                        // same verified cert, new address -> silent
          secureSet(K_URL, cand).then(function(){ finish(function(){ onConnected(); }); },
                                        function(){ finish(function(){ onConnected(); }); });   // persist best-effort; reconnect either way
        }
        // else: not our pinned cert -> keep looking (see header note on why this is not a hard-fail)
      }, function(){ /* candidate unreachable -> ignore, keep looking */ });
    }, function(){
      finish(function(){ showUnreachable(onConnected); });     // watch failed
    });
  }
  // Actionable terminal state when a remembered hub can't be reached or re-found. NEVER the frozen default
  // house: the overlay stays up with Search again + manual entry so the dead end is always recoverable.
  function showUnreachable(onConnected){
    var card = ensureOverlay("unreachable");
    card.appendChild(el("h2", "wavrm-h", "Can't reach " + (_coreName || "your hub")));
    card.appendChild(el("p", "wavrm-sub",
      "Your Wavr hub isn't answering at its last address, and we couldn't find it on this Wi-Fi. It may " +
      "be off, on a different network, or its address may have changed."));
    var again = el("button", "wavrm-btn", "Search again"); again.type = "button";
    again.onclick = function(){ connectPinned(onConnected); };
    var manual = el("button", "wavrm-btn ghost", "Enter address manually"); manual.type = "button";
    manual.onclick = function(){ showSetup(); };
    card.appendChild(again); card.appendChild(manual);
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

  // ----- Task 4: consent level as the connect/disconnect lever -----
  // The connection lever is driven by the consent level (green/yellow = attached, red = out). We never
  // edit index.html: to DISCONNECT we close live sockets and make netWebSocket refuse to open, so
  // index.html's reconnect loop keeps getting dead sockets and stays down; to RE-ENTER we re-allow
  // opening and reload so the provider reconstructs. Token is KEPT throughout (re-enter is one tap).
  var _attached = true;   // default; boot sets it from the stored level (Task 7)
  function closeLiveSockets(){
    for(var id in _socks){ if(Object.prototype.hasOwnProperty.call(_socks, id)){
      try{ if(WavrNet && typeof WavrNet.closeSocket === "function") WavrNet.closeSocket({ socketId: id }); }catch(_){}
    }}
    _socks = {};
  }
  // "Out" overlay: shown while RED/detached. FIX-A: the overlay is position:fixed;inset:0 and COVERS
  // index.html's .status-pills header, so the consent pill injected there is untappable while this card
  // is up. We therefore mount a REAL registered consent control INSIDE the card (makeConsentControl, so
  // it lives in _consentUis, paints the right colour, and its tap wraps red->green via changeConsent) --
  // that is the only guaranteed-tappable way back in. renderConsent() paints it red immediately.
  function showOut(){
    var card = ensureOverlay("out");
    card.appendChild(el("h2", "wavrm-h", "You've left Wavr"));
    card.appendChild(el("p", "wavrm-sub",
      "This device isn't connected and isn't sharing presence. Tap the control below to re-enter."));
    // Consent-control row, laid out like the sensor-node screen's consent row (a wavrm-node-stat row
    // with a label + makeConsentControl().btn). Tap = re-enter (red wraps to green); hold = stay out.
    card.appendChild(el("span", "wavrm-lab", "Your consent on this device"));
    var crow = el("div", "wavrm-node-stat");
    crow.appendChild(el("span", "wavrm-node-counts", "Tap to re-enter"));
    crow.appendChild(makeConsentControl().btn);
    card.appendChild(crow);
    renderConsent();   // paint the control red immediately (matches the current detached level)
  }
  // Enact the connection side of a consent level via WavrLib.consentToActions. Presence (register/DELETE)
  // is Task 5's job, called right after this. Returns nothing; never throws. green<->yellow (both attached)
  // is a NO-OP here -- no socket churn -- so only the contribution level changes on that transition.
  function applyAttachment(level){
    var act = WavrLib.consentToActions(level);
    if(act.attached){
      if(!_attached){
        _attached = true; hideOverlay();
        // ORDERING DEPENDENCY (do NOT reorder): on green/yellow re-enter this reload runs from inside
        // applyConsentLocal(level), which changeConsent calls BEFORE its own postConsent(level) on the
        // NEXT line. The re-enter consent POST only reaches the hub because Capacitor dispatches the
        // WavrNet.request native-bridge message SYNCHRONOUSLY, before this reload navigation commits.
        // Reordering (reloading before that POST is issued) would silently drop the re-enter consent POST.
        try{ location.reload(); }catch(_){}
      }
      // already attached (green<->yellow): NO socket churn -- only the contribution level changes.
    } else {
      _attached = false; closeLiveSockets(); showOut();
    }
  }

  // In-memory + UI + durable Keystore; on RED best-effort stop the native sensor loop. Returns the
  // Keystore write promise (a durable-write failure is non-fatal here -- the hub column is the enforcement).
  function applyConsentLocal(level){
    _consent = level;
    applyAttachment(level);                                 // Task 4: drive the connect/disconnect lever
    applyPresence(level);                                   // Task 5: register on green/yellow, DELETE on red
    if(level === "red"){ try{ stopSensor(); }catch(_){} }   // guarded no-op on a viewer with no sensor
    renderConsent();
    return secureSet(K_CONSENT, level).catch(function(){});
  }

  // ----- Task 5: network presence register / DELETE + re-assert -----
  // On ENTER (green/yellow) we POST our label; the Core resolves our SOURCE IP -> MAC (Android 10+ can't
  // read its own MAC) and lights us up "home". On EXIT (red) we DELETE. Fail-closed: mac_registered===false
  // means the Core CANNOT do network presence -> surface it (_presenceError) and never CLAIM presence. A
  // non-ok / 404 / network error is TRANSIENT: keep last state, do NOT throw, do NOT wipe the token, do NOT
  // falsely claim presence -- a later re-assert retries (respects FINDING F2: the running Core may 404 this
  // endpoint). Never logs the token or the label.
  var _presenceError = false;      // true only when the Core explicitly returned mac_registered:false
  // FIX-C2: presence is TRI-STATE, never fail-open. _presenceConfirmed is true ONLY after the Core returns
  // mac_registered:true; the presence-CLAIMING chip copy fires only on that. A non-ok / 404 / transient
  // (body===null) leaves BOTH flags at their last value, so an unconfirmed state renders the neutral
  // "Connected" that makes NO presence claim (honours "never claim presence the Core didn't confirm").
  var _presenceConfirmed = false;
  var _presenceInFlight = false;   // coalesce overlapping register calls (boot / reconnect / resume / timer)
  function registerPresence(){
    if(!_token || !_base || !_attached || _presenceInFlight) return;
    _presenceInFlight = true;
    netFetch(_base + "/api/presence/register-companion", {
      method: "POST",
      headers: { "Authorization": "Bearer " + _token, "Content-Type": "application/json" },
      body: JSON.stringify({ label: _presenceLabel || "" })
    }).then(function(r){
      return (r && r.ok) ? r.json() : null;   // non-ok / 404 -> null -> keep last state (transient)
    }).then(function(body){
      if(body && body.mac_registered === true){ _presenceConfirmed = true; _presenceError = false; }        // Core CONFIRMED network presence
      else if(body && body.mac_registered === false){ _presenceError = true; _presenceConfirmed = false; }  // Core can't do network presence
      // null / non-ok / 404 / transient -> leave BOTH as-is (keep last state; a later re-assert retries)
      renderStatusChip();                                                   // Task 6 chip reflects the state
    }).catch(function(){ /* transient (network / PIN_MISMATCH already raised): keep last state, retry later */ })
      .then(function(){ _presenceInFlight = false; });
  }
  function unregisterPresence(){
    if(!_token || !_base) return;
    netFetch(_base + "/api/presence/register-companion", {
      method: "DELETE",
      headers: { "Authorization": "Bearer " + _token }
    }).catch(function(){ /* best-effort; leaving LOCALLY (detach + token hidden) is what actually matters */ });
  }
  // Enact the presence side of a level (paired with applyAttachment). green/yellow register; red DELETEs
  // and clears the fail-closed flag (we are deliberately not present now, that is not an error).
  function applyPresence(level){
    var act = WavrLib.consentToActions(level);
    if(act.presence === "register"){ registerPresence(); }
    else { _presenceError = false; _presenceConfirmed = false; unregisterPresence(); renderStatusChip(); }
  }
  // Re-assert on the events that can silently drop a MAC/IP mapping (network change, Core restart, DHCP
  // renew): foreground resume, socket (re)connect, and a low-frequency safety timer. Only when entered.
  function reassertPresence(){
    if(WavrLib.consentToActions(_consent).presence === "register") registerPresence();
  }
  try{
    setInterval(function(){
      if(document.visibilityState === "visible") reassertPresence();
    }, 30 * 60 * 1000);
  }catch(_){}

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

  // ----- Task 6: status chip (attachment + presence) + details overlay (edit label / unpair) -----
  // A tappable chip stating what this device is doing right now. Fail-closed copy: only claims presence
  // the Core confirmed; on mac_registered:false it says so plainly. textContent only (never innerHTML with
  // the user's label). Injected into index.html's header .status-pills exactly like the consent pill --
  // NOT an index.html edit -- and idempotent.
  var _statusChip = null;
  function statusText(){
    if(!_attached) return "Out";
    if(_presenceError) return "Connected · no network presence";
    // FIX-C2: the presence CLAIM copy fires ONLY on a confirmed mac_registered:true. The default/transient/
    // 404 state says a neutral "Connected" that makes NO presence claim the Core hasn't confirmed.
    if(_presenceConfirmed) return "Connected to " + (_coreName || "your Core") + " as " + (_presenceLabel || "this device");
    return "Connected";
  }
  function renderStatusChip(){ if(_statusChip){ _statusChip.textContent = statusText(); } }
  function injectStatusChip(){
    if(!_token) return;
    var row = document.querySelector(".status-pills"); if(!row) return;
    if(document.getElementById("wavrm-status")) return;        // idempotent
    var b = el("button", "tpill"); b.id = "wavrm-status"; b.type = "button";
    b.onclick = function(){ showDetails(); };
    _statusChip = el("span", "p-txt", statusText());           // textContent set by el(); never logged
    b.appendChild(_statusChip);
    row.appendChild(b);
  }
  // Details overlay: edit the presence label (re-asserts on save) and a deliberate Unpair (DELETE presence,
  // then wipe token/url/fp + reload back to setup). Unpair is NOT a security downgrade -- it removes the
  // pairing entirely; re-pairing requires the full out-of-band fingerprint verify again.
  function showDetails(){
    var card = ensureOverlay("details");
    card.appendChild(el("h2", "wavrm-h", "This device"));
    var f = el("label", "wavrm-field"); f.appendChild(el("span", "wavrm-lab", "Your name on this device"));
    var input = el("input", "wavrm-input"); input.type = "text"; input.autocomplete = "off";
    input.maxLength = 48;   // FIX-C3: cap the display label (textContent + JSON.stringify already injection-safe)
    input.value = _presenceLabel || ""; input.placeholder = "e.g., Augusto";
    input.setAttribute("aria-label", "your name on this device");
    f.appendChild(input); card.appendChild(f);
    var msg = el("p", "wavrm-msg", "");
    var save = el("button", "wavrm-btn", "Save name"); save.type = "button";
    save.onclick = function(){
      _presenceLabel = (input.value || "").trim();
      save.disabled = true; msg.className = "wavrm-msg"; msg.textContent = "Saving…";
      secureSet(K_PRESENCE_LABEL, _presenceLabel).then(function(){
        renderStatusChip(); reassertPresence(); hideOverlay();
      }, function(){ save.disabled = false; msg.className = "wavrm-msg err"; msg.textContent = "Couldn't save. Try again."; });
    };
    card.appendChild(save);
    card.appendChild(el("p", "wavrm-sub", "Core: " + (_coreName || "—")));
    // The actual wipe -- security-critical, unchanged. Only reached via the explicit "Yes, unpair" confirm.
    function doUnpair(){
      unregisterPresence();   // best-effort DELETE FIRST -- builds the Bearer from the still-valid _token synchronously
      // FIX-C1 (mirror tokenSet(null)'s synchronous-invalidate): the secureDel writes below are ASYNC, so
      // without wiping the in-memory caches NOW a still-wired Task-5 trigger (socket-open reassert,
      // visibilitychange, or the 30-min timer) could fire a fully-authenticated register-companion POST in
      // the window before reload -- a fail-open in an explicit "stop participation" action. Invalidate
      // synchronously so registerPresence()'s guard (!_token || !_base || !_attached || _presenceInFlight)
      // early-returns for every post-click trigger, and any coalesced consent retry sees the wiped gen.
      _token = null; _base = ""; _pinnedFp = null; _attached = false; _presenceInFlight = false; _consentGen++;
      Promise.all([ secureDel(K_TOKEN), secureDel(K_URL), secureDel(K_FP) ]).then(function(){
        try{ location.reload(); }catch(_){}
      }, function(){ try{ location.reload(); }catch(_){} });
    }
    // F-UNPAIRCONFIRM: the soft keyboard can shift the overlay so a Save-aimed tap lands on Unpair and
    // instantly wipes the pairing. Require a distinct, on-demand confirm before the wipe ever fires.
    var unpair = el("button", "wavrm-btn ghost", "Unpair this device"); unpair.type = "button";
    var confirmPanel = el("div", "wavrm-field");   // hidden until Unpair is tapped; appears below, distinct from Save
    confirmPanel.style.display = "none";
    confirmPanel.appendChild(el("p", "wavrm-warn",
      "Unpairing removes this device. You'll need to verify the hub's certificate again to re-pair."));
    var yesUnpair = el("button", "wavrm-btn", "Yes, unpair"); yesUnpair.type = "button";
    var keepPaired = el("button", "wavrm-btn ghost", "Keep paired"); keepPaired.type = "button";
    unpair.onclick = function(){
      unpair.style.display = "none";        // hide the trigger so the confirm sits where nothing was
      confirmPanel.style.display = "";
    };
    keepPaired.onclick = function(){
      confirmPanel.style.display = "none";
      unpair.style.display = "";            // back to the normal details view
    };
    yesUnpair.onclick = function(){
      yesUnpair.disabled = true;
      doUnpair();
    };
    confirmPanel.appendChild(yesUnpair); confirmPanel.appendChild(keepPaired);
    card.appendChild(unpair); card.appendChild(confirmPanel);
    var back = el("button", "wavrm-btn ghost", "Back"); back.type = "button";
    back.onclick = function(){ hideOverlay(); };
    card.appendChild(back); card.appendChild(msg);
  }

  // ----- Boot: load caches, resolve the gate, then decide which screen to show -----
  function decideScreen(){
    whenDom(function(){
      injectStyle();
      if(_token){
        // Already paired -> never show the chooser. Clear the pairing chrome class index.html sets at
        // parse time (token cache was empty then) and mask the boot flash.
        try{ document.body.classList.remove("pairing-mode"); }catch(_){}
        // Task 7 Step 3: a RED-at-boot device is DETACHED -> show "You've left Wavr" with the in-card
        // re-enter control and do NOT connect. tokenGet() already hides the token and netWebSocket refuses,
        // so index.html's provider stays inert; this makes the state unmistakable and offers the way back.
        if(!_attached){ showOut(); injectConsentPill(); return; }
        if(capsSensorOnly()){
          // Dedicated sensor node: no dashboard (its token is hidden from index.html -> NullProvider).
          // Show the permission wizard once before the first Start, then the node screen.
          if(!_onboarded) showWizard(showNode); else showNode();
        } else if(capsHasSensor()){
          // sensor + viewer/admin: the dashboard is PRIMARY (boots exactly as the viewer path below);
          // inject the compact "this device" sensor pill into the header chrome. The wizard is deferred
          // to the pill's first Start (ensureOnboardedThen) so viewing is never blocked.
          // connectPinned confirms the stored hub is REACHABLE (silently re-finding it by pinned fp if its
          // DHCP address moved) before revealing the dashboard; the overlay stays up on failure instead of
          // showing the empty default house. Falls back to today's blind handoff when WavrNet.probe is absent.
          connectPinned(function(){ hideOverlay(); });
          injectSensorPill();
          injectConsentPill();   // consent toggle in the header chrome (combo device)
          injectStatusChip();    // Task 6: attachment + presence chip in the header chrome
        } else {
          // viewer / admin / migrated (token, no caps): the dashboard boots exactly as today once the hub is
          // CONFIRMED reachable. connectPinned probes the stored base (silently re-discovering it by pinned
          // fingerprint if the address moved) and only then hideOverlay reveals the dashboard; an unreachable
          // hub keeps an actionable overlay instead of the frozen default map. Absent WavrNet.probe it is
          // today's immediate handoff. Plus the consent toggle in the header chrome (participation !=
          // permission -- a viewer/admin/central device shows it too).
          connectPinned(function(){ hideOverlay(); });
          injectConsentPill();
          injectStatusChip();   // Task 6: attachment + presence chip in the header chrome
        }
      } else if(!_caps && !_base){
        // True first launch (nothing chosen AND nothing entered) -> capability chooser before setup.
        showChooser();
      } else if(_base && _pinnedFp){
        // Verified central but no token (resume after verify, a 401/403 revoke, or a pre-caps mid-pair
        // upgrade): the cert is already pinned, so resume the approve-on-Core request as PRIMARY (the pin
        // ceremony is not re-run). The 8-digit code entry stays one tap away inside showRequestPairing().
        showRequestPairing();
      } else {
        // caps chosen but not yet verified, or verify incomplete: an already-entered address resumes
        // straight to verify; otherwise offer discovery first (manual entry stays one tap away).
        if(_base) showVerify(); else showChooseCore();
      }
    });
  }

  Promise.all([secureGet(K_URL), secureGet(K_FP), secureGet(K_TOKEN),
               secureGet(K_DEVICE_ID), secureGet(K_ROLE),
               secureGet(K_CAPS), secureGet(K_ONBOARDED), secureGet(K_CONSENT),
               secureGet(K_PRESENCE_LABEL), secureGet(K_CORE_NAME)]).then(function(v){
    _base = v[0] || ""; _pinnedFp = v[1] || null; _token = v[2] || null;
    _deviceId = v[3] || null; _role = v[4] || null;   // role read synchronously at boot, like the token
    _caps = parseCaps(v[5]); _onboarded = (v[6] === "1");   // caps + onboarded read synchronously too
    _consent = normConsent(v[7]);   // consent level read synchronously -> the toggle's boot colour is correct
    _presenceLabel = v[8] || ""; _coreName = v[9] || "";   // Task 6: label + Core name for the status chip
    _attached = WavrLib.consentToActions(_consent).attached;   // Task 4: stored RED => boot DETACHED (fail-closed: tokenGet hides the token, netWebSocket refuses). Task 7 completes the boot-detached UX (showOut).
  }).catch(function(){}).then(function(){
    resolveReady();   // caches populated (or read failed): index.html's deferred boot may run now
    // detectRole drives the DASHBOARD admin surface only; a sensor-ONLY node has no dashboard and its
    // token 403s /api/devices, so skip it there. Viewer/admin/combo/migrated behave exactly as before.
    if(_token && !capsSensorOnly()){
      detectRole();
      if(_attached) registerPresence();   // Task 7 Step 2: entered boot -> re-assert network presence
    }
    decideScreen();
  });
})();
