"""Frontend-honesty guard (the single-file frontend has no JS test harness; mirrors
test_frontend_alerts.py's read-as-text style).

A5.1 audit fix (P1): fetchPinConfigured() (the Core Panel kiosk lock check) must NOT
read an explicit 401 -- our OWN WAVR_LOCAL_TOKEN hardening flag denying the request --
the same way it reads a genuinely unreachable backend (fail-OPEN, "no PIN configured").
Before the fix, `r.ok ? r.json() : {pin_set:false}` collapsed both cases to the same
unlocked state, so turning the token on silently revealed the kiosk unlocked. The fix
must fail SAFE (assume a lock IS configured) on 401, keeping the fail-open path only
for a real network error (the .catch below, unchanged).
"""
from pathlib import Path

# Same locator as wavr.app._INDEX: parents[2] of backend/tests/ is the repo root.
_INDEX = Path(__file__).resolve().parents[2] / "frontend" / "index.html"


def _html() -> str:
    from tests.frontend_source import ALL
    return ALL          # index.html plus every module it loads


def _fetch_pin_configured_body() -> str:
    html = _html()
    start = html.index("function fetchPinConfigured(){")
    end = html.index("fetchPinConfigured();", start)   # the eager warm-up call right after
    return html[start:end]


def test_fetch_pin_configured_distinguishes_401_from_unreachable():
    body = _fetch_pin_configured_body()
    # The 401 branch exists and fails SAFE (locked), not open (unlocked)...
    assert "r.status === 401" in body
    assert "return {pin_set:true}" in body
    # ...while a genuinely unreachable Core still fails open — but only for a
    # panel that has never been told a lock exists.
    #
    # This used to assert `.catch(function(){ return false; })`, and the word
    # "unchanged" was guarding the wrong thing. A blanket `false` means a kiosk
    # that HAS a PIN unlocks itself the moment the network hiccups: the same
    # fail-open the 401 branch above exists to close, arriving through the
    # other door. It now remembers whether a lock was ever confirmed, so a
    # panel that has seen one stays locked through a blip and a panel that
    # never has behaves exactly as before.
    assert ".catch(function(){ return pinEverConfirmed; })" in body, (
        "the unreachable path must not blanket-unlock a panel that has "
        "already been told a PIN exists")
    assert "pinEverConfirmed = true" in _html(), (
        "nothing ever sets the flag, so it can only ever fail open")


def test_fetch_pin_configured_still_sends_the_csrf_header():
    # A regression guard that the request still carries the loopback CSRF
    # header. It used to look for the literal `{headers:{"X-Wavr-Local":"1"}}`,
    # which stopped being how anything sends it: `js/api.js` owns header
    # composition now, and the hand-written copies were removed precisely so a
    # call site cannot forget one. The guard is worth keeping — it just has to
    # check what is now true, which is that this call goes through the client
    # that adds the header instead of a raw `fetch` that would not.
    body = _fetch_pin_configured_body()
    assert "WavrAPI.fetch(" in body, body[:400]
    assert "fetch(location.origin" not in body, (
        "this call bypasses WavrAPI, so nothing guarantees the CSRF header")

    from tests.frontend_source import MODULES
    api = MODULES["api.js"]
    assert 'var CSRF = "X-Wavr-Local"' in api and 'h[CSRF] = "1"' in api, (
        "WavrAPI no longer adds the CSRF header, so every call site that "
        "trusts it to — this one included — now sends the request without it")
