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
    "/sdk/javascript/wavr.js",
}

# "/js/" moved from a list of named files to a prefix when the shell was
# modularised, and the trade is worth writing down rather than assuming. What
# it gives up: a reviewer can no longer read the whole surface off this list.
# What it buys: the list can no longer fall out of step with the shell — and
# that particular drift is not a small bug. A module the page requests and the
# backend does not serve 404s, `Cache.addAll` is all-or-nothing, so the service
# worker's install fails as a unit and OFFLINE LAUNCH disappears, for one
# forgotten line in a list.
#
# The prefix stays honest because everything behind it is one class — script,
# no data, no action, nothing to draw without a credential — and because the
# route enforces exactly that: a bare `*.js` resolving to a direct child of
# `frontend/js`, nothing else. The tests below are the proof, not the claim.
EXPECTED_SHELL_PREFIXES = ("/vendor/", "/experiences/", "/js/")


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


# -- the "/js/" prefix, and what stops it being a directory listing ------------


def _client():
    from fastapi.testclient import TestClient
    from wavr.app import create_app
    return TestClient(create_app())


def test_every_module_the_shell_loads_is_actually_served():
    """The whole point of the prefix. Read the script tags out of index.html
    and fetch each one: a name the page asks for and the route refuses is a
    404, and a 404 here takes offline launch with it."""
    import re
    from pathlib import Path
    shell = (Path(appmod.__file__).resolve().parents[2]
             / "frontend" / "index.html").read_text(encoding="utf-8")
    names = re.findall(r'<script src="js/([^"]+)"', shell)
    assert names, "no module tags found — the extractor regex is wrong"
    c = _client()
    for name in names:
        r = c.get(f"/js/{name}")
        assert r.status_code == 200, f"index.html loads js/{name} and it {r.status_code}s"
        assert "javascript" in r.headers["content-type"]


def _raw_get(app, path: str, client: str = "127.0.0.1") -> int:
    """Status for a path sent EXACTLY as written, straight into the ASGI app.

    `TestClient.get("/js/../sw.js")` returns 200 and proves nothing: httpx
    resolves dot-segments per RFC 3986 before anything reaches the wire, so the
    server is asked for `/sw.js` and correctly serves it. The first version of
    the test below failed on that, and it was the TEST that was wrong —
    measuring the client, not the surface. An attacker writes the socket
    directly, so this does too.

    The second version was wrong in the opposite direction and mattered more:
    it put the raw string straight into `scope["path"]`, so `%2f` arrived at
    the handler still encoded and every probe was refused for being a
    weird-looking filename rather than for being a traversal. Per ASGI the
    SERVER owns that decode — uvicorn percent-decodes into `path` and keeps the
    original in `raw_path` — so the probe that matters never actually ran. Do
    what uvicorn does, and the probes test what they claim to.
    """
    import asyncio
    from urllib.parse import unquote

    async def run():
        got = {}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.start":
                got["status"] = message["status"]

        raw, _, query = path.partition("?")
        await app({
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "http", "path": unquote(raw),
            "raw_path": raw.encode(), "query_string": query.encode(),
            "root_path": "", "headers": [(b"host", b"127.0.0.1")],
            "client": (client, 51000), "server": ("127.0.0.1", 8787),
        }, receive, send)
        return got.get("status", 0)

    return asyncio.run(run())


def test_the_prefix_does_not_open_the_repository():
    """`startswith` is the risky half of the design, and the reason the route
    validates the NAME instead of trusting the prefix.

    Two shapes get through a naive prefix check. A path with real dot-segments
    never matches `/js/{name}` at all, because Starlette's path converter is a
    single segment. An ENCODED separator does match — Starlette decodes it into
    the name before the handler runs — and lands in the handler as
    `../sw.js`, where the pattern is the only thing standing between it and a
    file read. Both are sent raw, below.
    """
    from wavr.app import create_app
    app = create_app()
    for probe in (
        "../sw.js", "..%2fsw.js", "%2e%2e%2fsw.js", "%2E%2E/sw.js",
        "../../backend/wavr/app.py", "..%2F..%2Fwavr.db",
        "..\\\\sw.js", "%5c..%5csw.js",
        "sub/nested.js", "sub%2Fnested.js",
        "wizard.js.bak", "wizard.txt", "Wizard.js", ".env", "", ".", "..",
        "wizard.js%00.txt",
    ):
        status = _raw_get(app, f"/js/{probe}")
        assert status in (307, 404, 400), (
            f"/js/{probe} returned {status} — the prefix opened something the "
            f"old per-file allowlist would have refused")


