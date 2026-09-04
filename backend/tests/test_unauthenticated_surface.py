"""What an unpaired device on this network can reach, pinned.

This is the first question an operator, an auditor and a threat model all ask,
and until now the answer lived in three places: one module constant and two
literals buried roughly 2,000 lines deep inside `create_app`, which is itself
about 5,000 lines long. Widening the surface was a one-line edit in the middle
of a long middleware, which is the least visible place in the codebase for the
most security-relevant change it can carry.

The lists are hoisted to module level now, and this file makes them a decision
rather than an edit: adding a path fails here until somebody writes down why it
belongs. The rule is narrow.

  * **Onboarding** — a caller reaching it has no credential BY DEFINITION,
    because a credential is what it is asking for. Each path must be bounded by
    something else: a one-time rate-limited pairing code, a capability the
    requester was handed, or a handler that verifies a node bearer itself.
  * **Static shell** — markup and scripts. No data, no action. A companion has
    to LOAD the page before it can pair, and the page shows only the pairing
    screen until a token is entered.

Anything that reads the Space, writes to it, or does something belongs in
neither list. Both are additionally bounded by `in_subnet`, and neither grants a
role: `request.state.role` stays None, so every scope gate downstream still
denies.
"""
from __future__ import annotations

from wavr import app as appmod

# The exact surface, written out. A diff to this list is the review.
EXPECTED_ONBOARDING = {
    "/api/pair",                  # bounded by a one-time, rate-limited code
    "/api/peers/redeem",          # same code, ~2-minute window, loopback-minted
    "/api/nodes/enroll",          # handler verifies the node bearer itself
    "/api/nodes/telemetry",       # ditto
    "/api/nodes/heartbeat",       # ditto
    "/api/nodes/reactivate",      # ditto
    "/api/nodes/request",         # mints nothing, per-IP rate limited
    "/api/nodes/claim",           # bounded by a 192-bit capability
    "/api/pair-request",          # opens a PENDING record only
    "/api/pair-request/status",   # returns a token only after a loopback Approve
}

EXPECTED_SHELL = {
    "/", "/index.html", "/measure.html", "/manifest.webmanifest",
    "/sw.js", "/icon.svg",
    "/js/wizard.js", "/js/discoveries.js", "/js/trust.js", "/js/developer.js",
    "/js/runtime.js",
    "/sdk/javascript/wavr.js",
}

EXPECTED_SHELL_PREFIXES = ("/vendor/", "/experiences/")


def test_the_unauthenticated_onboarding_surface_is_exactly_this():
    assert set(appmod._UNAUTH_ONBOARDING_PATHS) == EXPECTED_ONBOARDING


def test_the_unauthenticated_static_shell_is_exactly_this():
    assert set(appmod._STATIC_SHELL_PATHS) == EXPECTED_SHELL
    assert tuple(appmod._STATIC_SHELL_PREFIXES) == EXPECTED_SHELL_PREFIXES


def test_no_admin_route_is_reachable_without_a_credential():
    """The onboarding list has a sibling for every entry that must NOT be in it:
    the admin route for the same feature. A copy-paste that pulled one in would
    hand an unpaired LAN device the ability to approve its own pairing."""
    must_not = (
        "/api/nodes", "/api/nodes/enroll-code", "/api/pending-pairings",
        "/api/peers/link-back", "/api/peers", "/api/devices",
    )
    for path in must_not:
        assert path not in appmod._UNAUTH_ONBOARDING_PATHS
        assert not appmod._is_static_shell(path)


def test_nothing_that_reads_the_space_is_in_the_static_shell():
    """The shell is markup. A data route that drifted into it would be readable
    by any device on the network with no token at all."""
    for path in ("/api/state", "/api/rooms", "/api/anchors", "/api/space",
                 "/api/experience/context", "/api/privacy/data", "/ws/live",
                 "/ws/events", "/api/providers/x/observations"):
        assert not appmod._is_static_shell(path), path


def test_the_experiences_prefix_does_not_leak_into_the_api():
    """`startswith` matching is the risky half of this design: a prefix one
    character too short opens a whole tree. These are the near-misses."""
    assert appmod._is_static_shell("/experiences/spatial-web/")
    assert appmod._is_static_shell("/vendor/three.min.js")
    for near in ("/experiences", "/api/experiences/x", "/experience/context",
                 "/vendor", "/api/vendor/x"):
        assert not appmod._is_static_shell(near), near


def test_the_sdk_is_reachable_but_only_that_one_file():
    """A browser must be able to load the SDK the reference pages import — that
    is why it is here at all. It must not open the rest of `sdk/`, which
    contains the Python and Kotlin sources and their tests."""
    assert appmod._is_static_shell("/sdk/javascript/wavr.js")
    for other in ("/sdk/", "/sdk/python/wavr_sdk/__init__.py",
                  "/sdk/javascript/test.mjs", "/sdk/README.md"):
        assert not appmod._is_static_shell(other), other


def test_the_token_exempt_list_is_narrower_than_the_shell():
    """`_TOKEN_EXEMPT_PATHS` governs a stricter gate — the local-token check
    that stops another process on the SAME machine reading inventory. It should
    never grow to match the shell list: a page a LAN companion may load is not
    automatically a page a same-machine process may load without the token."""
    assert set(appmod._TOKEN_EXEMPT_PATHS) < EXPECTED_SHELL | {"/healthz"}
    assert "/js/developer.js" not in appmod._TOKEN_EXEMPT_PATHS
    assert "/sdk/javascript/wavr.js" not in appmod._TOKEN_EXEMPT_PATHS
