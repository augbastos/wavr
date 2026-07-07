# Wavr Mobile ↔ Core Attachment — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Wavr Mobile companion attach to the Core via mDNS discovery + one-time-code pairing, then run the everyday enter/exit + auto-presence flow off the existing green/yellow/red consent control — no camera/QR yet.

**Architecture:** All logic lands in `mobile/src/wavr-mobile-shim.js` (the single mobile-specific IIFE), plus a NEW pure-logic seam `mobile/src/wavr-lib.js` (UMD, unit-tested with `node --test`) that the shim consumes. Discovery uses a null-guarded `capacitor-zeroconf` native plugin. The consent control is rewired to drive connection (a WS lever over the existing `netWebSocket`/token-visibility mechanism) and presence (`register-companion` POST/DELETE). Zero `index.html` edits.

**Tech Stack:** Vanilla browser JS (ES5-style `var`/IIFE to match the shim), Capacitor 8, `capacitor-zeroconf` (native), Node 22 built-in test runner (`node --test`) for pure helpers, existing backend pytest for the contract.

## Global Constraints

- **Zero `index.html` edits** — all chrome + the connect/disconnect lever go through the shim (token-visibility + socket control). Copied verbatim from the spec invariant.
- **Local-only** — the sole network peer is the attached Core, over the pinned transport (`netFetch`/`netWebSocket` → WavrNet) with the device token.
- **Fail-closed** — never show presence/attachment the Core did not confirm; RED is a real exit.
- **Never log** the token or the label (no `console.*` of either).
- **Match the shim's style** — `var`, function declarations, `"use strict"`, null-guard every native plugin call (mirror the existing `WavrSensor` handling), never `innerHTML` with cert/user data (use `textContent`).
- **Core contract (already exists):** `POST /api/pair-code`, `POST /api/pair {code, device_name}`, `POST|DELETE /api/presence/register-companion {label}` → `{mac_registered, label, mac_prefix}`, `POST /api/ws-ticket` → `GET /ws/live?ticket=…`, mDNS `_wavr._tcp` port 8000 TXT `{v=1, role=core, path=/?core}`.

---

## File Structure

- **Create** `mobile/src/wavr-lib.js` — pure, DOM-free, native-free helpers: `isHost`, `isPort`, `parseCoreService`, `consentToActions`. UMD export so both the shim (`window.WavrLib`) and node tests (`require`) use it.
- **Create** `mobile/test/wavr-lib.test.js` — `node --test` unit tests for the pure helpers.
- **Modify** `mobile/package.json` — add `"test": "node --test test/"`.
- **Modify** `mobile/scripts/sync-frontend.mjs` — copy `wavr-lib.js` into `www/` and inject `<script src="wavr-lib.js">` BEFORE the shim script.
- **Modify** `mobile/src/wavr-mobile-shim.js` — discovery screen, consent-lever rewiring, presence register/DELETE + re-assert, label capture, status chip + details overlay, boot auto-attach.
- **Coordination (not a code task here):** `capacitor-shell-engineer` installs `capacitor-zeroconf` + adds the Android service-discovery permission. The shim null-guards its absence, so tasks below are testable (degrade to manual entry) before the native plugin lands.

---

## Task 1: Pure-logic seam + test harness

**Files:**
- Create: `mobile/src/wavr-lib.js`
- Create: `mobile/test/wavr-lib.test.js`
- Modify: `mobile/package.json` (add `test` script)

**Interfaces:**
- Produces:
  - `isHost(h: string) → bool`
  - `isPort(p: string|number) → bool`
  - `parseCoreService(r: object) → {name, host, port}|null` — validates a zeroconf result, requires TXT `role==="core"`, a valid host, a valid port; returns null otherwise.
  - `consentToActions(level: "green"|"yellow"|"red") → {attached: bool, presence: "register"|"delete", contribution: "full"|"limited"|"none"}`

- [ ] **Step 1: Write the failing tests**

Create `mobile/test/wavr-lib.test.js`:

