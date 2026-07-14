"""Fail-closed regression test for wavr.api_connectors's `write_deps` sentinel
(sweep #13, mirrors test_api_nodes.py's test_admin_router_fails_closed_without_deps).
Mounted on a MINIMAL FastAPI app (no create_app -- app.py is intentionally not
imported here)."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from wavr.api_connectors import build_connectors_router
from wavr.connector_store import ConnectorStore


def _empty_catalog() -> list:
    return []


def test_connectors_router_fails_closed_without_write_deps(tmp_path):
    # Appsec sweep #13: write_deps=None used to default to [] (NO AUTH). A
    # forgotten wiring in app.py must never expose the enable route
    # unauthenticated -- this is the EGRESS-CONTROL plane (including the
    # assistant-cloud kill switch), so the default must DENY instead.
    store = ConnectorStore(str(tmp_path / "connectors.db"))
    app = FastAPI()
    app.include_router(build_connectors_router(store, _empty_catalog))  # no write_deps!
    client = TestClient(app)

    assert client.post("/api/connectors/narrator/enable",
                       json={"enabled": True}).status_code == 403
    # The reads stay reachable at the router level (app.py's own gating covers them).
    assert client.get("/api/connectors").status_code == 200
    assert client.get("/api/connectors/catalog").status_code == 200


def test_connectors_router_fails_closed_with_empty_write_deps(tmp_path):
    # Belt-and-suspenders: an explicitly-empty list is treated the same as
    # "forgot to wire it" -- also denies, rather than silently running open.
    store = ConnectorStore(str(tmp_path / "connectors.db"))
    app = FastAPI()
    app.include_router(build_connectors_router(store, _empty_catalog, write_deps=[]))
    client = TestClient(app)
    assert client.post("/api/connectors/narrator/enable",
                       json={"enabled": True}).status_code == 403
