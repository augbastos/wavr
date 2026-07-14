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
  // Item 7 (bonded Bluetooth import) + Complement (ii) (pinned web-asset OTA). Both are NATIVE
  // capabilities that live in their OWN sibling plugins (WavrNet contract stays FROZEN). ABSENT on a
  // build without the native plugin -> every call site is null-guarded and the feature degrades to an
  // honest "not available on this device" rather than throwing. Read-only bonds (no BLE scan, no
  // location); OTA carries WEB ASSETS ONLY over the SAME pinned trust anchor (never the shim/lib/native).
  var WavrBluetooth = plugin("WavrBluetooth");
  var WavrUpdate = plugin("WavrUpdate");

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
  // Item 3 (first-open contribute onboarding): a one-time flag so the affirmative-tap "help sense
  // who's home?" sequence is shown ONCE before the dashboard, never nagging after a choice is made
  // (mirrors K_ONBOARDED). Complement (ii) (What's-New): the last app version whose release-notes card
  // the user dismissed, so the card re-shows only on a version CHANGE and stays re-readable from details.
  var K_CONTRIB_ONBOARDED = "wavr.contribOnboarded", K_SEEN_VERSION = "wavr.seenVersion";

  // Complement (ii) — What's-New release notes, shown once per version change BEFORE the dashboard and
  // re-readable later from This-device. PUBLIC-facing copy (ships EN). `version` is compared against the
  // dismissed K_SEEN_VERSION; bump it + edit `notes` on each release. Purely local, no network.
  var WHATS_NEW = {
    version: "1.4",
    title: "What's new",
    notes: [
      "See at a glance whether this is an Admin or Member device.",
      "Your home's sensing level now shows here, read-only — set how much THIS device shares from the same card.",
      "A quick connection check tells you if this device and your hub are talking.",
      "Clearer help: tap the ? on any screen, then tap a control to see what it does."
    ]
  };

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
  var _contribOnboarded = false; // item 3: first-open contribute sequence completed at least once
  var _seenVersion = "";         // complement (ii): last What's-New version the user dismissed
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
  var _resolveReady, _readyDone = false, _screenDecided = false;
  var ready = new Promise(function(res){ _resolveReady = res; });
  function resolveReady(){ if(_readyDone) return; _readyDone = true; try{ _resolveReady(); }catch(_){} }
  // Defensive wedge path (#21): if the SecureStorage plugin never settles, the Promise.all at the bottom
  // never runs decideScreen(), so WITHOUT this the user is stranded on a blank overlay and the 8-digit
  // fallback would POST to an empty base. So the 4s timer force-resolves the gate AND lands the user on a
  // usable screen. decideScreen() is idempotent (the _screenDecided latch): whichever of the Keystore-
  // settled path or this timer runs first wins; the loser is a no-op. On a true wedge the caches are still
  // at their empty defaults, so decideScreen() falls through to the capability chooser (showChooser()).
  setTimeout(function(){ resolveReady(); decideScreen(); }, 4000);

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
    if(_roleInFlight) return;                            // coalesce overlapping triggers (boot / reconnect / resume)
    if(!_token || !_base || !_attached || !WavrNet) return; // no token+base+bridge, or DETACHED (left Wavr) -> resolve no role
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
        renderRolePill();                                        // item 1: repaint the pill on the no-reload path (null->user)
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
    listBondedDevices: listBondedDevices,   // item 7: read-only bonded-BT list -> status object (never a dead null)
    addAllBonded: addAllBonded,             // bulk consent-first import: ONE confirm -> register all bonded devices
    ready: ready
  };

  // Item 7 — read this phone's ALREADY-BONDED Bluetooth devices via the sibling WavrBluetooth plugin
  // (read-only: no BLE scan, no location, never a connect).
  // Resolves a STATUS OBJECT (never a bare null->dead-end) so the caller renders honest states:
  //   {state:'list', devices:[{address,label}]}  usable, bonded set (possibly empty)
  //   {state:'permission-needed'}                 BLUETOOTH_CONNECT (API 31+) not granted after a prompt
  //   {state:'bluetooth-off'}                     adapter is powered off — ask the user to enable it
  //   {state:'unavailable'}                       no plugin / no adapter / unexpected failure
  // Runtime permission is checked/requested FIRST ('na' below API 31 needs no prompt). READ-ONLY bonded
  // set: no BLE scan, no location. Never logs a MAC or a device name.
  function listBondedDevices(){
    if(!(WavrBluetooth && typeof WavrBluetooth.listBonded === "function")) return Promise.resolve({ state: "unavailable" });
    function usable(state){ return state === "granted" || state === "na"; }
    function readBonded(){
      return Promise.resolve(WavrBluetooth.listBonded()).then(function(r){
        var raw = (r && Array.isArray(r.devices)) ? r.devices : [];
        var devices = raw.map(function(d){
          return { address: (d && d.address) || "", label: (d && (d.label || d.name)) || "" };
        }).filter(function(d){ return !!d.address; });
        return { state: "list", devices: devices };
      }, function(err){
        var code = err && err.code;
        if(code === "BT_OFF") return { state: "bluetooth-off" };
        if(code === "PERMISSION_DENIED") return { state: "permission-needed" };
        return { state: "unavailable" };   // NO_ADAPTER / anything else
      });
    }
    function check(){
      if(typeof WavrBluetooth.checkPermissions !== "function") return Promise.resolve("granted");
      return Promise.resolve(WavrBluetooth.checkPermissions())
        .then(function(p){ return (p && p.bluetooth) || "granted"; }, function(){ return "granted"; });
    }
    return check().then(function(state){
      if(usable(state)) return readBonded();
      if(typeof WavrBluetooth.requestPermissions !== "function") return { state: "permission-needed" };
      return Promise.resolve(WavrBluetooth.requestPermissions()).then(function(p){
        var after = (p && p.bluetooth) || state;
        return usable(after) ? readBonded() : { state: "permission-needed" };
      }, function(){ return { state: "permission-needed" }; });
    });
  }

  // [C2] Bulk consent-first import: after ONE confirm, register EVERY bonded device of THIS phone as a
  // LABEL under `person` (defaults to this device's own presence label). HONEST SCOPE — a bonded MAC is a
  // name so your home can recognise the device, NOT live presence (the hub's radios decide that). Sequential
  // loop-POST /api/identity/devices so one failure never aborts the rest. Resolves {added,skipped}. The
  // per-device add path stays available in the tile; this is the one-consent-brings-all primary UX. Never
  // logs a MAC, name, or the token.
  function addAllBonded(person){
    return listBondedDevices().then(function(status){
      if(!status || status.state !== "list" || !status.devices || !status.devices.length){
        return { added: 0, skipped: 0, state: (status && status.state) || "unavailable" };
      }
      var name = String(person != null ? person : (_presenceLabel || "")).trim();
      var n = status.devices.length;
      var ask = "Add " + n + " Bluetooth device" + (n === 1 ? "" : "s") + " paired to this phone to Wavr" +
                (name ? " as “" + name + "”" : "") + "?\n\n" +
                "This adds their names so your home can recognise them. It does NOT track live location. " +
                "You can remove them any time on your hub.";
      var ok = false; try{ ok = window.confirm(ask); }catch(_){ ok = false; }
      if(!ok) return { added: 0, skipped: n, cancelled: true };
      if(!_token || !_base) return { added: 0, skipped: n, state: "not-paired" };
      var added = 0, skipped = 0, chain = Promise.resolve();
      status.devices.forEach(function(d){
        chain = chain.then(function(){
          if(!d || !d.address){ skipped++; return; }
          return netFetch(_base + "/api/identity/devices", {
            method: "POST",
            headers: { "Authorization": "Bearer " + _token, "Content-Type": "application/json" },
            body: JSON.stringify({ person: name, devices: [{ address: d.address, source: "ble", origin: "bonded" }] })
          }).then(function(r){ if(r && r.ok) added++; else skipped++; }, function(){ skipped++; });
        });
      });
      return chain.then(function(){ return { added: added, skipped: skipped }; });
    });
  }

  // Foreground/resume trigger for detectRole: re-check our role when the app returns to the fore.
  // visibilitychange is the zero-dependency choice (no @capacitor/app); it fires on WebView
  // resume in Chrome/Android. detectRole self-guards on token/base and coalesces, so an extra
  // fire is harmless. FALLBACK (only if on-device proves visibilitychange does NOT fire on native
  // resume): add @capacitor/app's appStateChange. Do NOT add that dependency pre-emptively.
  try{
    document.addEventListener("visibilitychange", function(){
      if(document.visibilityState === "visible"){ detectRole(); reassertPresence(); pollManifest(); }   // Task 5 + OTA (ii) on resume
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
      ".wavrm-consent.holding .wavrm-cprog{width:100%;transition:width 2s linear;}" +
      // Item 5: the shared "?" help button, top-right of every card.
      ".wavrm-card{position:relative;}" +
      ".wavrm-help{position:absolute;top:10px;right:10px;width:32px;height:32px;min-height:0;padding:0;" +
      "border-radius:999px;border:1px solid var(--line,rgba(255,255,255,.14));background:transparent;" +
      "color:var(--dim,#9AA4AD);font-size:1rem;font-weight:700;line-height:1;cursor:pointer;}" +
      // Item 1: role pill accent (Admin device). Member device stays the neutral .tpill default.
      "#wavrm-role.wavrm-role-admin{border-color:var(--accent,#3db54a)!important;color:var(--accent,#3db54a);}" +
      // Item 2/6: a labelled row that hosts a control (consent tile / health rows).
      ".wavrm-inrow{display:flex;align-items:center;justify-content:space-between;gap:10px;" +
      "background:var(--elevated,#1C232C);border:1px solid var(--line,rgba(255,255,255,.07));" +
      "border-radius:10px;padding:10px 12px;}" +
      ".wavrm-inrow .wavrm-inrow-l{color:var(--dim,#9AA4AD);font-size:.82rem;}" +
      ".wavrm-inrow .wavrm-inrow-v{font-size:.86rem;font-variant-numeric:tabular-nums;text-align:right;}" +
      ".wavrm-inrow .wavrm-inrow-v.ok{color:var(--accent,#3db54a);}" +
      ".wavrm-inrow .wavrm-inrow-v.warn{color:var(--warn,#e8a13a);}" +
      ".wavrm-inrow .wavrm-inrow-v.err{color:var(--danger,#e8726a);}" +
      // Item 2: device-consent row mounted INTO index.html's #sensingLevelTile (companion).
      "#wavrm-consent-tile{margin-top:12px;display:flex;flex-direction:column;gap:6px;}" +
      "#wavrm-consent-tile .wavrm-ct-lab{font-size:.72rem;font-weight:600;text-transform:uppercase;" +
      "letter-spacing:.06em;color:var(--dim,#9AA4AD);}" +
      // Complement (ii): the OTA "update available" chip in the header pill row.
      "#wavrm-ota{cursor:pointer;}" +
      // Complement (iii): the one-glance per-device privacy receipt.
      ".wavrm-receipt{margin:0;font-size:.82rem;color:var(--dim,#9AA4AD);" +
      "border-left:2px solid var(--consent-color,var(--line,rgba(255,255,255,.14)));padding-left:10px;}";
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
    // Item 5 (help everywhere): one shared "?" per overlay card. The shim overlay is
    // position:fixed;inset:0;z-index:2147483000 and COVERS index.html's own #helpModeBtn, so we can't
    // rely on the user reaching it. This button PROGRAMMATICALLY clicks #helpModeBtn — reusing
    // index.html's existing help state machine verbatim (a real click fires regardless of z-order), which
    // then turns any tap on a [data-tip] control (incl. the shim controls below) into a tooltip reveal.
    // It carries NO data-tip itself, so index.html's help-mode click handler always lets it ACT (toggle),
    // never intercepts it. Security screens (mismatch/reVerify) only gain EXPLANATION this way — never a
    // bypass. NOT VERIFIED on-device: #tipPop z-index vs this overlay (bump #tipPop if a tip is occluded).
    var helpBtn = el("button", "wavrm-help", "?"); helpBtn.type = "button";
    helpBtn.setAttribute("aria-label", "Help — explain the controls on this screen");
    helpBtn.onclick = function(){ try{ var hb = document.getElementById("helpModeBtn"); if(hb) hb.click(); }catch(_){} };
    card.appendChild(helpBtn);
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

  // FIX-E1b PINNED DERIVATION -- MUST stay byte-identical to the backend Python:
  //   input   = <fp_hex_lowercase_no_colons> + "|" + <pair_code>              (ASCII/UTF-8)
  //   digest  = SHA-256(input)                                                (32 bytes)
  //   verify6 = ( first 4 bytes, big-endian uint32 ) mod 1000000, zero-padded to 6 decimal digits
  // Binding the 6-digit to the EXISTING short-TTL rotating pair_code (from /api/pair-code) makes it NOT
  // offline-grindable: a MitM must grind within the code's ~2-min TTL (the accepted local-wifi tradeoff).
  // Both the probed fp and the pair_code arrive over the UNVERIFIED channel, but the derivation is anchored
  // by the user comparing to the 6-digit the HUB computes from its OWN real cert -- an attacker's probed
  // cert yields a different verify6, so a rushed user cannot pin it. This is the CONVENIENCE tier; a QR
  // full-256-bit machine-compare is the later strong tier (see the QR-SCAN HOOK in showVerify).
  // Returns a Promise<string(6 digits)>, or null when secure crypto / inputs are unavailable (fail-closed).
  function computeVerify6(probedFp, pairCode){
    if(!(window.crypto && window.crypto.subtle && window.TextEncoder)) return null;
    var fpHex = normHex(probedFp).toLowerCase();                 // lowercase hex, NO colons (normalized)
    if(!fpHex || pairCode == null || pairCode === "") return null;
    var bytes = new TextEncoder().encode(fpHex + "|" + String(pairCode));   // ASCII/UTF-8
    return window.crypto.subtle.digest("SHA-256", bytes).then(function(buf){
      var d = new Uint8Array(buf);
      var u32 = ((d[0] << 24) | (d[1] << 16) | (d[2] << 8) | d[3]) >>> 0;   // first 4 bytes, big-endian
      var s = String(u32 % 1000000);
      while(s.length < 6) s = "0" + s;
      return s;
    });
  }

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
      showScanPair();   // QR scan is the PRIMARY pin+pair path; "type the code" stays one tap away inside it
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
    btn.setAttribute("data-tip", "Saves the hub address and moves on to verifying its certificate.");
    btn.onclick = function(){
      var host = (ip.value || "").trim(), p = (port.value || "").trim() || "8000";
      if(!isHost(host)){ msg.className = "wavrm-msg err"; msg.textContent = "Enter a valid IP address."; return; }
      if(!isPort(p)){ msg.className = "wavrm-msg err"; msg.textContent = "Enter a valid port (1 to 65535)."; return; }
      _base = "https://" + host + ":" + p;
      showScanPair();   // QR scan primary; the typed 6-digit path stays reachable from inside showScanPair
    };
    card.appendChild(btn); card.appendChild(msg);
  }

  // ----- Screen 2: verify (out-of-band 6-DIGIT compare). Pins only on an ACTIVE derived match. -----
  // PINNED-DERIVATION flow (replaces the old typed-hex-last-6 challenge; NO hex is shown anywhere):
  //   1. probe the cert -> PROBED fingerprint (the sole trust anchor; used only inside the derivation).
  //   2. fetch /api/pair-code -> the hub's short-TTL rotating pair_code. The body ALSO carries verify6 +
  //      cert_fingerprint: BOTH are DELIBERATELY IGNORED as trust anchors -- a MitM controls this body --
  //      mirroring the discipline that already ignores body cert_fingerprint in the pair-request flow.
  //   3. derive verify6_local = SHA-256(probed_fp | pair_code) on-device (computeVerify6).
  //   4. the user types the 6-digit the HUB shows on its own screen; we live-enable ("digita e entra")
  //      ONLY on a full match. An attacker's probed cert yields a different verify6, so the hub's real
  //      6-digit cannot pin the attacker's cert -- the whole reason this screen exists.
  // A non-matching full entry surfaces an honest hard-stop message (typo OR interception); there is NO
  // "trust anyway", NO silent proceed, and NO fallback to a weaker/grindable pin. Works for the 'user' role.
  // ⚠️ DEAD PATH (kept for reference only): NOTHING calls showVerify() anymore. Pairing is QR-scan-only
  // (showScanPair, the stronger full-fingerprint anchor). The /api/pair-code fetch below is admin/loopback-
  // only and 403s a LAN phone, so this typed-verify6 flow could never complete — it was removed from the UI.
  function showVerify(){
    var card = ensureOverlay("verify");
    card.appendChild(el("h2", "wavrm-h", "Verify your hub"));
    card.appendChild(el("p", "wavrm-sub",
      "Your Wavr hub shows a 6-digit code on its own screen (open Settings, then Pair device). " +
      "Type that code below so this phone can confirm it is really talking to your hub."));
    card.appendChild(el("p", "wavrm-warn",
      "Only use the code shown on the hub's own screen. If the codes never match, stop — " +
      "someone may be intercepting your network."));

    var f = el("label", "wavrm-field");
    f.appendChild(el("span", "wavrm-lab", "Enter the 6-digit code shown on your hub"));
    var codeIn = el("input", "wavrm-input"); codeIn.type = "text";
    codeIn.inputMode = "numeric"; codeIn.setAttribute("inputmode", "numeric");
    codeIn.setAttribute("pattern", "[0-9]*"); codeIn.autocomplete = "off"; codeIn.spellcheck = false;
    codeIn.maxLength = 6; codeIn.placeholder = "000000";
    codeIn.setAttribute("aria-label", "6-digit code shown on your hub");
    f.appendChild(codeIn); card.appendChild(f);

    // The presence label is NOT collected here -- it is collected once at showRequestPairing() (Gate B),
    // right after this pin. This screen is purely the out-of-band certificate compare.
    var pinBtn = el("button", "wavrm-btn", "Verify & connect"); pinBtn.type = "button"; pinBtn.disabled = true;
    pinBtn.setAttribute("data-tip", "Trusts this hub. Enabled only when the 6-digit code you typed matches the one derived from the hub's own certificate.");
    var backBtn = el("button", "wavrm-btn ghost", "Back"); backBtn.type = "button";
    var msg = el("p", "wavrm-msg", "");

    // fp = PROBED cert fingerprint (anchor). pairCode = short-TTL rotating code (closure only; NEVER
    // rendered, logged, or persisted). expect6 = the 6 digits we compare the entry to, derived once.
    var fp = null, pairCode = null, expect6 = null, deriving = false;
    function typed(){ return (codeIn.value || "").replace(/[^0-9]/g, ""); }
    function recompute(){ pinBtn.disabled = !(expect6 && typed().length === 6 && typed() === expect6); }
    // Derive verify6_local once BOTH the probed fp and the pair_code are known (async SHA-256). Fail-closed.
    function deriveExpect(){
      if(deriving || expect6 || !fp || pairCode == null) return;
      var p = computeVerify6(fp, pairCode);
      if(!p){ msg.className = "wavrm-msg err"; msg.textContent = "This device can't verify securely. Update the app."; return; }
      deriving = true;
      p.then(function(v){ expect6 = v; deriving = false; recompute(); },
             function(){ deriving = false; msg.className = "wavrm-msg err"; msg.textContent = "Couldn't verify the code. Try again."; });
    }
    codeIn.oninput = function(){
      recompute();
      // Honest hard-stop on a FULL non-matching entry (typo OR interception). No proceed control exists;
      // the only way forward is a code that actually derives from the probed cert.
      if(expect6 && typed().length === 6 && typed() !== expect6){
        msg.className = "wavrm-msg err";
        msg.textContent = "That code doesn't match your hub. Re-check the hub's screen. If it never matches, someone may be intercepting your network — stop.";
      } else if(msg.className === "wavrm-msg err" && (typed().length < 6)){
        msg.textContent = ""; msg.className = "wavrm-msg";
      }
    };
    pinBtn.onclick = function(){
      if(pinBtn.disabled) return;
      if(!fp || !expect6 || typed() !== expect6) return;   // defence in depth: never pin without a derived match
      _pinnedFp = fp;                                       // anchor = the PROBED cert, never a body value
      _coreName = _pendingCoreName || _base;                // Task 6: friendly Core name (mDNS pick or base)
      pinBtn.disabled = true; msg.className = "wavrm-msg"; msg.textContent = "Saving…";
      Promise.all([ persistPairing(_base, fp, null),
                    secureSet(K_CORE_NAME, _coreName) ]).then(function(){
        // Gate A done (cert pinned via the out-of-band 6-digit derivation). Gate B (authorization) is the
        // approve-on-Core flow; the 8-digit code path stays one tap away inside showRequestPairing().
        showRequestPairing();
      }, function(){
        _pinnedFp = null;                                    // durable write failed: do NOT pretend paired
        msg.className = "wavrm-msg err";
        msg.textContent = "Couldn't save the pairing securely. Try again.";
        recompute();
      });
    };
    backBtn.onclick = function(){ showSetup(); };
    card.appendChild(pinBtn); card.appendChild(backBtn);
    // Back-to-discovery: a discovery user who reached verify by picking a hub must not be stranded at the
    // manual-IP setup screen (plain Back goes there). Offer a second ghost path straight back to the list.
    if(zeroconfAvailable()){
      var discBtn = el("button", "wavrm-btn ghost", "Back to hub list"); discBtn.type = "button";
      discBtn.onclick = function(){ showChooseCore(); };
      card.appendChild(discBtn);
    }
    card.appendChild(msg);

    // QR-SCAN HOOK (Phase-2 STRONG tier -- NOT built here; needs the camera): slot a bundled on-device
    // scanner button here that reads the hub's QR carrying the FULL SHA-256 and MACHINE-compares the entire
    // 256-bit fingerprint to `fp` (no typing, no TTL grind window). Frames processed on-device only, never
    // stored or transmitted; still under the isNative guard; ships only after an egress sign-off.

    if(!WavrNet || typeof WavrNet.probe !== "function"){
      msg.className = "wavrm-msg err"; msg.textContent = "Native networking is not available."; return;
    }
    // 1) Probe the cert -> PROBED fingerprint. Never rendered as hex; used only inside the derivation.
    WavrNet.probe({ url: _base }).then(function(r){
      fp = (r && r.fingerprint) || null;
      if(!fp){ msg.className = "wavrm-msg err"; msg.textContent = "The hub presented no certificate. Check the address."; return; }
      deriveExpect();
    }).catch(function(){
      msg.className = "wavrm-msg err";
      msg.textContent = "Could not reach " + _base + ". Check the address and that the hub is on.";
    });
    // 2) Fetch the hub's short-TTL rotating pair_code over the (still-unverified) pinned transport. body.verify6
    // and body.cert_fingerprint are IGNORED as anchors (a MitM controls the body); we derive locally from the
    // PROBED cert. pair_code is a binding input, not a credential: never rendered, logged, or persisted.
    netFetch(_base + "/api/pair-code", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role: "user" })
    }).then(function(res){
      if(!res || !res.ok) throw new Error("pair-code status " + (res ? res.status : "?"));
      return res.json();
    }).then(function(body){
      pairCode = body && body.code;                          // rotating short-TTL code; closure only
      if(pairCode == null || pairCode === ""){ msg.className = "wavrm-msg err"; msg.textContent = "Your hub didn't return a pairing code. Try again."; return; }
      deriveExpect();
    }).catch(function(err){
      if(err && err.code === "PIN_MISMATCH") return;         // no pin yet, but keep the discipline
      msg.className = "wavrm-msg err";
      msg.textContent = "Couldn't get a code from " + _base + ". Check the hub is on and try again.";
    });
  }

  // ===== QR strong-anchor pairing (chosen 2026-07-14): scan the hub's QR instead of typing =====
  // The hub's Pair-device screen (Settings -> Devices) renders a QR of {v, u, fp, c}:
  //   u  = the hub's OWN origin -- DELIBERATELY IGNORED here. On a Core kiosk it is loopback
  //        (https://localhost:8000), useless to a phone; we connect to _base, the address THIS device
  //        already chose via mDNS discovery / manual entry. The QR supplies the ANCHOR, not the where.
  //   fp = the FULL cert SHA-256 -> the trust anchor, MACHINE-compared to the probed cert (not a 6-digit
  //        code a MitM can offline-grind while relaying).
  //   c  = the same short-TTL 8-digit code the typed path redeems via POST /api/pair.
  // Camera frames are decoded on-device (self-hosted jsQR) and NEVER stored or transmitted -- same
  // zero-egress invariant as the rest of the app.
  var _jsqrPromise = null;
  function _jsqrCallable(){
    var g = window.jsQR;
    if(typeof g === "function") return g;
    if(g && typeof g.default === "function") return g.default;   // some UMD builds expose {default:fn}
    return null;
  }
  function _ensureJsQR(){
    var have = _jsqrCallable();
    if(have) return Promise.resolve(have);
    if(_jsqrPromise) return _jsqrPromise;
    _jsqrPromise = new Promise(function(resolve, reject){
      var s = document.createElement("script");
      s.src = "vendor/jsqr.js";                 // same origin -> nothing leaves the device
      s.onload = function(){ var fn = _jsqrCallable(); fn ? resolve(fn) : reject(new Error("jsQR global not callable")); };
      s.onerror = function(){ _jsqrPromise = null; reject(new Error("failed to load vendor/jsqr.js")); };
      document.head.appendChild(s);
    });
    return _jsqrPromise;
  }

  // Screen: collect the presence name, then open the camera. The ONLY pairing path — QR scan pins the hub's
  // FULL cert fingerprint (not a grindable typed code). The old "type the 6-digit code" fallback was removed:
  // its /api/pair-code fetch is admin/loopback-only (403 for a LAN phone), so it was a dead, misleading path.
  function showScanPair(){
    var card = ensureOverlay("scanPair");
    card.appendChild(el("h2", "wavrm-h", "Scan your hub's QR"));
    card.appendChild(el("p", "wavrm-sub",
      "On your Wavr hub, open Settings then Devices to show its pairing QR. Enter your name, then point the camera at it."));
    var f = el("label", "wavrm-field");
    f.appendChild(el("span", "wavrm-lab", "Your name on this device (shown as your presence at home)"));
    var nameIn = el("input", "wavrm-input"); nameIn.type = "text"; nameIn.autocomplete = "off";
    nameIn.maxLength = 48; nameIn.placeholder = "e.g., Augusto"; nameIn.value = _presenceLabel || "";
    nameIn.setAttribute("aria-label", "your name on this device");
    f.appendChild(nameIn); card.appendChild(f);
    var msg = el("p", "wavrm-msg", "");
    var scanBtn = el("button", "wavrm-btn", "Open camera to scan"); scanBtn.type = "button";
    scanBtn.setAttribute("data-tip", "Scans the hub's QR: pins its full certificate and pairs, no typing.");
    scanBtn.onclick = function(){
      var name = (nameIn.value || "").trim();
      if(!name){ msg.className = "wavrm-msg err"; msg.textContent = "Enter your name on this device."; return; }
      _presenceLabel = name;
      secureSet(K_PRESENCE_LABEL, name).catch(function(){});   // best-effort; display-only, not a credential
      startCameraScan(name);
    };
    card.appendChild(scanBtn);
    var back = el("button", "wavrm-btn ghost", "Back"); back.type = "button";
    back.onclick = function(){ if(zeroconfAvailable()){ showChooseCore(); } else { showSetup(); } };
    card.appendChild(back);
    card.appendChild(msg);
  }

  // Live camera scan: getUserMedia (Capacitor 8's BridgeWebChromeClient auto-requests the runtime CAMERA
  // permission on this call) -> per-frame jsQR decode on a canvas. On a hit, hand the raw text to
  // onScannedPayload. Always tears the stream down (cancel, success, or error) -> no camera left running.
  function startCameraScan(name){
    var card = ensureOverlay("cameraScan");
    card.appendChild(el("h2", "wavrm-h", "Point at the hub's QR"));
    var video = document.createElement("video");
    video.setAttribute("playsinline", ""); video.muted = true; video.autoplay = true;
    video.style.width = "100%"; video.style.maxWidth = "320px"; video.style.aspectRatio = "1 / 1";
    video.style.objectFit = "cover"; video.style.borderRadius = "14px"; video.style.background = "#000";
    card.appendChild(video);
    var msg = el("p", "wavrm-msg", "Looking for the QR…");
    var canvas = document.createElement("canvas");
    var ctx = canvas.getContext("2d", { willReadFrequently: true });
    var stream = null, raf = 0, done = false, jsQRfn = null;
    function stop(){
      done = true;
      if(raf){ try{ cancelAnimationFrame(raf); }catch(_){} raf = 0; }
      if(stream){ try{ stream.getTracks().forEach(function(t){ t.stop(); }); }catch(_){} stream = null; }
    }
    var cancel = el("button", "wavrm-btn ghost", "Cancel"); cancel.type = "button";
    cancel.onclick = function(){ stop(); showScanPair(); };
    card.appendChild(cancel); card.appendChild(msg);
    if(!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)){
      msg.className = "wavrm-msg err"; msg.textContent = "This device can't open a camera to scan the pairing QR."; return;
    }
    _ensureJsQR().then(function(fn){
      jsQRfn = fn;
      return navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" } }, audio: false });
    }).then(function(s){
      if(done){ try{ s.getTracks().forEach(function(t){ t.stop(); }); }catch(_){} return; }   // cancelled mid-await
      stream = s; video.srcObject = s;
      var pv = video.play && video.play(); if(pv && pv.catch) pv.catch(function(){});
      function tick(){
        if(done) return;
        if(video.readyState >= 2 && video.videoWidth){
          canvas.width = video.videoWidth; canvas.height = video.videoHeight;
          ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
          var img = null;
          try{ img = ctx.getImageData(0, 0, canvas.width, canvas.height); }catch(_){ img = null; }
          if(img){
            var r = jsQRfn(img.data, img.width, img.height, { inversionAttempts: "dontInvert" });
            if(r && r.data){ stop(); onScannedPayload(r.data, name); return; }
          }
        }
        raf = requestAnimationFrame(tick);
      }
      raf = requestAnimationFrame(tick);
    }).catch(function(e){
      stop();
      var denied = e && (e.name === "NotAllowedError" || e.name === "SecurityError" || e.name === "NotFoundError");
      msg.className = "wavrm-msg err";
      msg.textContent = denied
        ? "Camera access is off. Allow the camera for Wavr, then go back and scan again."
        : "Couldn't open the camera. Check no other app is using it, then go back and scan again.";
      var back = el("button", "wavrm-btn ghost", "Back"); back.type = "button";
      back.onclick = function(){ showScanPair(); }; card.appendChild(back);
    });
  }

  // Parse the scanned payload, MACHINE-verify the hub's full cert against the QR fingerprint, pin, then
  // redeem the code for a token over the pinned transport. Mirrors showVerify's pin + showRequestPairing's
  // token-commit: a durable-write failure never pretends paired, a cert swap hard-fails on PIN_MISMATCH.
  function onScannedPayload(text, name){
    var p = null;
    try{ p = JSON.parse(text); }catch(_){}
    if(!p || !p.fp || p.c == null || p.c === ""){
      scanHardFail("That QR isn't a Wavr pairing code. Open Settings then Devices on your hub and scan the QR shown there.", name, true);
      return;
    }
    var qrFp = String(p.fp), code = String(p.c);
    var card = ensureOverlay("scanVerify");
    card.appendChild(el("div", "wavrm-spin", ""));
    card.appendChild(el("h2", "wavrm-h", "Checking the hub…"));
    var msg = el("p", "wavrm-msg", "Matching the hub's certificate…"); card.appendChild(msg);
    if(!WavrNet || typeof WavrNet.probe !== "function"){
      msg.className = "wavrm-msg err"; msg.textContent = "Native networking is not available."; return;
    }
    // Probe the ACTUAL cert at _base (the mDNS / manually chosen hub) and require it to EQUAL the scanned
    // full fingerprint. A LAN MitM presents a different cert -> normHex mismatch -> hard stop, never pin.
    WavrNet.probe({ url: _base }).then(function(r){
      var probed = (r && r.fingerprint) || null;
      if(!probed){ scanHardFail("The hub presented no certificate. Check you picked the right hub.", name, false); return; }
      if(normHex(probed) !== normHex(qrFp)){
        scanHardFail("The hub's certificate does NOT match the QR. Stop — someone may be intercepting your network. If it keeps happening, pair on your home Wi-Fi.", name, false);
        return;
      }
      _pinnedFp = probed;                                    // anchor == the scanned full fp (machine-verified)
      _coreName = _pendingCoreName || _base;
      msg.className = "wavrm-msg"; msg.textContent = "Codes match. Connecting…";
      // Persist the pin, then redeem the code over the PINNED transport (netFetch uses _pinnedFp). 403 =
      // the code rotated/expired -> scan a fresh QR. A cert swap now hard-fails via PIN_MISMATCH.
      persistPairing(_base, probed, null).then(function(){
        return netFetch(_base + "/api/pair", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ code: code, device_name: name })
        });
      }).then(function(res){
        if(!res || !res.ok) throw new Error("pair status " + (res ? res.status : "?"));
        return res.json();
      }).then(function(body){
        var token = body && body.token, deviceId = body && body.device_id;
        if(!token){ scanHardFail("The hub accepted the code but sent no token. Scan a fresh QR.", name, false); return; }
        persistPairing(_base, probed, token).then(function(){
          _token = token;                                    // sync cache; the reload re-reads it from Keystore
          try{ onPaired({ device_id: deviceId }); }catch(_){}
          showConnecting("Connecting to " + (_coreName || "your home") + "…");
          try{ location.reload(); }catch(_){}                // reboot straight into the dashboard viewer
        }, function(){
          _pinnedFp = null;                                  // durable write failed: do NOT pretend paired
          scanHardFail("Couldn't save the pairing securely. Try again.", name, false);
        });
      }).catch(function(err){
        if(err && err.code === "PIN_MISMATCH") return;       // netFetch already raised the hard-fail card
        scanHardFail("Couldn't finish pairing at " + (_coreName || _base) + ". The code may have expired — scan a fresh QR.", name, false);
      });
    }).catch(function(){
      scanHardFail("Couldn't reach the hub. Check it's on and you're on the same Wi-Fi, then scan again.", name, false);
    });
  }

  function scanHardFail(text, name, isBadQr){
    var card = ensureOverlay("scanFail");
    card.appendChild(el("h2", "wavrm-h", isBadQr ? "That isn't a Wavr QR" : "Pairing stopped"));
    card.appendChild(el("p", isBadQr ? "wavrm-sub" : "wavrm-warn", text));
    var retry = el("button", "wavrm-btn", "Scan again"); retry.type = "button";
    retry.onclick = function(){ startCameraScan(name); };
    card.appendChild(retry);
    var back = el("button", "wavrm-btn ghost", "Back"); back.type = "button";
    back.onclick = function(){ showScanPair(); };
    card.appendChild(back);
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
    askBtn.setAttribute("data-tip", "Sends a request to your hub. Its owner approves it on the hub's own screen — no code to type.");
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
    if(!sensorAvailable()) return;
    // Item 4: you cannot start contributing while shown "Off" (red = detached). Raise to Presence
    // (yellow) first -- which re-attaches (and reloads) via changeConsent -- so the tri-color never
    // disagrees with a running sensor. The post-reattach node screen offers Start again.
    if(_consent === "red"){ changeConsent("yellow"); return; }
    ensureSensor();
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
    btn.setAttribute("data-tip", "Starts or stops contributing this device's presence to your home.");
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
  // Item 4: labels use the HUB's own sensing vocabulary (Off / Presence / Full, mirroring index.html's
  // TIER_META) so the device-scope control and the home-scope tile read the same. Wire values
  // (green/yellow/red) and the colour mapping are UNCHANGED -- only the human label/tip.
  var CONSENT = {
    green:  { next: "yellow", color: "var(--accent,#3db54a)", label: "Full",
              tip: "Full — Wavr uses this phone's presence and names it at home." },
    yellow: { next: "red",    color: "var(--warn,#e8a13a)",   label: "Presence",
              tip: "Presence — present but anonymous; minimal data, no name." },
    // TAP wraps red -> green: a single visible control must be able to RE-ENGAGE, and re-granting one's
    // OWN consent is legitimate. The deliberate 2s-hold is the easy-withdrawal path, so an accidental
    // single tap can only ever step the level (never hold-to-off), and every tap changes colour+label
    // and POSTs -- so a mistaken tap is instantly visible and reversible with another tap.
    red:    { next: "green",  color: "var(--danger,#e8726a)", label: "Off",
              tip: "Off — you've left Wavr; this device contributes nothing." }
  };
  function normConsent(v){ return (v === "green" || v === "yellow" || v === "red") ? v : "green"; }

  var _consentUis = [];        // every rendered consent control (header pill + node-screen copy)
  // [A2] Registered privacy-receipt nodes (the live "shares … / shares nothing" sentence). renderConsent
  // re-paints these in place on every level change, so the tile/details sentence updates WITH the pills.
  var _consentReceipts = [];
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
      u.btn.setAttribute("data-tip", tip);   // complement (iv): help mode explains the CURRENT level
      u.btn.setAttribute("aria-label", "Consent: " + label + ". Tap to reduce, hold two seconds to withdraw.");
      u.btn.classList.toggle("pending", !!_consentPending);
    }
    // [A2] Re-paint the registered privacy receipts (tile + details) so the live sentence tracks the level.
    if(document.body){ _consentReceipts = _consentReceipts.filter(function(p){ return document.body.contains(p); }); }
    for(var j = 0; j < _consentReceipts.length; j++){
      var rp = _consentReceipts[j];
      rp.textContent = privacyReceiptText();
      try{ rp.style.setProperty("--consent-color", meta.color); }catch(_){}
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
  function renderStatusChip(){
    if(_statusChip){ _statusChip.textContent = statusText(); }
    // The privacy receipt now shares the presence tri-state, so re-paint it on the same trigger
    // (renderConsent only fires on LEVEL changes; this is the hook for presence-state changes).
    if(document.body){ _consentReceipts = _consentReceipts.filter(function(p){ return document.body.contains(p); }); }
    for(var i = 0; i < _consentReceipts.length; i++){ _consentReceipts[i].textContent = privacyReceiptText(); }
  }
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
  // ================= ITEM 1: ROLE INDICATOR (read-only reflection of the token's role) ==============
  // The pill states whether THIS device is an Admin (central) or Member (user) device. It is a pure
  // reflection of WAVR_MOBILE.role (resolved by detectRole from GET /api/devices) -- NEVER an actuator:
  // there is deliberately NO "become admin" button (privilege escalation is a Core-side grant only). null
  // (role not yet known) hides the pill rather than guessing. Member uses the neutral .tpill (accent is
  // reserved for presence); Admin gets the accent border via .wavrm-role-admin.
  var _roleUi = null;
  function roleLabel(){ return _role === "central" ? "Admin device" : _role === "user" ? "Member device" : ""; }
  function renderRolePill(){
    if(!_roleUi) return;
    var lab = roleLabel();
    if(!lab){ _roleUi.btn.hidden = true; return; }
    _roleUi.btn.hidden = false;
    _roleUi.txt.textContent = lab;
    _roleUi.btn.classList.toggle("wavrm-role-admin", _role === "central");
    _roleUi.btn.setAttribute("aria-label", lab + " — tap to learn what this means");
  }
  function injectRolePill(){
    if(!_token) return;
    var row = document.querySelector(".status-pills"); if(!row) return;
    if(document.getElementById("wavrm-role")){ renderRolePill(); return; }   // idempotent
    var b = el("button", "tpill"); b.id = "wavrm-role"; b.type = "button";
    b.setAttribute("data-tip", "Whether this device can manage your home (Admin) or only view it (Member). Set on your hub.");
    var txt = el("span", "p-txt", "");
    b.appendChild(txt);
    b.onclick = function(){ showRoleInfo(); };
    row.appendChild(b);
    _roleUi = { btn: b, txt: txt };
    renderRolePill();
  }
  function showRoleInfo(){
    var card = ensureOverlay("roleInfo");
    card.appendChild(el("h2", "wavrm-h", roleLabel() || "This device"));
    card.appendChild(el("p", "wavrm-sub", _role === "central"
      ? "This is an Admin device: it can view your home AND change settings (rooms, sensing, connectors)."
      : "This is a Member device: it can view your home, but can't change its settings."));
    card.appendChild(el("p", "wavrm-sub",
      "Your access level is set on your hub. To change it, ask the hub's owner to update this device on the hub's own screen. It can't be changed from here."));
    var back = el("button", "wavrm-btn ghost", "Back"); back.type = "button";
    back.onclick = function(){ hideOverlay(); };
    card.appendChild(back);
  }

  // Complement (iii) — one-glance per-device privacy receipt: exactly what THIS device shares RIGHT NOW,
  // derived from role + the current consent level. Honest and non-overclaiming; never names a data path
  // the device doesn't actually use. Colour matches the consent level via --consent-color.
  function privacyReceiptText(){
    if(_consent === "red") return "Right now this device shares nothing — it's turned off.";
    var who = (_role === "central") ? "As an admin device, it can also change home settings." : "";
    if(_consent === "yellow")
      return "Right now this device shares that someone is home from here — anonymously, with no name. " + who;
    // Green mirrors statusText's fail-closed tri-state (FIX-C2): the "shares your presence" CLAIM
    // fires ONLY on a hub-confirmed mac_registered:true. Otherwise the device is only SET to share:
    // say so plainly instead of claiming a presence the hub hasn't confirmed.
    if(_presenceConfirmed)
      return "Right now this device shares your presence at home as “" + (_presenceLabel || "this device") + "”. " + who;
    if(_presenceError)
      return "This device is set to share your presence at home, but your hub can't confirm it — no network presence. " + who;
    return "This device is set to share your presence at home as “" + (_presenceLabel || "this device") + "” — your hub hasn't confirmed it yet. " + who;
  }
  function injectReceipt(card){
    var p = el("p", "wavrm-receipt", privacyReceiptText());
    try{ p.style.setProperty("--consent-color", (CONSENT[_consent] || CONSENT.green).color); }catch(_){}
    card.appendChild(p);
    _consentReceipts.push(p);   // [A2] renderConsent re-paints this sentence in place on every level change
    return p;
  }

  // Details overlay: edit the presence label (re-asserts on save) and a deliberate Unpair (DELETE presence,
  // then wipe token/url/fp + reload back to setup). Unpair is NOT a security downgrade -- it removes the
  // pairing entirely; re-pairing requires the full out-of-band fingerprint verify again.
  function showDetails(){
    var card = ensureOverlay("details");
    card.appendChild(el("h2", "wavrm-h", "This device"));
    // Item 1: overlays cover the header role pill, so restate the role here.
    var rl = roleLabel(); if(rl) card.appendChild(el("p", "wavrm-sub", rl));
    // Complement (iii): the one-glance "what this device shares now" receipt.
    injectReceipt(card);
    // [A1] This overlay is the PERMANENT config home (reachable anytime from the status chip), so the
    // device-scope SENDING control lives here too — behaviour is changeable from the menu, not only the
    // header/tile. Same registered tri-color control (makeConsentControl -> _consentUis), so it stays in
    // lockstep with every other copy; renderConsent() below paints its colour+label + the receipt above.
    card.appendChild(el("span", "wavrm-lab", "This device — what it sends"));
    var sendRow = el("div", "wavrm-inrow");
    sendRow.appendChild(el("span", "wavrm-inrow-l", "Tap to reduce · hold to turn off"));
    sendRow.appendChild(makeConsentControl().btn);
    card.appendChild(sendRow);
    // Ghost: re-open the affirmative contribute chooser DIRECTLY (bypasses the once-only _contribOnboarded
    // gate — safe: its taps only call changeConsent + the idempotent markContribOnboarded). onDone returns
    // here; the "Not now"/red branch still routes to showOut by design (deliberate turn-off).
    var changeBtn = el("button", "wavrm-btn ghost", "Change what this device does"); changeBtn.type = "button";
    changeBtn.setAttribute("data-tip", "Re-open the choice of how much this device shares with your home.");
    changeBtn.onclick = function(){ showContributeOnboarding(function(){ showDetails(); }); };
    card.appendChild(changeBtn);
    renderConsent();   // paint the new control + receipt at their current level
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
    // Item 6: a dedicated connection/health check, composed on-device.
    var health = el("button", "wavrm-btn ghost", "Connection check"); health.type = "button";
    health.setAttribute("data-tip", "Checks whether this device and your hub are talking — reachability, certificate and presence.");
    health.onclick = function(){ showHealth(); };
    card.appendChild(health);
    // Complement (ii): the What's-New notes stay re-readable after first dismissal.
    var wn = el("button", "wavrm-btn ghost", "What's new"); wn.type = "button";
    wn.onclick = function(){ showWhatsNew(function(){ showDetails(); }); };
    card.appendChild(wn);
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
    unpair.setAttribute("data-tip", "Removes this pairing. You'll verify the hub's certificate again before you can reconnect.");
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

  // ================= ITEM 6: DEVICE + NETWORK HEALTH (composed on-device) ==========================
  // Honest, self-reported verdict from signals THIS device already tracks -- attachment, presence
  // (Core-confirmed only), pin match (WavrNet.probe vs the pinned fp), WS liveness (window.__wavrWsDown,
  // exposed by index.html's setReconnecting wrapper), and a live round-trip time. Reuses WavrNet.probe
  // for the TLS/reachability leg (no new native reachability method). For an Admin device it adds a
  // user-invoked "your home's internet" check against the real /api/health (control scope); a Member
  // device is honestly told to ask an admin device (never widening its egress boundary). Never logs.
  function hrow(list, label){
    var r = el("div", "wavrm-inrow");
    r.appendChild(el("span", "wavrm-inrow-l", label));
    var v = el("span", "wavrm-inrow-v", "…");
    r.appendChild(v); list.appendChild(r);
    return v;
  }
  function setV(v, text, cls){ if(!v) return; v.textContent = text; v.className = "wavrm-inrow-v" + (cls ? " " + cls : ""); }
  function showHealth(){
    var card = ensureOverlay("health");
    card.appendChild(el("h2", "wavrm-h", "Connection check"));
    // RED / detached: calm state, nothing to check (matches the "you turned this off" posture).
    if(!_attached){
      card.appendChild(el("p", "wavrm-sub", "You've turned this device off, so it isn't connected. Nothing to check. Re-enter from the coloured control to reconnect."));
      var b0 = el("button", "wavrm-btn ghost", "Back"); b0.type = "button"; b0.onclick = function(){ showDetails(); };
      card.appendChild(b0); return;
    }
    card.appendChild(el("p", "wavrm-sub", "How this device and " + (_coreName || "your hub") + " are getting along right now."));
    var list = el("div", "wavrm-field"); card.appendChild(list);
    var vReach = hrow(list, "Reachable");
    var vCert  = hrow(list, "Certificate");
    var vRtt   = hrow(list, "Round-trip");
    var vLive  = hrow(list, "Live updates");
    var vPres  = hrow(list, "Sharing presence");
    var verdict = el("p", "wavrm-msg", "Checking…"); card.appendChild(verdict);

    // Presence (already known locally; Core-confirmed only claims presence).
    if(_presenceError) setV(vPres, "No network presence", "warn");
    else if(_presenceConfirmed) setV(vPres, "Yes, as " + (_presenceLabel || "this device"), "ok");
    else setV(vPres, "Not confirmed", "");
    // WS liveness from index.html's wrapper (may be undefined on an older page -> "unknown").
    try{
      var down = window.__wavrWsDown;
      if(down === true) setV(vLive, "Reconnecting…", "warn");
      else if(down === false) setV(vLive, "Flowing", "ok");
      else setV(vLive, "—", "");
    }catch(_){ setV(vLive, "—", ""); }

    function finishVerdict(reachable, certOk, rtt){
      if(!reachable){ verdict.className = "wavrm-msg err"; verdict.textContent = "Can't reach your hub right now."; return; }
      if(certOk === false){ verdict.className = "wavrm-msg err"; verdict.textContent = "The hub's certificate doesn't match what you verified."; return; }
      if(rtt != null && rtt < 1500 && _attached && !_presenceError){
        verdict.className = "wavrm-msg"; verdict.textContent = "Your device and the hub are talking. All good.";
      } else {
        verdict.className = "wavrm-msg"; verdict.textContent = "Connected. Some checks are slow or unconfirmed — see above.";
      }
    }
    // TLS/reachability + pin leg (WavrNet.probe), then RTT via a real authed status read.
    var certOk = null, reachable = false, rtt = null;
    function afterProbe(){
      if(!(_token && _base && WavrNet && typeof WavrNet.request === "function")){
        setV(vRtt, "—", ""); finishVerdict(reachable, certOk, rtt); return;
      }
      var s0 = Date.now();
      netFetch(_base + "/api/status", { method: "GET", headers: { "Authorization": "Bearer " + _token } })
        .then(function(r){
          rtt = Date.now() - s0; reachable = reachable || !!(r && r.status);
          setV(vRtt, rtt + " ms", rtt < 1500 ? "ok" : "warn");
          if(reachable) setV(vReach, "Yes", "ok");
        }, function(){ setV(vRtt, "—", "err"); })
        .then(function(){ finishVerdict(reachable, certOk, rtt); });
    }
    if(WavrNet && typeof WavrNet.probe === "function"){
      WavrNet.probe({ url: _base }).then(function(r){
        var fp = (r && r.fingerprint) || null;
        reachable = !!fp;
        setV(vReach, fp ? "Yes" : "No", fp ? "ok" : "err");
        certOk = (fp && _pinnedFp) ? (normHex(fp) === normHex(_pinnedFp)) : null;
        setV(vCert, certOk === true ? "Verified" : certOk === false ? "Changed!" : "—",
             certOk === true ? "ok" : certOk === false ? "err" : "");
      }, function(){ reachable = false; setV(vReach, "No", "err"); setV(vCert, "—", ""); })
        .then(afterProbe);
    } else { setV(vReach, "—", ""); setV(vCert, "—", ""); afterProbe(); }

    // Admin: a user-invoked "your home's internet" leg against the real /api/health (never automatic --
    // /api/health pings public resolvers, an egress path). Member: honestly deferred to an admin device.
    if(_role === "central"){
      card.appendChild(el("span", "wavrm-lab", "Your home's internet"));
      var netFb = el("p", "wavrm-msg", "");
      var chk = el("button", "wavrm-btn ghost", "Check now"); chk.type = "button";
      chk.setAttribute("data-tip", "Asks your hub to test its internet — this also pings public DNS servers, so it runs only when you tap it.");
      chk.onclick = function(){
        chk.disabled = true; netFb.className = "wavrm-msg"; netFb.textContent = "Checking…";
        netFetch(_base + "/api/health", { method: "GET", headers: { "Authorization": "Bearer " + _token } })
          .then(function(r){ return (r && r.ok) ? r.json() : null; })
          .then(function(body){
            chk.disabled = false;
            if(!body){ netFb.className = "wavrm-msg err"; netFb.textContent = "Couldn't run the check."; return; }
            var sev = body.severity || "unknown";
            netFb.className = "wavrm-msg"; netFb.textContent = "Home internet: " + sev + ".";
          }, function(){ chk.disabled = false; netFb.className = "wavrm-msg err"; netFb.textContent = "Couldn't run the check."; });
      };
      card.appendChild(chk); card.appendChild(netFb);
    } else if(_role === "user"){
      card.appendChild(el("p", "wavrm-sub", "To check your home's internet, ask an admin device."));
    }
    var back = el("button", "wavrm-btn ghost", "Back"); back.type = "button";
    back.onclick = function(){ showDetails(); };
    card.appendChild(back);
  }

  // ================= COMPLEMENT (ii): WHAT'S-NEW CARD (version-gated, re-readable) =================
  function whatsNewPending(){ return !!(WHATS_NEW && WHATS_NEW.version && _seenVersion !== WHATS_NEW.version); }
  function markSeenVersion(){
    _seenVersion = (WHATS_NEW && WHATS_NEW.version) || "";
    secureSet(K_SEEN_VERSION, _seenVersion).catch(function(){});
  }
  // Shown BEFORE the dashboard on a version change; dismiss => don't reshow THAT version. onDone continues
  // to whatever comes next (the contribute gate / the dashboard, or back to details when re-read).
  function showWhatsNew(onDone){
    var card = ensureOverlay("whatsNew");
    card.appendChild(el("h2", "wavrm-h", (WHATS_NEW && WHATS_NEW.title) || "What's new"));
    if(WHATS_NEW && WHATS_NEW.version) card.appendChild(el("p", "wavrm-sub", "Version " + WHATS_NEW.version));
    var notes = (WHATS_NEW && WHATS_NEW.notes) || [];
    notes.forEach(function(n){ card.appendChild(el("p", "wavrm-sub", "• " + n)); });
    var ok = el("button", "wavrm-btn", "Got it"); ok.type = "button";
    ok.onclick = function(){ markSeenVersion(); if(typeof onDone === "function") onDone(); };
    card.appendChild(ok);
  }

  // ================= COMPLEMENT (ii): OTA "update available" chip (pinned, web-assets-only) =========
  // Poll /api/app/manifest over the PINNED transport on resume; if the hub advertises a newer WEB bundle
  // AND the sibling WavrUpdate plugin is present, surface an honest "Update available -> Apply & restart"
  // chip. The plugin does the pinned download + SHA-256 verify + safe-untar + next-launch apply; the shim
  // only signals. OTA NEVER carries the shim/lib/native (that's the code holding the pin -> ships via APK).
  // Absent the plugin, no chip is shown (the check is a no-op). Never logs versions/urls.
  var _otaUi = null, _otaSeenVersion = null, _otaInFlight = false;
  function updateAvailable(){ return !!(WavrUpdate && typeof WavrUpdate.download === "function"); }
  function pollManifest(){
    if(_otaInFlight || !updateAvailable() || !_token || !_base || !_attached) return;
    if(!(WavrNet && typeof WavrNet.request === "function")) return;
    _otaInFlight = true;
    netFetch(_base + "/api/app/manifest", { method: "GET", headers: { "Authorization": "Bearer " + _token } })
      .then(function(r){ return (r && r.ok) ? r.json() : null; })
      .then(function(m){
        _otaInFlight = false;
        var ver = m && m.version;
        if(!ver || !WavrUpdate) return;
        Promise.resolve(typeof WavrUpdate.current === "function" ? WavrUpdate.current() : null).then(function(cur){
          var active = (cur && cur.version) || null;
          if(active && String(active) === String(ver)) return;   // already on this bundle
          _otaSeenVersion = String(ver); injectOtaChip(m);
        }, function(){ _otaSeenVersion = String(ver); injectOtaChip(m); });
      }, function(){ _otaInFlight = false; });
  }
  function injectOtaChip(manifest){
    if(!_token) return;
    var row = document.querySelector(".status-pills"); if(!row) return;
    if(!document.getElementById("wavrm-ota")){
      var b = el("button", "tpill", ""); b.id = "wavrm-ota"; b.type = "button";
      var dot = el("i", "p-dot"); var txt = el("span", "p-txt", "Update available");
      try{ dot.style.background = "var(--warn,#e8a13a)"; }catch(_){}
      b.appendChild(dot); b.appendChild(txt);
      b.setAttribute("data-tip", "A newer version of the app screens is ready on your hub. Downloads over your verified, pinned connection and applies on next launch.");
      b.onclick = function(){ applyOta(manifest); };
      row.appendChild(b);
      _otaUi = b;
    }
    _otaUi.hidden = false;
  }
  function applyOta(manifest){
    if(!(WavrUpdate && typeof WavrUpdate.download === "function")) return;
    // [G] Guard: the FROZEN download contract needs {url, sha256, size, version}. Missing any field ->
    // do not attempt (a half-formed manifest would only reject with INVALID_ARGS).
    if(!(manifest && manifest.version && manifest.sha256 && manifest.size)){
      if(_otaUi){ _otaUi.querySelector(".p-txt").textContent = "Update failed"; _otaUi.disabled = false; }
      return;
    }
    // Resolve the bundle URL to an ABSOLUTE pinned-central URL (manifest.url may be relative or absent).
    // The plugin refuses any host:port other than the stored central's — this only fills the host in.
    var burl = (manifest && manifest.url) || "/api/app/bundle";
    if(burl.charAt(0) === "/") burl = _base + burl;
    if(_otaUi){ _otaUi.querySelector(".p-txt").textContent = "Updating…"; _otaUi.disabled = true; }
    Promise.resolve(WavrUpdate.download({ url: burl, sha256: manifest.sha256, size: manifest.size, version: manifest.version }))
      .then(function(){
        return typeof WavrUpdate.apply === "function" ? WavrUpdate.apply({ version: manifest.version }) : null;   // next-launch activation
      }).then(function(){
        if(_otaUi){ _otaUi.querySelector(".p-txt").textContent = "Restart to finish"; _otaUi.disabled = false; }
      }, function(){
        if(_otaUi){ _otaUi.querySelector(".p-txt").textContent = "Update failed"; _otaUi.disabled = false; }
      });
  }

  // ================= ITEM 3: FIRST-OPEN CONTRIBUTE ONBOARDING (affirmative-tap only) ================
  function markContribOnboarded(){ _contribOnboarded = true; secureSet(K_CONTRIB_ONBOARDED, "1").catch(function(){}); }
  function choiceCard(title, sub, primary){
    var b = el("button", "wavrm-choice" + (primary ? " primary" : "")); b.type = "button";
    b.appendChild(el("span", "wavrm-choice-mark", ""));
    b.appendChild(el("div", "wavrm-choice-t", title));
    b.appendChild(el("div", "wavrm-choice-s", sub));
    return b;
  }
  // Shown ONCE (K_CONTRIB_ONBOARDED) before the dashboard. Nothing is registered until the user TAPS a
  // choice (no silent default-green enrolment). Undo is the persistent tri-color control + the note; a
  // "Not now" tap sets red (which routes to showOut via changeConsent). onDone reveals the dashboard.
  function showContributeOnboarding(onDone){
    var card = ensureOverlay("contribute");
    card.appendChild(el("h2", "wavrm-h", "Help your home know who's in?"));
    card.appendChild(el("p", "wavrm-sub",
      "This device can add its presence so your home knows when you're home. You choose how much, and you can change or turn it off anytime."));
    var cGreen = choiceCard("Help sense who's home", "Wavr uses this phone's presence and names it at home.", true);
    cGreen.setAttribute("data-tip", "Full participation — your presence counts and is named at home.");
    cGreen.onclick = function(){ markContribOnboarded(); changeConsent("green"); if(typeof onDone === "function") onDone(); };
    var cYellow = choiceCard("Stay present but anonymous", "Counted as home, without a name. Minimal data.", false);
    cYellow.setAttribute("data-tip", "Limited — present but anonymous; minimal data.");
    cYellow.onclick = function(){ markContribOnboarded(); changeConsent("yellow"); if(typeof onDone === "function") onDone(); };
    var cRed = choiceCard("Not now", "Don't contribute from this device. Turn it on anytime from the control at the top.", false);
    cRed.setAttribute("data-tip", "Off — this device contributes nothing until you turn it on.");
    cRed.onclick = function(){ markContribOnboarded(); changeConsent("red"); };   // red routes to showOut itself
    card.appendChild(cGreen); card.appendChild(cYellow); card.appendChild(cRed);
    card.appendChild(el("p", "wavrm-sub", "You can change this anytime — tap the coloured control, or hold it to turn off."));
  }

  // ================= ITEM 2: DEVICE-CONSENT ROW MOUNTED INTO #sensingLevelTile ======================
  // The consent tri-color is the device-scope participation control. It lives WITH the hub's sensing tile
  // (index.html's #sensingLevelTile shows the HOME's read-only level; this row is "this device"), so the
  // two are read together. Registered in _consentUis via makeConsentControl, so its colour+label stay in
  // lockstep with the header pill (item 4). isNative-guarded; the tile is untouched in web modes.
  function injectConsentTile(){
    if(!_token) return;
    var tile = document.getElementById("sensingLevelTile"); if(!tile) return;
    if(document.getElementById("wavrm-consent-tile")){ renderConsent(); return; }   // idempotent
    var box = el("div", ""); box.id = "wavrm-consent-tile";
    // [A2] OWN sub-head in SENDING vocab, visually separated so it doesn't read as part of the admin house
    // control above. The tile shows how much of your HOME Wavr senses (admin-only, read-only here); THIS
    // box is device-scope — what THIS device sends about you.
    box.appendChild(el("span", "wavrm-ct-lab", "This device — what it sends"));
    box.appendChild(el("p", "wavrm-sub",
      "The level above is how much of your home Wavr senses. This is what THIS device sends about you."));
    var row = el("div", "wavrm-inrow");
    row.appendChild(el("span", "wavrm-inrow-l", "Tap to reduce · hold to turn off"));
    row.appendChild(makeConsentControl().btn);
    box.appendChild(row);
    injectReceipt(box);   // [A2] live "shares … / shares nothing" sentence directly under the control;
                          // registered so renderConsent re-paints it in place on every level change
    tile.appendChild(box);
    renderConsent();
  }

  // Reveal the dashboard viewer chrome: confirm the hub is reachable (connectPinned), then inject the
  // header pills. connectPinned keeps the overlay up on failure instead of the frozen default house.
  // withSensorPill = the combo (sensor+viewer) device also gets the compact contribute pill.
  function revealDashboard(withSensorPill){
    connectPinned(function(){ hideOverlay(); });
    if(withSensorPill) injectSensorPill();
    injectConsentPill();   // consent toggle in the header chrome (participation != permission)
    injectStatusChip();    // Task 6: attachment + presence chip
    injectRolePill();      // item 1: Admin/Member indicator
    injectConsentTile();   // item 2: device-consent row inside #sensingLevelTile (with the hub level)
    pollManifest();        // complement (ii): check for a newer web bundle over the pinned transport
    // [G] Confirm an applied OTA bundle is healthy now that the dashboard chrome is up, so a good bundle
    // is NOT auto-reverted on a later launch. Idempotent + a no-op when nothing is pending.
    try{ if(WavrUpdate && typeof WavrUpdate.markLaunchOk === "function") WavrUpdate.markLaunchOk(); }catch(_){}
  }
  // First-open gates run BEFORE the dashboard, in order: What's-New (on a version change) -> contribute
  // onboarding (once). Each is affirmative and dismissible; only then is the dashboard revealed. The
  // contribute "Not now" path routes to showOut itself (red), so revealDashboard is skipped there.
  function gateThenDashboard(withSensorPill){
    var proceed = function(){ revealDashboard(withSensorPill); };
    var afterWhatsNew = function(){
      if(_token && !_contribOnboarded){ showContributeOnboarding(proceed); }
      else proceed();
    };
    if(whatsNewPending()) showWhatsNew(afterWhatsNew); else afterWhatsNew();
  }

  // ----- Boot: load caches, resolve the gate, then decide which screen to show -----
  function decideScreen(){
    if(_screenDecided) return;   // #21: idempotent -- run at most once (Keystore-settled path OR the 4s wedge timer)
    _screenDecided = true;
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
          // sensor + viewer/admin: the dashboard is PRIMARY; inject the compact "this device" sensor pill
          // into the header chrome. The wizard is deferred to the pill's first Start (ensureOnboardedThen)
          // so viewing is never blocked. The gates below (What's-New, contribute) run BEFORE the dashboard.
          gateThenDashboard(true);
        } else {
          // viewer / admin / migrated (token, no caps): the dashboard boots exactly as today once the hub is
          // CONFIRMED reachable, after the same first-open gates.
          gateThenDashboard(false);
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
        if(_base) showScanPair(); else showChooseCore();
      }
    });
  }

  Promise.all([secureGet(K_URL), secureGet(K_FP), secureGet(K_TOKEN),
               secureGet(K_DEVICE_ID), secureGet(K_ROLE),
               secureGet(K_CAPS), secureGet(K_ONBOARDED), secureGet(K_CONSENT),
               secureGet(K_PRESENCE_LABEL), secureGet(K_CORE_NAME),
               secureGet(K_CONTRIB_ONBOARDED), secureGet(K_SEEN_VERSION)]).then(function(v){
    _base = v[0] || ""; _pinnedFp = v[1] || null; _token = v[2] || null;
    _deviceId = v[3] || null; _role = v[4] || null;   // role read synchronously at boot, like the token
    _caps = parseCaps(v[5]); _onboarded = (v[6] === "1");   // caps + onboarded read synchronously too
    _consent = normConsent(v[7]);   // consent level read synchronously -> the toggle's boot colour is correct
    _presenceLabel = v[8] || ""; _coreName = v[9] || "";   // Task 6: label + Core name for the status chip
    _contribOnboarded = (v[10] === "1"); _seenVersion = v[11] || "";   // item 3 + complement (ii) gates
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