```js
const { test } = require("node:test");
const assert = require("node:assert");
const { isHost, isPort, parseCoreService, consentToActions } = require("../src/wavr-lib.js");

test("isPort accepts 1..65535, rejects others", () => {
  assert.equal(isPort("8000"), true);
  assert.equal(isPort("1"), true);
  assert.equal(isPort("65535"), true);
  assert.equal(isPort("0"), false);
  assert.equal(isPort("70000"), false);
  assert.equal(isPort("abc"), false);
});

test("isHost accepts IPv4 and hostnames, rejects junk", () => {
  assert.equal(isHost("192.168.1.50"), true);
  assert.equal(isHost("wavr-core.local"), true);
  assert.equal(isHost("bad host"), false);
  assert.equal(isHost(""), false);
});

test("parseCoreService keeps role=core with valid host/port", () => {
  const r = { name: "Wavr Core", hostname: "192.168.1.50", port: 8000, txtRecord: { v: "1", role: "core" } };
  assert.deepEqual(parseCoreService(r), { name: "Wavr Core", host: "192.168.1.50", port: 8000 });
});

test("parseCoreService rejects non-core / invalid", () => {
  assert.equal(parseCoreService({ name: "x", hostname: "192.168.1.50", port: 8000, txtRecord: { role: "printer" } }), null);
  assert.equal(parseCoreService({ name: "x", hostname: "bad host", port: 8000, txtRecord: { role: "core" } }), null);
  assert.equal(parseCoreService({ name: "x", hostname: "192.168.1.50", port: 0, txtRecord: { role: "core" } }), null);
  assert.equal(parseCoreService(null), null);
});

test("consentToActions maps the three levels", () => {
  assert.deepEqual(consentToActions("green"),  { attached: true,  presence: "register", contribution: "full" });
  assert.deepEqual(consentToActions("yellow"), { attached: true,  presence: "register", contribution: "limited" });
  assert.deepEqual(consentToActions("red"),    { attached: false, presence: "delete",   contribution: "none" });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mobile && node --test test/`
Expected: FAIL with `Cannot find module '../src/wavr-lib.js'`.

- [ ] **Step 3: Implement the pure lib**

Create `mobile/src/wavr-lib.js`:

```js
/* Wavr pure-logic helpers — DOM-free, native-free, so node --test can exercise them and the
 * shim can consume them as window.WavrLib. No console, no side effects. */
(function(root){
  "use strict";
  function isPort(p){ var s = String(p); return /^\d{1,5}$/.test(s) && (+s) >= 1 && (+s) <= 65535; }
  function isHost(h){
    if(typeof h !== "string" || !h) return false;
    return /^(\d{1,3})(\.\d{1,3}){3}$/.test(h) || /^[a-zA-Z0-9][a-zA-Z0-9.\-]{0,253}$/.test(h);
  }
  // Validate one zeroconf/NSD result. Accept only role=core with a usable host+port.
  // Accepts either {hostname|host|ipv4Addresses[0]} and {txtRecord|txt} shapes defensively.
  function parseCoreService(r){
    if(!r || typeof r !== "object") return null;
    var txt = r.txtRecord || r.txt || {};
    if(String(txt.role || "").toLowerCase() !== "core") return null;
    var host = r.hostname || r.host || (Array.isArray(r.ipv4Addresses) && r.ipv4Addresses[0]) || "";
    var port = r.port;
    if(!isHost(host) || !isPort(port)) return null;
    var name = (typeof r.name === "string" && r.name.trim()) ? r.name.trim() : "Wavr Core";
    return { name: name, host: host, port: +port };
  }
  // The single source of truth for what each consent level means for connection + presence.
  function consentToActions(level){
    if(level === "red")    return { attached: false, presence: "delete",   contribution: "none" };
    if(level === "yellow") return { attached: true,  presence: "register", contribution: "limited" };
    return { attached: true, presence: "register", contribution: "full" };   // green / default
  }
  var api = { isHost: isHost, isPort: isPort, parseCoreService: parseCoreService, consentToActions: consentToActions };
  if(typeof module !== "undefined" && module.exports) module.exports = api;
  else root.WavrLib = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
```

- [ ] **Step 4: Add the test script**

In `mobile/package.json`, add to `"scripts"`:

```json
"test": "node --test test/"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd mobile && npm test`
Expected: PASS, 5 tests, 0 fail.

- [ ] **Step 6: Commit**

```bash
git add mobile/src/wavr-lib.js mobile/test/wavr-lib.test.js mobile/package.json
git commit -m "feat(mobile): pure-logic lib (consent->actions, mDNS parse) + node --test harness"
```

---

## Task 2: sync-frontend injects wavr-lib.js

**Files:**
- Modify: `mobile/scripts/sync-frontend.mjs`

