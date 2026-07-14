"""Fail-closed regression test for wavr.api_identity's `write_deps` sentinel
(sweep #13, mirrors test_api_nodes.py's test_admin_router_fails_closed_without_deps).
Mounted on a MINIMAL FastAPI app (no create_app -- app.py is intentionally not
imported here)."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from wavr.api_identity import build_identity_router
from wavr.identity_store import IdentityStore


def test_identity_router_fails_closed_without_write_deps(tmp_path):
    # Appsec sweep #13: write_deps=None used to default to [] (NO AUTH). A
    # forgotten wiring in app.py must never expose register/set_details/
    # unregister unauthenticated -- the default must DENY instead.
    store = IdentityStore(str(tmp_path / "identity.db"))
    app = FastAPI()
    app.include_router(build_identity_router(store))  # no write_deps!
    client = TestClient(app)

    assert client.post("/api/identity/devices",
                       json={"devices": [{"address": "aa:bb:cc:dd:ee:ff"}]}
                       ).status_code == 403
    assert client.patch("/api/identity/devices/aa:bb:cc:dd:ee:ff/details",
                        json={"on": True}).status_code == 403
    assert client.delete("/api/identity/devices/aa:bb:cc:dd:ee:ff").status_code == 403
    # The reads stay reachable at the router level (app.py's own gating covers them).
    assert client.get("/api/identity/devices").status_code == 200


def test_identity_router_fails_closed_with_empty_write_deps(tmp_path):
    # Belt-and-suspenders: an explicitly-empty list is treated the same as
    # "forgot to wire it" -- also denies, rather than silently running open.
    store = IdentityStore(str(tmp_path / "identity.db"))
    app = FastAPI()
    app.include_router(build_identity_router(store, write_deps=[]))
    client = TestClient(app)
    assert client.post("/api/identity/devices",
                       json={"devices": [{"address": "aa:bb:cc:dd:ee:ff"}]}
                       ).status_code == 403
