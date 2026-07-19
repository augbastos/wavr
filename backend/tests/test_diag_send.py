"""Diagnostics sending (opt-in, never forced): the manual POST /api/health/doctor/send route,
the `diagnostics` connector (default OFF), the auto-send path in /api/health/doctor,
and the privacy contract (re-redaction — a raw MAC can never leave)."""
import re
import time

from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.storage import Storage
from wavr.hub import Hub
from wavr.fusion import FusionEngine
from wavr.camera_store import CameraStore
from wavr.connector_store import ConnectorStore
from wavr.connectors.diag import send_report

_RAW_MAC = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")


def _app(**kw):
    return create_app(sources=[], storage=Storage(":memory:"), hub=Hub(),
                      fusion=FusionEngine(), camera_store=CameraStore(":memory:"),
                      health_resolvers={}, health_check=_up, **kw)


async def _up() -> bool:
    return True


class _FakeInv:
    """15 ARP-visible devices so discovery_reach crosses DISCOVERY_MIN_ARP."""
    def __init__(self, n=15):
        self._n = n
    def latest_inventory(self):
        return [object() for _ in range(self._n)]
    def last_scan_ts(self):
        return None
    def recent_alerts(self, limit=50):
        return []
    async def scan_once(self):
        return []
    async def start(self):
        return None
    async def stop(self):
        return None


# ---- the connector exists, DEFAULT OFF --------------------------------------

def test_diagnostics_connector_in_catalog_default_off(monkeypatch):
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    monkeypatch.delenv("WAVR_DIAG_ENDPOINT", raising=False)
    with TestClient(_app(), headers={"X-Wavr-Local": "1"}) as c:
        body = c.get("/api/connectors").json()
    d = next(x for x in body["connectors"] if x["id"] == "diagnostics")
    assert d["active"] is False and d["override"] is None          # nothing on by default
    assert d["available"] is False                                 # no endpoint configured
    assert d["direction"] == "outbound"


# ---- pure send_report: gate + redaction --------------------------------------

def test_send_report_denied_when_connector_off_and_not_manual():
    s = ConnectorStore(":memory:")
    res = send_report(s, "https://diag.example/report", "hi",
                      transport=lambda *a, **k: {"ok": True})
    assert res["ok"] is False and "off" in res["reason"]


def test_send_report_manual_bypasses_toggle_but_not_redaction():
    # manual tap = per-action consent; the payload is still scrubbed (defense in depth)
    s = ConnectorStore(":memory:")
    seen = {}
    def _t(url, payload, **kw):
        seen.update(payload); return {"ok": True}
    res = send_report(s, "https://diag.example/report",
                      "gateway aa:bb:cc:dd:ee:ff misbehaving", manual=True, transport=_t)
    assert res["ok"] is True
    assert _RAW_MAC.search(seen["report"]) is None
    assert "aa:bb:cc:**:**:**" in seen["report"]


def test_send_report_enabled_toggle_allows_auto():
    s = ConnectorStore(":memory:")
    s.upsert("diagnostics", "builtin", "Diagnostics reporting")
    s.set_enabled("diagnostics", True)
    res = send_report(s, "https://diag.example/report", "x",
                      transport=lambda *a, **k: {"ok": True})
    assert res["ok"] is True


def _block_egress(store):
    # flip the reserved sys:egress master row OFF (System-tab "Egress: blocked")
    store.upsert("sys:egress", "system", "egress")
    store.set_enabled("sys:egress", False)


def test_send_report_refuses_when_egress_master_blocked_even_manual():
    # the module self-guards on the egress master: the operator's global block wins over a
    # manual tap AND the standing toggle — no caller can ship a report while egress is blocked.
    s = ConnectorStore(":memory:")
    _block_egress(s)
    sent = []
    def _t(url, payload, **kw):
        sent.append(payload); return {"ok": True}
    manual = send_report(s, "https://diag.example/report", "x", manual=True, transport=_t)
    assert manual["ok"] is False and "egress" in manual["reason"]
    s.upsert("diagnostics", "builtin", "Diagnostics reporting"); s.set_enabled("diagnostics", True)
    auto = send_report(s, "https://diag.example/report", "x", transport=_t)
    assert auto["ok"] is False and "egress" in auto["reason"]
    assert sent == []   # transport never even attempted while egress is blocked


def test_send_report_transport_failure_degrades_clean():
    def _boom(*a, **k):
        raise OSError("unreachable")
    res = send_report(ConnectorStore(":memory:"), "https://diag.example/report",
                      "x", manual=True, transport=_boom)
    assert res["ok"] is False and "unreachable" in res["reason"]


# ---- manual route ------------------------------------------------------------

def test_diag_send_409_without_endpoint(monkeypatch):
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    monkeypatch.delenv("WAVR_DIAG_ENDPOINT", raising=False)
    with TestClient(_app(), headers={"X-Wavr-Local": "1"}) as c:
        r = c.post("/api/health/doctor/send", json={"report": "hello"})
    assert r.status_code == 409


