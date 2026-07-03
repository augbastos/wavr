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
// Bump CACHE to invalidate the old shell on the next activate.
const CACHE = "wavr-shell-v2";
const VENDOR_CACHE = "wavr-vendor-v1";
const SHELL = ["./", "./index.html", "./manifest.webmanifest", "./icon.svg"];
const SHELL_PATHS = new Set(["/", "/index.html", "/manifest.webmanifest", "/icon.svg"]);

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

  // Cache-first ONLY for the static shell (and offline app-launch navigations, which
  // fall back to the cached index.html). Anything else — /api/*, /ws/*, any data — is
  // left entirely to the network and is never read from or written to the cache.
  if (!isShell && !isNavigation) return;

  event.respondWith(
    caches.match(isShell ? req : "./index.html").then((cached) => {
      // A cache miss on a non-shell path (e.g. an SPA-style deep link) falls through to
      // the network, but the response is deliberately NOT cached — only the precached
      // shell ever lives in the cache.
      return cached || fetch(req);
    })
  );
});
