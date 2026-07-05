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
  function tokenGet(){ try{ return _token; }catch(_){ return null; } }
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
      "@keyframes wavrm-rot{to{transform:rotate(360deg);}}";
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

  // ----- Boot: load caches, resolve the gate, then decide which screen to show -----
  function decideScreen(){
    whenDom(function(){
      injectStyle();
      if(_token){
        // Have a token: index.html boots the read-only viewer. Clear the pairing chrome class that
        // index.html sets at parse time (token cache was empty then) and mask the boot flash.
        try{ document.body.classList.remove("pairing-mode"); }catch(_){}
        showConnecting("Connecting to your home…");
        setTimeout(function(){ if(_screen === "connecting") hideOverlay(); }, 1200);
      } else if(_base && _pinnedFp){
        // Verified central but no token (first-run resume after verify, or a 401/403 revoke):
        // jump straight to the 8-digit code entry. No setup/verify re-prompt.
        revealCodeEntry(); hideOverlay();
      } else {
        // Nothing (or verify incomplete): native setup, then out-of-band verify.
        if(_base) showVerify(); else showSetup();
      }
    });
  }

  Promise.all([secureGet(K_URL), secureGet(K_FP), secureGet(K_TOKEN),
               secureGet(K_DEVICE_ID), secureGet(K_ROLE)]).then(function(v){
    _base = v[0] || ""; _pinnedFp = v[1] || null; _token = v[2] || null;
    _deviceId = v[3] || null; _role = v[4] || null;   // role read synchronously at boot, like the token
  }).catch(function(){}).then(function(){
    _resolveReady();   // caches populated: index.html's deferred boot may run now
    if(_token) detectRole();   // once after ready + token exists; fire-and-forget, never blocks boot
    decideScreen();
  });
})();