**Interfaces:**
- Consumes: `mobile/src/wavr-lib.js` (Task 1).
- Produces: `www/wavr-lib.js` present, and `www/index.html` loads `<script src="wavr-lib.js">` BEFORE the `wavr-mobile-shim.js` script (so `window.WavrLib` exists when the shim IIFE runs).

- [ ] **Step 1: Read the current injection point**

Run: `grep -n "wavr-mobile-shim\|injecting shim\|<script" mobile/scripts/sync-frontend.mjs`
Expected: find where the shim `<script>` tag string is injected (the log line "injecting shim <script> before the main inline app script").

- [ ] **Step 2: Add the lib copy + script injection**

In `sync-frontend.mjs`, alongside the existing shim copy, copy the lib and inject its tag immediately BEFORE the shim tag. The two injected tags must end up in this order in `www/index.html`:

```html
<script src="wavr-lib.js"></script>
<script src="wavr-mobile-shim.js"></script>
```

Concretely: wherever the script copies `mobile/src/wavr-mobile-shim.js` → `www/wavr-mobile-shim.js`, add the same for `wavr-lib.js`; and wherever it builds the injected `<script src="wavr-mobile-shim.js"></script>` string, prepend `<script src="wavr-lib.js"></script>` to it. Add a matching `[sync-frontend] copied mobile/src/wavr-lib.js -> www/wavr-lib.js` log line.

- [ ] **Step 3: Run sync and verify output**

Run: `cd mobile && npm run sync-frontend`
Then: `ls www/wavr-lib.js && grep -n "wavr-lib.js" www/index.html`
Expected: `www/wavr-lib.js` exists; `www/index.html` contains the `wavr-lib.js` script tag on the line immediately before the `wavr-mobile-shim.js` tag.

- [ ] **Step 4: Commit**

```bash
git add mobile/scripts/sync-frontend.mjs
git commit -m "build(mobile): sync-frontend copies + injects wavr-lib.js before the shim"
```

---

## Task 3: mDNS discovery + "choose your Core" screen

**Files:**
- Modify: `mobile/src/wavr-mobile-shim.js`

**Interfaces:**
- Consumes: `WavrLib.parseCoreService` (Task 1); existing `showSetup()`, `showVerify()`, `ensureOverlay()`, `el()`.
- Produces: `showChooseCore()` overlay; a null-guarded `Zeroconf` plugin handle; discovery feeds `_base = "https://" + host + ":" + port` then calls `showVerify()`.

- [ ] **Step 1: Add the plugin handle (near the other plugin handles ~line 43)**

```js
// mDNS/DNS-SD browse for Cores advertising _wavr._tcp. ABSENT on a build without the native
// plugin -> discovery is skipped and the user falls to manual entry (never throws).
var Zeroconf = plugin("Zeroconf");
function zeroconfAvailable(){ return !!(Zeroconf && typeof Zeroconf.watch === "function"); }
```

- [ ] **Step 2: Add the discovery screen**

Add `showChooseCore()` near `showSetup()` (~line 476). It browses `_wavr._tcp`, lists parsed Cores, and offers manual entry. Each tap sets `_base` and goes to verify:

```js
// ----- Discovery: browse _wavr._tcp, let the user pick a Core. Manual entry always available. -----
function showChooseCore(){
  var card = ensureOverlay("chooseCore");
  card.appendChild(el("h2", "wavrm-h", "Find your Wavr Core"));
  card.appendChild(el("p", "wavrm-sub", "Looking for Cores on your Wi-Fi…"));
  var list = el("div", "wavrm-field"); card.appendChild(list);
  var manual = el("button", "wavrm-btn ghost", "Enter address manually"); manual.type = "button";
  manual.onclick = function(){ try{ stopCoreWatch(); }catch(_){} showSetup(); };
  card.appendChild(manual);
  var seen = {};
  function addCore(svc){
    var core = WavrLib.parseCoreService(svc); if(!core) return;
    var key = core.host + ":" + core.port; if(seen[key]) return; seen[key] = true;
    var b = el("button", "wavrm-choice primary"); b.type = "button";
    b.appendChild(el("div", "wavrm-choice-t", core.name));
    b.appendChild(el("div", "wavrm-choice-s", core.host + ":" + core.port));
    b.onclick = function(){
      try{ stopCoreWatch(); }catch(_){}
      _base = "https://" + core.host + ":" + core.port;
      _pendingCoreName = core.name;            // remembered for the pairing store (Task 6)
      showVerify();
    };
    list.appendChild(b);
  }
  if(!zeroconfAvailable()){
    card.appendChild(el("p", "wavrm-msg", "Automatic discovery isn't available on this device — enter the address."));
    return;
  }
  startCoreWatch(addCore);
}
var _coreWatchActive = false;
function startCoreWatch(onFound){
  if(_coreWatchActive) return; _coreWatchActive = true;
  try{
    Zeroconf.watch({ type: "_wavr._tcp.", domain: "local." }, function(res){
      // Capacitor delivers {action:"resolved"|"added", service:{...}}; guard both shapes.
      var svc = (res && (res.service || res)) || null;
      if(svc && (!res.action || res.action === "resolved")) onFound(svc);
    });
  }catch(_){ _coreWatchActive = false; }
}
function stopCoreWatch(){
  _coreWatchActive = false;
  try{ if(Zeroconf && typeof Zeroconf.unwatch === "function") Zeroconf.unwatch({ type: "_wavr._tcp.", domain: "local." }); }catch(_){}
  try{ if(Zeroconf && typeof Zeroconf.stop === "function") Zeroconf.stop(); }catch(_){}
}
```

