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
    return _INDEX.read_text(encoding="utf-8")


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
    # ...while the genuinely-unreachable path (network error) still fails open, unchanged.
    assert ".catch(function(){ return false; })" in body


def test_fetch_pin_configured_still_sends_the_csrf_header():
    # Unrelated to the fix -- a regression guard that the edit didn't drop the existing
    # loopback CSRF header on the request.
    body = _fetch_pin_configured_body()
    assert '{headers:{"X-Wavr-Local":"1"}}' in body