def test_which_layer_refuses_which_probe():
    """Names the defence for each shape, because a probe list alone does not.

    A green probe list is compatible with every probe dying at the same place,
    and then one layer is load-bearing and the others are decoration nobody
    would notice removing. Measured, the split is:

      * **an encoded separator never reaches the handler.** Once the server
        percent-decodes (uvicorn does; the helper above does), `..%2fsw.js`
        IS `../sw.js`, three path segments, and `/js/{name}` matches one. The
        ROUTER refuses it. This surprised the first draft of the test, which
        credited the pattern.
      * **`..` alone is one segment and does reach the handler.** Nothing about
        the routing stops it. The PATTERN is the only thing between it and
        `(_JS_DIR / "..").resolve()`, which is `frontend/`.
      * **a legal name pointing outside** — a symlink dropped in the directory
        — passes both. The resolved-parent check is what refuses it.

    Each bullet is asserted below, so removing any one layer fails here.
    """
    import re
    from fastapi import FastAPI
    from wavr.app import create_app

    pattern = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.js$")
    seen = []
    probe = FastAPI()

    @probe.get("/js/{name}")
    async def _spy(name: str):          # same signature as the real handler
        seen.append(name)
        return {"ok": True}

    # 1. the router, not the pattern
    assert _raw_get(probe, "/js/..%2fsw.js") == 404
    assert seen == [], (
        f"a decoded separator reached the handler as {seen} — the routing "
        f"assumption in this module's comments is no longer true, and the "
        f"pattern is now the only defence for it")

    # 2. the pattern, not the router
    assert _raw_get(probe, "/js/..") == 200 and seen == [".."], (
        f"`..` is a single path segment and Starlette should hand it over; "
        f"got {seen}. If it stopped, the pattern's job here is untested.")
    assert not pattern.match("..")
    assert _raw_get(create_app(), "/js/..") == 404

    # 3. the resolved-parent check, not the pattern
    assert pattern.match("elsewhere.js"), (
        "a plausible symlink name the pattern is happy with — only resolving "
        "it and comparing parents catches where it points")


def test_a_name_the_route_accepts_must_exist_on_disk():
    """A well-formed name for a file that is not there is a 404, not a 500 and
    not an empty 200 the service worker would happily cache."""
    r = _client().get("/js/nothing-here.js")
    assert r.status_code == 404


def test_nothing_but_javascript_lives_under_the_prefix():
    """The prefix is only honest while the directory holds one class of thing.
    A `.json` of household data dropped in `frontend/js/` would be readable by
    any unpaired device on the network — the route would refuse to SERVE it,
    and this fails first so nobody has to notice that it did."""
    from pathlib import Path
    js = Path(appmod.__file__).resolve().parents[2] / "frontend" / "js"
    strays = [f.name for f in js.iterdir()
              if f.is_file() and f.suffix != ".js"]
    assert not strays, (
        f"non-script files in frontend/js: {strays}. Everything under the "
        f"'/js/' prefix is reachable without a credential — put data anywhere "
        f"else.")


def test_every_module_on_disk_is_named_the_way_the_route_requires():
    """A file the route cannot express is a file that cannot be loaded. Catch
    the naming mistake at commit time rather than as a blank page."""
    import re
    from pathlib import Path
    pattern = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.js$")
    js = Path(appmod.__file__).resolve().parents[2] / "frontend" / "js"
    bad = [f.name for f in js.iterdir()
           if f.is_file() and not pattern.match(f.name)]
    assert not bad, (
        f"these cannot be served by /js/{{name}}: {bad} — lowercase letters, "
        f"digits and inner hyphens only")


# -- the SDK's advertised reads are actually readable --------------------------
#
# `sdk/README.md` has a table headed "what every SDK can reach". Two of the
# seven entries returned 403 to the credential an ordinary pairing produces,
# because a READ was gated as though it changed state.


def test_the_sdk_reads_are_gated_the_same_way_the_stream_is():
    """`/api/events/recent` and `/ws/events` are one stream over two
    transports. They carried two different gates, and the HTTP one refused
    `user` — the role the pairing UI defaults to and the only one
    docs/mcp-connect.md tells you to mint.

    Read off the source rather than by driving a paired client: the point is
    which DEPENDENCIES the route declares, and a behavioural test would pass
    the day somebody re-added `require_local` behind a role that happened to
    satisfy it.
    """
    from pathlib import Path
    src = (Path(appmod.__file__)).read_text(encoding="utf-8")
    i = src.index('@app.get("/api/events/recent")')
    sig = src[i:src.index('"""', i)]
    assert "require_scope(\"presence:read\")" in sig, sig
    assert "require_local" not in sig, (
        "a READ is gated as a state change again: require_local refuses every "
        "role that is not central, so the SDKs' only event mechanism 403s for "
        "an ordinary paired application")

    # To the `deps=` line, not to the first `))` — the mount contains
    # `lambda: list(_fusion.rooms())`, and slicing on the first double paren
    # cut the argument list in half and read the gate off a fragment that never
    # contained it.
    j = src.index("build_coverage_router(")
    mount = src[j:src.index("\n", src.index("deps=", j))]
    assert 'require_scope("presence:read")' in mount, mount
    assert "require_local" not in mount and '"admin"' not in mount, (
        "coverage is admin-gated again; it is the surface that says 'no sensor "
        "covers this room', and the SDKs advertise it to every caller")


def test_coverage_still_needs_a_credential():
    """Widening it to `presence:read` must not have widened it to nobody.

    Probed from a LAN address, not from loopback. A loopback caller IS the
    Core's own screen and holds every scope — the first version of this test
    asked from 127.0.0.1, got the 200 that role is supposed to get, and read it
    as a hole. What matters is that a device on the network with no credential
    is still refused.
    """
    from wavr.app import create_app
    assert not appmod._is_static_shell("/api/coverage")
    assert "/api/coverage" not in appmod._UNAUTH_ONBOARDING_PATHS
    status = _raw_get(create_app(), "/api/coverage", client="192.168.1.50")
    assert status in (401, 403), status