Add the module-level declaration near the other caches (~line 109): `var _pendingCoreName = null;`

- [ ] **Step 3: Route first-launch-after-chooser into discovery**

In `decideScreen()` (~line 1100), change the "true first launch" branch so that after the capability chooser it lands on discovery instead of straight to manual setup. Find the branch that calls `showChooser()` and the later `showSetup()` fallback; the chooser's Continue (`showChooser`'s `cont.onclick`, ~line 801) currently calls `showSetup()` — change that single call to `showChooseCore()`:

```js
// in showChooser() cont.onclick success handler:
persistCaps({ sensor: sel.sensor, viewer: sel.viewer, admin: sel.admin }).then(function(){
  showChooseCore();
}, function(){ /* unchanged error path */ });
```

Also in `decideScreen()`'s final else (`if(_base) showVerify(); else showSetup();`, ~line 1109) change `showSetup()` → `showChooseCore()` so a caps-chosen-but-unpaired resume offers discovery first.

- [ ] **Step 4: On-device drill — discovery lists the Core**

Preconditions: the Core (G9) advertising `_wavr._tcp` on the LAN; `capacitor-zeroconf` installed in the build; app freshly installed (no token).
Drive: launch app → pick a capability → the "Find your Wavr Core" screen appears.
Observe (PASS): the Core shows in the list as "Wavr Core · <host>:8000"; tapping it opens the fingerprint-verify screen with that host. "Enter address manually" opens the IP form.
Fallback check (PASS): on a build WITHOUT the plugin, the screen shows the "discovery isn't available" line and manual entry still works.

- [ ] **Step 5: Commit**

```bash
git add mobile/src/wavr-mobile-shim.js
git commit -m "feat(mobile): mDNS 'choose your Core' discovery screen (null-guarded, manual fallback)"
```

---

## Task 4: Consent-as-enter/exit — the connection lever

**Files:**
- Modify: `mobile/src/wavr-mobile-shim.js`

**Interfaces:**
- Consumes: `WavrLib.consentToActions` (Task 1); existing `_socks`, `netWebSocket()`, `changeConsent()`, `applyConsentLocal()`, `showConnecting()`, `hideOverlay()`, `tokenGet()`.
- Produces: module var `_attached` (bool); `applyAttachment(level)` that opens/closes the WS lever; an "Out" overlay `showOut()`. `netWebSocket` refuses to open while detached.

- [ ] **Step 1: Add the attachment state + lever helpers (near the consent code ~line 934)**

```js
// Connection lever driven by the consent level (green/yellow = attached, red = out). We never
// edit index.html: to DISCONNECT we close live sockets and make netWebSocket refuse to open, so
// index.html's reconnect loop keeps getting dead sockets and stays down; to RECONNECT we re-allow
// opening and reload so the provider reconstructs. Token is KEPT throughout (re-enter is one tap).
var _attached = true;   // default; boot sets it from the stored level (Task 7)
function closeLiveSockets(){
  for(var id in _socks){ if(Object.prototype.hasOwnProperty.call(_socks, id)){
    try{ if(WavrNet && typeof WavrNet.closeSocket === "function") WavrNet.closeSocket({ socketId: id }); }catch(_){}
  }}
  _socks = {};
}
// ----- "Out" overlay: shown while RED/detached. The consent pill (still injected) flips back to
// green/yellow to re-enter; this overlay just makes "you left" unmistakable. -----
function showOut(){
  var card = ensureOverlay("out");
  card.appendChild(el("h2", "wavrm-h", "You've left Wavr"));
  card.appendChild(el("p", "wavrm-sub",
    "This device isn't connected and isn't sharing presence. Set the toggle back to green or yellow to re-enter."));
}
```

