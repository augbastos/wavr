/* Talking to the Core. One place.
 *
 * ## The duplication this replaces
 *
 * `trust.js` and `wizard.js` each carried a byte-identical fourteen-line
 * `api(path, opts)` — same header composition, same JSON encode, same
 * `r.json().catch(...)`, same "Wavr answered 404" fallback. Two copies of one
 * decision is the shape that eventually disagrees: somebody fixes the error
 * message in one of them, and from then on the product says two different
 * things about the same failure depending on which screen you were looking at.
 *
 * Around them, a hundred and thirty call sites hand-wrote
 * `fetch(location.origin + path, {headers: {"X-Wavr-Local": "1"}})`. That
 * header is not decoration: it is the CSRF guard the backend checks on every
 * non-shell route, so every one of those literals is a security decision
 * written out again from memory. They are all the same decision.
 *
 * ## What this owns
 *
 *   * the origin — every path here is same-origin by construction, and a
 *     caller cannot accidentally send household data to somewhere else;
 *   * `X-Wavr-Local`, on by default, off only where a caller says so;
 *   * JSON encoding, and the `Content-Type` that has to accompany it;
 *   * the companion bearer token, for the modes that carry one.
 *
 * ## What it deliberately does NOT own
 *
 * Response handling. `WavrAPI.fetch` returns exactly what `fetch` returns — a
 * `Promise<Response>` — so every existing `.then(r => r.ok ? ... : ...)` reads
 * the same and behaves the same. Centralising the REQUEST is a safe,
 * mechanical change; centralising the response would quietly rewrite a hundred
 * error paths, several of which are load-bearing (`failureText` reads both
 * `detail` and `error`, some callers want the status, some want the raw body).
 *
 * `WavrAPI.json` is the one opinionated helper, and it exists because it was
 * already written twice.
 *
 * ## Load position: after `format.js`, before every feature module
 *
 * Modules call it while their own script is still executing. It depends on
 * nothing but `location` and `fetch`, so it can sit as early as it needs to.
 */
