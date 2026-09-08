// Wavr PWA service worker — caches the static app shell (offline launch) plus, on
// first use, the same-origin vendored three.js bundle under /vendor/ (offline 3D view).
//
// PRIVACY INVARIANT: the SW caches nothing but static, non-personal assets — the app
// shell files and the /vendor/ three.js library. API/WS responses, room state, house
// maps, device inventory — any request to /api/*, /ws/*, or any cross-origin URL — are
// NEVER intercepted and NEVER cached. The fetch handler bails out (no respondWith) for
// everything that isn't shell/vendor/navigation, so those requests hit the network
// exactly as if no SW existed. The SW also never initiates a request of its own beyond
// precaching the same-origin shell, so it adds zero external egress.
//
// Bump CACHE to invalidate the old shell on the next activate. Bumping it is no
// longer what carries a fix to an installed Core — see the shell branch of the
// fetch handler — but it still discards entries an older SW wrote.
const CACHE = "wavr-shell-v44";
const VENDOR_CACHE = "wavr-vendor-v1";
// index.html is a SHELL — markup, styles, and an ordered list of <script> tags.
// The product itself is the modules in `frontend/js/`, so EVERY ONE of them is
// part of the shell in the sense that matters here: without them a cached
// offline launch renders a dashboard whose pieces silently do nothing. The
// early ones are worse than that — `i18n.js` defines `WavrT`, which every later
// module calls at parse time, and `format.js` is right behind it — so an
// offline launch missing either is a blank page, not a degraded one.
//
// This list is therefore not a curated subset. It is every script index.html
// loads, and it fails in both directions. A script the shell loads and this
// list omits is simply not cached, so offline it 404s and the page is broken
// from that tag onward. A name in this list the backend does NOT serve is
// worse: `Cache.addAll` is all-or-nothing, so that one rejection throws the
// entire precache away and there is no offline launch at all. Either way the
// bug only appears offline, which is the hardest place to notice one.
// `test_sw_shell.py` fails when the two lists drift apart.
const SHELL = [
  "./", "./index.html", "./manifest.webmanifest", "./icon.svg", "./js/i18n.js",
  "./js/locale-pt.js", "./js/format.js", "./js/api.js", "./js/core-connection.js",
  "./js/shared.js", "./js/house-indicator.js", "./js/status-panel.js",
  "./js/control-plane.js", "./js/cameras.js", "./js/core-lock.js", "./js/narration.js",
  "./js/network.js", "./js/house-status.js", "./js/transparency.js", "./js/pairing.js",
  "./js/identity.js", "./js/known-presence.js", "./js/peers.js", "./js/nodes.js",
  "./js/core-settings.js", "./js/space-admin.js", "./js/connectors.js",
  "./js/assistant.js", "./js/routines.js", "./js/radar.js", "./js/housemap.js",
  "./js/house3d.js", "./js/render.js", "./js/pwa.js", "./js/shell-nav.js",
  "./js/devices.js", "./js/features.js", "./js/tooltips.js", "./js/whoshome.js",
  "./js/new-devices.js", "./js/trust.js", "./js/developer.js", "./js/runtime.js",
  "./js/discoveries.js", "./js/whats-new.js", "./js/core-panel.js", "./js/wizard.js",
  "./js/language.js"];
const SHELL_PATHS = new Set([
  "/", "/index.html", "/manifest.webmanifest", "/icon.svg", "/js/i18n.js",
  "/js/locale-pt.js", "/js/format.js", "/js/api.js", "/js/core-connection.js",
  "/js/shared.js", "/js/house-indicator.js", "/js/status-panel.js", "/js/control-plane.js",
  "/js/cameras.js", "/js/core-lock.js", "/js/narration.js", "/js/network.js",
  "/js/house-status.js", "/js/transparency.js", "/js/pairing.js", "/js/identity.js",
  "/js/known-presence.js", "/js/peers.js", "/js/nodes.js", "/js/core-settings.js",
  "/js/space-admin.js", "/js/connectors.js", "/js/assistant.js", "/js/routines.js",
  "/js/radar.js", "/js/housemap.js", "/js/house3d.js", "/js/render.js", "/js/pwa.js",
  "/js/shell-nav.js", "/js/devices.js", "/js/features.js", "/js/tooltips.js",
  "/js/whoshome.js", "/js/new-devices.js", "/js/trust.js", "/js/developer.js",
  "/js/runtime.js", "/js/discoveries.js", "/js/whats-new.js", "/js/core-panel.js",
  "/js/wizard.js", "/js/language.js"]);

self.addEventListener("install", (event) => {
  // Keep the precache light: only the tiny app shell, NOT the ~750KB three.js bundle —
  // /vendor/ is populated lazily (cache-first, below) the first time the 3D view is used.
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE && k !== VENDOR_CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;                     // never touch non-GET (POST toggles etc.)

  let url;
  try { url = new URL(req.url); } catch { return; }
  if (url.origin !== self.location.origin) return;      // never touch cross-origin — no external egress

  // Cache-first for the vendored three.js bundle (same-origin, static, non-personal):
  // once downloaded, the 3D view works offline and never re-fetches the ~750KB library
  // on subsequent cold caches / view toggles.
  if (url.pathname.startsWith("/vendor/")) {
    event.respondWith(
      caches.open(VENDOR_CACHE).then(async (c) => {
        const cached = await c.match(req);
        if (cached) return cached;
        const res = await fetch(req);
        if (res && res.ok) c.put(req, res.clone());
        return res;
      })
    );
    return;
  }

  const isShell = SHELL_PATHS.has(url.pathname);
  const isNavigation = req.mode === "navigate";

  // Network-FIRST for the app shell, cache as the offline fallback.
  //
  // This used to be cache-first, and the comment justifying it called these files
  // "content-stable". They are the opposite: `index.html` and every module under
  // `/js/` change on every release, and they are the entire product. Cache-first
  // meant an installed Core kept serving the shell it was installed with until a
  // human remembered to edit the CACHE constant above — an upgrade path whose
  // correctness depends on somebody's memory.
  //
  // It failed exactly that way. The first real user reinstalled Wavr, opened
  // Settings, and could not find a search box that had been in the served HTML
  // for hours; his WebView2 was answering from a Cache Storage written before
  // that box existed, and reinstalling the app does not clear it. `pwa.js`
  // already carried this reasoning in full — for Capacitor, where the same
  // masking was found first and dealt with by refusing to register a SW at all.
  // Only one of the two shells got the lesson.
  //
  // The cache is still written on every successful fetch and still read when the
  // network fails, so the offline cold launch this SW exists for is unchanged:
  // the last shell the Core actually served is the one that comes back.
  if (isShell) {
    event.respondWith(
      fetch(req).then((res) => {
        if (res && res.ok) {
          const copia = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copia)).catch(() => {});
        }
        return res;
      }).catch(() => caches.match(req).then((c) => c || caches.match("./index.html")))
    );
    return;
  }

  // Navigations to any OTHER same-origin path (e.g. /measure.html, or an SPA-style deep link)
  // are NETWORK-FIRST: fetch the real page so it is never masked by the precached index.html.
  // The cached shell is used ONLY as an offline fallback (app launch with no network). This
  // fixes the bug where EVERY navigation was answered with the precached index.html, so
  // /measure.html could never load on an installed PWA. The network response is never cached.
  if (isNavigation) {
    event.respondWith(fetch(req).catch(() => caches.match("./index.html")));
    return;
  }

  // Everything else (/api/*, /ws/*, any data request) is left entirely to the network and is
  // never read from or written to the cache.
});