- [ ] **Step 2: Gate `netWebSocket` while detached**

At the very top of `netWebSocket(url)` (~line 176), before building `sock`:

```js
function netWebSocket(url){
  if(!_attached){
    var dead = { onmessage:null, onclose:null, onerror:null, __id:null, send:function(){}, close:function(){} };
    setTimeout(function(){ if(typeof dead.onclose === "function") dead.onclose({ code:1000, reason:"detached" }); }, 0);
    return dead;
  }
  // …existing body unchanged…
```

- [ ] **Step 3: Add `applyAttachment` and call it from the consent flow**

Add near the lever helpers:

```js
// Enact the connection side of a consent level. Presence (register/DELETE) is Task 5's job,
// called right after this. Returns nothing; never throws.
function applyAttachment(level){
  var act = WavrLib.consentToActions(level);
  if(act.attached){
    if(!_attached){ _attached = true; hideOverlay(); try{ location.reload(); }catch(_){} }
    // already attached (green<->yellow): no socket churn, presence contribution level changes only
  } else {
    _attached = false; closeLiveSockets(); showOut();
  }
}
```

In `applyConsentLocal(level)` (~line 957), the existing `if(level === "red"){ stopSensor(); }` line becomes the hook point — call the lever there for ALL levels:

```js
function applyConsentLocal(level){
  _consent = level;
  applyAttachment(level);                         // NEW: drive the connection lever
  if(level === "red"){ try{ stopSensor(); }catch(_){} }
  renderConsent();
  return secureSet(K_CONSENT, level).catch(function(){});
}
```

- [ ] **Step 4: Make `tokenGet` respect detachment**

In `tokenGet()` (~line 220), extend the existing guard so a detached device also hides the token (so a reload lands on the inert NullProvider):

```js
function tokenGet(){
  try{
    if(capsSensorOnly()) return null;
    if(!_attached) return null;   // RED/out: hide token so index.html boots NullProvider (no WS)
    return _token;
  }catch(_){ return null; }
}
```

- [ ] **Step 5: On-device drill — red exits, green re-enters**

Precondition: a device paired + attached (dashboard live).
Drive: tap the consent pill to RED (or hold 2s).
Observe (PASS): live data stops; the "You've left Wavr" overlay shows; the pill is red. Set the pill back to GREEN → the app reloads, reconnects, dashboard is live again — NO code re-entry.
Regression (PASS): GREEN↔YELLOW does not tear down/rebuild the socket (no reconnect flash) — only the contribution level changes.

- [ ] **Step 6: Commit**

```bash
git add mobile/src/wavr-mobile-shim.js
git commit -m "feat(mobile): consent level drives the connect/disconnect lever (red = real exit)"
```

---

## Task 5: Auto-presence register / DELETE + re-assert

**Files:**
- Modify: `mobile/src/wavr-mobile-shim.js`

**Interfaces:**
- Consumes: `netFetch()`, `_token`, `_base`, `WavrLib.consentToActions`; the `_presenceLabel` cache (Task 6) — until Task 6 lands, default `_presenceLabel` to `""`.
- Produces: `registerPresence()`, `unregisterPresence()`, `applyPresence(level)`, re-assert wiring; `_presenceError` state for the fail-closed message.

- [ ] **Step 1: Add the presence caches + calls (near the consent code)**