(function () {
  "use strict";

  var CSRF = "X-Wavr-Local";

  /* The companion's bearer, if this page is a companion and holds one.
   *
   * Guarded by `typeof`: `MODE` and `companionToken` are declared in
   * `core-connection.js`, which loads first, but this module is also read by
   * tests and by pages that load only part of the shell.
   */
  function bearer() {
    try {
      if (typeof MODE === "undefined" || MODE !== "companion") return "";
      if (typeof companionToken !== "function") return "";
      return companionToken() || "";
    } catch (e) { return ""; }
  }

  function headersFor(opts) {
    var h = {};
    // Copy first so an explicit header from the caller wins over the default —
    // the one case being a caller that has to send its own Authorization.
    var given = opts.headers || {};
    if (opts.csrf !== false) h[CSRF] = "1";
    if (opts.json !== undefined) h["Content-Type"] = "application/json";
    // The bearer this module's own docstring says it owns, and did not send.
    //
    // Every caller that predates this module carried the header by hand and
    // still does, so nothing regressed while those were the only users. The
    // day a caller trusted the docstring instead — the runtime chip and the
    // attention badge, which started polling in companion mode — the request
    // went out with no credential, the Core refused it, and the chip settled
    // on "Wavr is not answering" over a Core that was answering perfectly.
    // A paired phone saw that permanently, and it is silent by construction:
    // a refused request is exactly what "not answering" is supposed to be.
    var token = bearer();
    if (token) h["Authorization"] = "Bearer " + token;
    for (var k in given) if (Object.prototype.hasOwnProperty.call(given, k)) {
      h[k] = given[k];
    }
    return h;
  }

  /* Where a request actually goes.
   *
   * Same-origin on the Core's own machine and in a LAN browser. On the
   * Capacitor build the page is served from the app's own `https://localhost`,
   * so same-origin would be the app talking to itself: the shim publishes the
   * paired Core's base and a pinned fetch, and every companion-aware module
   * already routes through it. This module did not, which is the other half
   * of the same defect.
   */
  function send(path, init) {
    if (typeof window !== "undefined" && window.WAVR_MOBILE
        && typeof window.WAVR_MOBILE.netFetch === "function"
        && window.WAVR_MOBILE.base) {
      return window.WAVR_MOBILE.netFetch(window.WAVR_MOBILE.base + path, init);
    }
    return fetch(location.origin + path, init);
  }

  /* A request to this Core.
   *
   * `path` is same-origin and starts with "/". `opts` is a `fetch` init with
   * three additions: `json` (an object, encoded as the body with the right
   * Content-Type), `csrf: false` (for the handful of routes deliberately
   * reachable without the header), and `timeoutMs`.
   *
   * Returns `Promise<Response>`, unchanged, on purpose.
   *
   * ## Why `timeoutMs` exists, and why it is not the default
   *
   * A host that goes DARK — power cut, laptop suspended, Wi-Fi dropped — does
   * not refuse a connection. It says nothing, and `fetch` waits: no rejection,
   * no resolution, for minutes. Any caller whose freshness depends on its own
   * request completing therefore stops updating and keeps showing whatever it
   * last drew. For the runtime chip that means a green "Live" over a Core that
   * is gone, which is the single failure the observability rule names.
   *
   * It is opt-in because most callers should NOT have one: a slow answer to a
   * network scan or an inventory refresh is still the right answer, and
   * aborting it would turn "this is taking a while" into "this failed". Only a
   * caller that reports LIVENESS needs a deadline, because for that caller a
   * late answer is not an answer.
   */
  function apiFetch(path, opts) {
    opts = opts || {};
    var init = {};
    for (var k in opts) if (Object.prototype.hasOwnProperty.call(opts, k)) {
      if (k !== "json" && k !== "csrf" && k !== "timeoutMs") init[k] = opts[k];
    }
    init.headers = headersFor(opts);
    if (opts.json !== undefined) init.body = JSON.stringify(opts.json);

    if (!opts.timeoutMs) return send(path, init);

    // A caller's own `signal` still wins; the timeout only adds a second way
    // for the request to end.
    var ctl = typeof AbortController === "function" ? new AbortController()
                                                    : null;
    if (!ctl) return send(path, init);
    if (init.signal && typeof init.signal.addEventListener === "function") {
      init.signal.addEventListener("abort", function () { ctl.abort(); });
    }
    init.signal = ctl.signal;
    var timer = setTimeout(function () { ctl.abort(); }, opts.timeoutMs);
    return send(path, init).then(function (r) {
      clearTimeout(timer);
      return r;
    }, function (e) {
      clearTimeout(timer);
      throw e;
    });
  }

  /* JSON in, JSON out, and an Error whose message is safe to show somebody.
   *
   * Lifted from the two identical copies in `trust.js` and `wizard.js`,
   * behaviour intact: a body that is not JSON becomes `{}` rather than
   * throwing a parse error over the real failure, and a non-OK response throws
   * with the backend's own `detail` when there is one.
   */
  function apiJson(path, opts) {
    opts = opts || {};
    return apiFetch(path, {
      method: opts.method || "GET",
      json: opts.body,
      headers: opts.headers,
      csrf: opts.csrf,
      signal: opts.signal,
      // `apiFetch` builds this init by hand, so anything not listed here is
      // dropped. `timeoutMs` was not listed: a caller that asked for a
      // deadline got none, silently, and went on waiting for ever on a dark
      // host — which is the exact failure `timeoutMs` exists to end, arriving
      // through the door that was supposed to prevent it.
      timeoutMs: opts.timeoutMs
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) {
          var msg = j.detail || j.error
            || (typeof WavrT === "function"
                ? WavrT("Wavr answered {status}", { status: r.status })
                : "Wavr answered " + r.status);
          var err = new Error(msg);
          err.status = r.status;
          err.payload = j;
          throw err;
        }
        return j;
      });
    });
  }

  window.WavrAPI = {
    fetch: apiFetch,
    json: apiJson,
    // Exposed so the few callers that build their own init (the companion
    // bearer path in `core-connection.js`, the mobile shim) name the header
    // instead of retyping it.
    CSRF_HEADER: CSRF
  };
})();
