"""Fail-closed regression test for wavr.api_devices's `delete_deps` sentinel
(sweep #13, mirrors test_api_nodes.py's test_admin_router_fails_closed_without_deps).
Mounted on a MINIMAL FastAPI app (no create_app -- app.py is intentionally not
imported here)."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from wavr.api_devices import build_devices_router
from wavr.devices import DeviceStore


def test_devices_router_fails_closed_without_delete_deps(tmp_path):
    # Appsec sweep #13: delete_deps=None used to default to [] (NO AUTH). A
    # forgotten wiring in app.py must never expose revoke/set_role
    # unauthenticated -- the default must DENY instead.
    store = DeviceStore(str(tmp_path / "devices.db"))
    app = FastAPI()
    app.include_router(build_devices_router(store))  # no delete_deps!
    client = TestClient(app)

    assert client.delete("/api/devices/ghost").status_code == 403
    assert client.post("/api/devices/ghost/role",
                       json={"role": "central"}).status_code == 403
    # The read stays reachable at the router level (app.py's own gating covers it).
    assert client.get("/api/devices").status_code == 200


def test_devices_router_fails_closed_with_empty_delete_deps(tmp_path):
    # Belt-and-suspenders: an explicitly-empty list is treated the same as
    # "forgot to wire it" -- also denies, rather than silently running open.
    store = DeviceStore(str(tmp_path / "devices.db"))
    app = FastAPI()
    app.include_router(build_devices_router(store, delete_deps=[]))
    client = TestClient(app)
    assert client.delete("/api/devices/ghost").status_code == 403