```js
// Network presence: on ENTER we POST the label; the Core resolves our source IP -> MAC (we can't
// read our own MAC on Android 10+). On EXIT we DELETE. Fail-closed: mac_registered:false means the
// Core can't do network presence -> surface it, don't claim presence. Never logs the token/label.
var _presenceError = false;        // true when the Core said mac_registered:false
var _presenceInFlight = false;
function registerPresence(){
  if(!_token || !_base || _presenceInFlight) return;
  _presenceInFlight = true;
  netFetch(_base + "/api/presence/register-companion", {
    method: "POST",
    headers: { "Authorization": "Bearer " + _token, "Content-Type": "application/json" },
    body: JSON.stringify({ label: _presenceLabel || "" })
  }).then(function(r){
    return (r && r.ok) ? r.json() : null;
  }).then(function(body){
    _presenceError = !!(body && body.mac_registered === false);   // explicit false -> Core can't
    renderStatusChip();                                            // Task 6 (no-op if absent)
  }).catch(function(){ /* transient: keep last state, a later re-assert retries */ })
    .then(function(){ _presenceInFlight = false; });
}
function unregisterPresence(){
  if(!_token || !_base) return;
  netFetch(_base + "/api/presence/register-companion", {
    method: "DELETE",
    headers: { "Authorization": "Bearer " + _token }
  }).catch(function(){ /* best-effort; leaving locally is what matters */ });
}
// Enact the presence side of a level (paired with applyAttachment).
function applyPresence(level){
  var act = WavrLib.consentToActions(level);
  if(act.presence === "register") registerPresence();
  else { _presenceError = false; unregisterPresence(); }
}
```

If Task 6 is not yet merged, add a temporary `var _presenceLabel = "";` and a no-op `function renderStatusChip(){}` near the caches so this task is self-contained; Task 6 replaces both.

- [ ] **Step 2: Call presence from the consent flow**

In `applyConsentLocal(level)`, add the presence call right after the attachment lever:

```js
function applyConsentLocal(level){
  _consent = level;
  applyAttachment(level);
  applyPresence(level);              // NEW: register on green/yellow, DELETE on red
  if(level === "red"){ try{ stopSensor(); }catch(_){} }
  renderConsent();
  return secureSet(K_CONSENT, level).catch(function(){});
}
```

- [ ] **Step 3: Re-assert on foreground / reconnect / periodic**

Extend the existing `visibilitychange` handler (~line 324) and the socket-open path (~line 188) to re-assert, and add a 30-min timer. Near the presence code:

```js
function reassertPresence(){
  if(WavrLib.consentToActions(_consent).presence === "register") registerPresence();
}
setInterval(function(){
  if(document.visibilityState === "visible") reassertPresence();
}, 30 * 60 * 1000);
```

In the existing `visibilitychange` listener add `reassertPresence();` next to the `detectRole()` call. In `netWebSocket`'s `openSocket().then(...)` success (~line 187, next to `detectRole()`), add `reassertPresence();`.

- [ ] **Step 4: On-device drill — presence lights up and clears**

Precondition: device paired to the Core (G9), Core does network presence (has ARP).
Drive: set consent GREEN.
Observe (PASS): on the Core's dashboard the owner's label appears "home" within a few seconds. Set consent RED → the label goes away (DELETE). Toggle GREEN again → it returns.
Fail-closed (PASS): against a Core WITHOUT ARP/root (mac_registered:false), the app shows "this Core can't do network presence" (Task 6 chip) and does not claim presence.

- [ ] **Step 5: Contract check (pytest, if a test central is reachable)**

Run the backend suite to confirm the presence endpoints' contract is intact:
Run: `cd /c/IA/wavr-phase1 && python -m pytest backend/tests -q -k "presence or consent or pair"`
Expected: PASS (no regressions in the endpoints the app calls).

- [ ] **Step 6: Commit**

```bash
git add mobile/src/wavr-mobile-shim.js
git commit -m "feat(mobile): auto-presence register/DELETE bound to consent + foreground re-assert"
```

---

## Task 6: Label capture at pair + status chip + details overlay

**Files:**
- Modify: `mobile/src/wavr-mobile-shim.js`

**Interfaces:**
- Consumes: secure storage helpers `secureGet/secureSet`; `persistPairing()`; `_pendingCoreName` (Task 3); `registerPresence()` (Task 5).
- Produces: keys `K_PRESENCE_LABEL`, `K_CORE_NAME`; caches `_presenceLabel`, `_coreName`; `renderStatusChip()`; `injectStatusChip()`; a details overlay `showDetails()` with label edit + unpair.

- [ ] **Step 1: Add the keys + caches**

Near the other keys (~line 85): `var K_PRESENCE_LABEL = "wavr.presenceLabel", K_CORE_NAME = "wavr.coreName";`
Near the caches (~line 109): `var _presenceLabel = "", _coreName = "";` (replace the temporary from Task 5).

