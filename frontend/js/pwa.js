// ==========================================================================
// pwa.js — lifted verbatim out of index.html.
//
// A CLASSIC script, not a module, and its <script> tag sits at the exact
// position the inline block occupied. Both facts are load-bearing: several
// blocks chain onto `window.__wavr*` by wrapping whatever handler is already
// installed, so tag ORDER is the order those renderers run in, and top-level
// declarations here are reachable from later shell scripts only because this
// is not an ES module. `test_shell_modules.py` pins the order.
// ==========================================================================
// ---- PWA: register the shell service worker (offline launch + installable) ----
// Only on secure contexts (HTTPS, or localhost/127.0.0.1) — browsers block SW on plain
// HTTP off-localhost, so this stays silent on the public HTTP demo. The SW is same-origin
// and caches only the static shell, so registration triggers zero external requests.
// Fully guarded so a failure can never break the dashboard or the simulator self-switch.
try {
  // In the native app (Capacitor) the bundled assets ARE the offline copy, so a service
  // worker only adds a cache-first shell that MASKS app updates: across an in-place APK
  // install Android keeps the WebView's Cache Storage, so the SW keeps serving the old
  // index.html and a freshly-bundled build looks identical. In native we therefore SKIP
  // registration AND actively tear down any SW + caches a prior build left behind, so the
  // WebView always renders the freshly-bundled assets. The web PWA (demo/loopback in a
  // real browser) keeps the SW exactly as before.
  var __isNativeApp = !!window.WAVR_MOBILE ||
    !!(window.Capacitor && window.Capacitor.isNativePlatform && window.Capacitor.isNativePlatform());
  if (__isNativeApp) {
    if ("serviceWorker" in navigator && navigator.serviceWorker.getRegistrations) {
      navigator.serviceWorker.getRegistrations()
        .then((rs) => rs.forEach((r) => r.unregister())).catch(() => {});
    }
    if (window.caches && caches.keys) {
      caches.keys().then((ks) => ks.forEach((k) => caches.delete(k))).catch(() => {});
    }
  } else if ("serviceWorker" in navigator && window.isSecureContext) {
    // If this page was ALREADY under an older service worker when it loaded, the
    // markup and modules it is running came from that worker's cache. When the
    // new worker claims the page, this document is still the old one. Reload once
    // so an upgrade lands on the launch that installed it instead of the launch
    // after — the difference between "I reinstalled and the fix is there" and "I
    // reinstalled and nothing changed", which is what the first user reported.
    //
    // `controller` is read BEFORE registering, because it also becomes non-null
    // on a first-ever registration, and reloading then would be a reload for
    // nothing. The flag makes the reload once-per-page-load even if a worker
    // changes twice.
    var __jaControlado = !!navigator.serviceWorker.controller;
    var __recarregou = false;
    navigator.serviceWorker.addEventListener("controllerchange", () => {
      if (!__jaControlado || __recarregou) return;
      __recarregou = true;
      location.reload();
    });
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("sw.js").catch(() => {});
    });
  }
} catch (e) { /* SW unavailable — dashboard works exactly as before */ }