def test_diag_send_manual_delivers_via_injected_sender(monkeypatch):
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    monkeypatch.setenv("WAVR_DIAG_ENDPOINT", "https://diag.example/report")
    sent = []

    async def _sender(report):
        sent.append(report)
        return {"ok": True, "status": 200}

    with TestClient(_app(diag_sender=_sender), headers={"X-Wavr-Local": "1"}) as c:
        r = c.post("/api/health/doctor/send", json={"report": "doctor says hi"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert sent == ["doctor says hi"]


def test_diag_send_403_without_local_header(monkeypatch):
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    monkeypatch.setenv("WAVR_DIAG_ENDPOINT", "https://diag.example/report")
    with TestClient(_app()) as c:      # no X-Wavr-Local
        assert c.post("/api/health/doctor/send", json={"report": "x"}).status_code == 403


# ---- auto-send path (standing consent via the connector toggle) ---------------

def _degraded_doctor_app(store, sender, monkeypatch):
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    monkeypatch.setenv("WAVR_DIAG_ENDPOINT", "https://diag.example/report")

    async def _probe():
        return 0            # 15 devices, 0 responders -> discovery_reach ok=False

    async def _viability():
        return None

    return _app(net_inventory=_FakeInv(), net_mcast_probe=_probe,
                net_mcast_viability=_viability, connector_store=store,
                diag_sender=sender)


def test_doctor_auto_sends_when_toggle_on(monkeypatch):
    store = ConnectorStore(":memory:")
    store.upsert("diagnostics", "builtin", "Diagnostics reporting")
    store.set_enabled("diagnostics", True)
    sent = []

    async def _sender(report):
        sent.append(report)
        return {"ok": True}

    app = _degraded_doctor_app(store, _sender, monkeypatch)
    with TestClient(app, headers={"X-Wavr-Local": "1"}) as c:
        c.get("/api/health/doctor")
        time.sleep(0.2)     # fire-and-forget task
    assert len(sent) == 1
    assert "discovery_reach" in sent[0]
    assert _RAW_MAC.search(sent[0]) is None


def test_doctor_never_auto_sends_when_toggle_off(monkeypatch):
    # DEFAULT state: no row -> nothing leaves, even with an endpoint configured.
    store = ConnectorStore(":memory:")
    sent = []

    async def _sender(report):
        sent.append(report)
        return {"ok": True}

    app = _degraded_doctor_app(store, _sender, monkeypatch)
    with TestClient(app, headers={"X-Wavr-Local": "1"}) as c:
        c.get("/api/health/doctor")
        time.sleep(0.2)
    assert sent == []       # opt-in means OPT-IN


def _healthy_doctor_app(store, sender, monkeypatch):
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    monkeypatch.setenv("WAVR_DIAG_ENDPOINT", "https://diag.example/report")

    async def _probe():
        return 6            # 15 devices, 6 responders -> discovery_reach ok=True (no problem)

    async def _viability():
        return None

    return _app(net_inventory=_FakeInv(), net_mcast_probe=_probe,
                net_mcast_viability=_viability, connector_store=store,
                diag_sender=sender)


def test_doctor_toggle_on_but_healthy_never_auto_sends(monkeypatch):
    # QA-lens gap: auto-send is gated on `any(check.ok is False)`. With the toggle ON but the
    # doctor HEALTHY, nothing must leave -- this guards the exact clause that separates
    # "opt-in reporting WHEN SOMETHING'S WRONG" from "phone home on every poll". Drop/invert
    # that clause and THIS is the test that turns red.
    store = ConnectorStore(":memory:")
    store.upsert("diagnostics", "builtin", "Diagnostics reporting")
    store.set_enabled("diagnostics", True)
    sent = []

    async def _sender(report):
        sent.append(report)
        return {"ok": True}

    app = _healthy_doctor_app(store, _sender, monkeypatch)
    with TestClient(app, headers={"X-Wavr-Local": "1"}) as c:
        body = c.get("/api/health/doctor").json()
        time.sleep(0.2)
    dr = next(x for x in body["checks"] if x["id"] == "discovery_reach")
    assert dr["ok"] is True                 # sanity: the doctor really is healthy this run
    assert not any(x["ok"] is False for x in body["checks"])
    assert sent == []                       # healthy + toggle ON => still nothing leaves


# ---- the REAL send wiring (no injected diag_sender) --------------------------

def test_diag_send_route_wires_to_real_send_report(monkeypatch):
    # QA-lens gap: every other send test injects a fake diag_sender, bypassing the real
    # asyncio.to_thread(diag_send_report, _connectors, cfg.diag_endpoint, report, manual=True)
    # glue. Exercise it for real WITHOUT leaving the box: point the endpoint at a refused local
    # port, do NOT inject a sender, and assert the route surfaces a real transport failure --
    # proving the store + manual=True + endpoint actually reach send_report.
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    monkeypatch.setenv("WAVR_DIAG_ENDPOINT", "http://127.0.0.1:1/report")   # nothing listens on :1
    store = ConnectorStore(":memory:")
    with TestClient(_app(connector_store=store), headers={"X-Wavr-Local": "1"}) as c:
        r = c.post("/api/health/doctor/send", json={"report": "gw aa:bb:cc:dd:ee:ff"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body.get("reason")   # a real connection failure, not a mock
    assert body["status"] is None