- [ ] **Step 2: Capture the label during pairing**

In `showVerify()`'s pin success (~line 536, the `persistPairing(...).then(...)` block), before revealing the code entry, add a label field to the verify card OR add a one-field step after pin. Minimal approach: add a label input to the verify card and persist it with the pairing:

```js
// add near the other fields in showVerify(), before pinBtn:
var lf = el("label", "wavrm-field");
lf.appendChild(el("span", "wavrm-lab", "Your name on this device (shown as your presence at home)"));
var labelIn = el("input", "wavrm-input"); labelIn.type = "text"; labelIn.autocomplete = "off";
labelIn.placeholder = "e.g., Augusto"; labelIn.setAttribute("aria-label", "your name on this device");
lf.appendChild(labelIn); card.appendChild(lf);
```

In the pin success handler, persist the label + core name alongside the pairing:

```js
_pinnedFp = fp;
_presenceLabel = (labelIn.value || "").trim();
_coreName = _pendingCoreName || _base;
pinBtn.disabled = true; msg.className = "wavrm-msg"; msg.textContent = "Saving…";
Promise.all([ persistPairing(_base, fp, null),
              secureSet(K_PRESENCE_LABEL, _presenceLabel),
              secureSet(K_CORE_NAME, _coreName) ]).then(function(){
  revealCodeEntry(); hideOverlay();
}, function(){ /* unchanged failure path: _pinnedFp = null; show save error */ });
```

- [ ] **Step 3: Status chip + details overlay**

Add near the consent UI:

```js
// A tappable chip that states the attachment + presence, and opens details (edit label, unpair).
var _statusChip = null;
function statusText(){
  if(!_attached) return "Out";
  if(_presenceError) return "Connected · no network presence";
  return "Connected to " + (_coreName || "your Core") + " as " + (_presenceLabel || "this device");
}
function renderStatusChip(){ if(_statusChip) _statusChip.textContent = statusText(); }
function injectStatusChip(){
  if(!_token) return;
  var row = document.querySelector(".status-pills"); if(!row) return;
  if(document.getElementById("wavrm-status")) return;
  var b = el("button", "tpill"); b.id = "wavrm-status"; b.type = "button";
  b.onclick = function(){ showDetails(); };
  _statusChip = el("span", "p-txt", statusText()); b.appendChild(_statusChip);
  row.appendChild(b);
}
function showDetails(){
  var card = ensureOverlay("details");
  card.appendChild(el("h2", "wavrm-h", "This device"));
  var f = el("label", "wavrm-field"); f.appendChild(el("span", "wavrm-lab", "Your name on this device"));
  var input = el("input", "wavrm-input"); input.type = "text"; input.value = _presenceLabel || "";
  input.placeholder = "e.g., Augusto"; f.appendChild(input); card.appendChild(f);
  var save = el("button", "wavrm-btn", "Save name"); save.type = "button";
  save.onclick = function(){
    _presenceLabel = (input.value || "").trim();
    secureSet(K_PRESENCE_LABEL, _presenceLabel).then(function(){
      renderStatusChip(); reassertPresence(); hideOverlay();
    });
  };
  card.appendChild(save);
  card.appendChild(el("p", "wavrm-sub", "Core: " + (_coreName || "—")));
  var unpair = el("button", "wavrm-btn ghost", "Unpair this device"); unpair.type = "button";
  unpair.onclick = function(){
    unregisterPresence();
    Promise.all([ secureDel(K_TOKEN), secureDel(K_URL), secureDel(K_FP) ]).then(function(){
      try{ location.reload(); }catch(_){}
    });
  };
  card.appendChild(unpair);
}
```

Call `injectStatusChip()` wherever `injectConsentPill()` is called in `decideScreen()` (the viewer/admin/combo branches, ~lines 1091/1098).

- [ ] **Step 4: Load the caches at boot**

In the boot `Promise.all` (~line 1114), add `secureGet(K_PRESENCE_LABEL)` and `secureGet(K_CORE_NAME)` to the array, and in the `.then` assign `_presenceLabel = v[8] || ""; _coreName = v[9] || "";` (indices follow the existing order).

- [ ] **Step 5: On-device drill — label + status + unpair**

Drive: pair with a name typed → after attaching, the header chip reads "Connected to <coreName> as <name>". Tap it → details overlay; change the name → Save → chip + Core presence update. Tap "Unpair" → app reloads to the discovery/setup screen and the Core presence is gone.

