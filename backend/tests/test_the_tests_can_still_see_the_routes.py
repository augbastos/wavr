"""The suite must be able to see the endpoints it makes claims about.

## What happened

FastAPI 0.139 changed `include_router`. It used to copy each router's routes
into the app; now it appends ONE `_IncludedRouter` wrapper per router and keeps
the real routes inside it, under `original_router`. This app includes
thirty-two routers, so `app.routes` became thirty-two opaque entries with no
`path` at all: including a router with three routes grows `app.routes` by one.

Four tests in this suite enumerated `app.routes` looking for a path.

* Two were POSITIVE — "these routes must be mounted" — and started failing.
  That is how any of this was noticed.
* Two were NEGATIVE — "these routes must NOT be mounted without the
  prerequisite" — and they kept passing. Not because the protection held, but
  because they were searching a list that could no longer contain what they were
  looking for. They would have gone on passing if the protection had been
  deleted outright. One of them guards the UWB session key; another guards the
  "a setting must not be able to brick the Core" degrade.

A dependency upgrade turned two security assertions into decoration, and the
suite reported success the whole time.

## What this file does

`conftest.served_paths` and `conftest.served_routes` are the one producer of
"what does this app serve" now. This file is the thing that notices when that
producer goes blind again — by whatever mechanism, since the next FastAPI is
under no obligation to keep `original_router` either.

Two independent answers are compared: the walk over route objects, and the app
describing itself through `openapi()`. A blind walk disagrees with the app. That
is a much better alarm than a number in this file, which would only ever be
updated by somebody who had already noticed.
"""
from __future__ import annotations

import os

import pytest

from wavr.app import create_app


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A Core with the multidevice surfaces on, so there are nested routes to miss."""
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "wavr.db"))
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(tmp_path / "house.json"))
    monkeypatch.setenv("WAVR_LOCAL_TOKEN", "")
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_NODES_ENABLED", "1")
    monkeypatch.setenv("WAVR_PEERS_ENABLED", "1")
    return create_app()


def test_the_route_walk_still_sees_everything(app, served_routes, served_paths):
    """The walk and the app's own description must agree.

    Not "the walk finds at least N routes" — a threshold is a number somebody
    lowers. Two independent answers to the same question either match or one of
    them is broken, and that holds whatever FastAPI does next.
    """
    andadas = {r.path for r in served_routes(app)}
    descritas = served_paths(app)
    faltando = sorted(descritas - andadas)
    assert not faltando, (
        "the route walk in conftest cannot reach endpoints the app says it "
        f"serves — {len(faltando)} of them, starting with {faltando[:5]}. "
        "Something changed how routes are stored; every test that enumerates "
        "routes is now measuring less than it claims.")


def test_a_scan_of_app_routes_alone_would_not_have_seen_them(app, served_paths):
    """The blindness is real, and this pins the fact rather than the version.

    If a future FastAPI goes back to flattening routes into `app.routes`, this
    test fails and the honest response is to delete it — the hazard is gone. It
    exists so that "we handle this" stays a measured statement instead of a
    comment somebody wrote once.
    """
    cru = {getattr(r, "path", "") for r in app.routes}
    perdidas = served_paths(app) - cru
    assert perdidas, (
        "`app.routes` now contains every served path on its own, so the "
        "wrapper this suite works around is gone. Simplify `served_routes` "
        "and delete this test rather than leaving a workaround nobody needs.")


@pytest.mark.parametrize("caminho", [
    # One from each of the four tests that went blind, so the specific claims
    # they make are reachable and not just the general count.
    "/api/uwb/sessions/{session_id}/claim",
    "/api/nodes/enroll",
    "/api/peers",
    "/api/settings/{key}",
])
def test_the_endpoints_the_blind_tests_guard_are_reachable(app, served_paths, caminho):
    assert caminho in served_paths(app), (
        f"{caminho} is not visible to the suite, so anything asserted about it "
        f"is asserted about nothing")