- [ ] **Step 6: Commit**

```bash
git add mobile/src/wavr-mobile-shim.js
git commit -m "feat(mobile): label capture at pair + status chip + details overlay (edit/unpair)"
```

---

## Task 7: Boot auto-attach from the stored level

**Files:**
- Modify: `mobile/src/wavr-mobile-shim.js`

**Interfaces:**
- Consumes: `_consent` (already loaded at boot ~line 1120), `WavrLib.consentToActions`, `_attached`, `registerPresence()`.
- Produces: boot sets `_attached` from the stored level and registers presence when entered.

- [ ] **Step 1: Set `_attached` from the stored level before deciding the screen**

In the boot `.then` after the caches load (~line 1120, right after `_consent = normConsent(v[7]);`), add:

```js
_attached = WavrLib.consentToActions(_consent).attached;   // stored RED => boot detached
```

- [ ] **Step 2: Register presence on an entered boot**

In the same `.then`, where `detectRole()` is called for an attached token (~line 1125), also re-assert presence when entered:

```js
if(_token && !capsSensorOnly()){
  detectRole();
  if(_attached) registerPresence();
}
```

- [ ] **Step 3: Show the Out overlay when booting detached**

In `decideScreen()`, in the paired viewer/admin/combo branches, when `!_attached` show the Out screen instead of connecting. At the top of the `if(_token){ … }` block (~line 1076), after clearing the pairing chrome:

```js
if(!_attached){ showOut(); injectConsentPill(); return; }
```

(so a red-at-boot device shows "You've left Wavr" with the pill to re-enter, and does not try to connect).

- [ ] **Step 4: On-device drill — persistence across relaunch**

Drive: set GREEN, force-quit the app, relaunch → it auto-connects and re-registers presence (no code, no toggle). Set RED, force-quit, relaunch → it opens on the "You've left Wavr" screen, no connection, no presence, until you set green/yellow.

- [ ] **Step 5: Commit**

```bash
git add mobile/src/wavr-mobile-shim.js
git commit -m "feat(mobile): boot auto-attach from the stored consent level"
```

---

## Self-Review

**Spec coverage:**
- Discovery (mDNS + choose-your-Core + manual fallback) → Task 3. ✓
- One-time pair → persistent token (paths B/C reuse existing verify/code) → Task 3 feeds existing `showVerify`/code; label/coreName stored → Task 6. ✓
- Consent = enter/exit (green/yellow connect+register, red disconnect+DELETE) → Tasks 4 (lever) + 5 (presence). ✓
- Re-assert (foreground/reconnect/30min) → Task 5. ✓
- Fail-closed on `mac_registered:false` → Task 5 (+ chip copy Task 6). ✓
- Label + status chip + details/unpair → Task 6. ✓
- Boot auto-attach from stored level → Task 7. ✓
- QR / bootstrap-QR / admin-QR → **Phase 2/3, deliberately out of this plan.**
- Native dep `capacitor-zeroconf` → coordination note (capacitor-shell-engineer); tasks degrade gracefully without it.

**Placeholder scan:** the `renderStatusChip`/`_presenceLabel` temporaries in Task 5 are explicitly replaced in Task 6 (called out in-step), not left dangling. No TBD/TODO.

**Type consistency:** `parseCoreService` returns `{name, host, port}` (Task 1) — consumed as `core.name/host/port` in Task 3. `consentToActions` returns `{attached, presence, contribution}` (Task 1) — consumed as `.attached`/`.presence` in Tasks 4/5/7. `registerPresence`/`unregisterPresence` names consistent across Tasks 5–7. Keys `K_PRESENCE_LABEL`/`K_CORE_NAME` defined once (Task 6), read at boot (Tasks 6/7).

---

## Notes for the executor

- **The riskiest task is Task 4** (the connect/disconnect lever against index.html's provider). If the reload-on-green feels heavy or the reconnect doesn't re-fire, the fallback is already the mechanism in use (token-hide + reload); verify the provider's actual reconnect timing on-device and adjust the nudge.
- **`capacitor-zeroconf`** may not be installed yet — Tasks 3–7 are all testable via manual entry + the pure tests until it lands; do not block on it.
- Run `npm test` (pure lib) after Tasks 1–2 and the relevant backend `pytest` after Task 5; everything else is the on-device drills as written.
