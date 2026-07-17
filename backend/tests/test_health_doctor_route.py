"""GET /api/health/doctor: auth gate (mirrors test_a5_hardening.py's /api/health
coverage) + response shape + one end-to-end auto-fix pass with a fake stalled
source, proving the SAFE-AUTO allowlist actually revives a source that is
enabled=True but stalled -- and does NOTHING when the two-factor auto-fix
gate (WAVR_NET_DOCTOR_AUTOFIX env AND auto_fix=true) isn't fully satisfied."""
import time

from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.storage import Storage
from wavr.hub import Hub
from wavr.fusion import FusionEngine
from wavr.camera_store import CameraStore
from wavr.sources.simulated import SimulatedSource


async def _up() -> bool:
    return True


def _app(**kw):
    return create_app(sources=[], storage=Storage(":memory:"), hub=Hub(),
                      fusion=FusionEngine(), camera_store=CameraStore(":memory:"),
                      health_resolvers={}, health_check=_up, **kw)


# ---- auth gate (same tier as GET /api/health) --------------------------------

def test_doctor_403_without_local_header(monkeypatch):
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    with TestClient(_app()) as c:      # no default header
        assert c.get("/api/health/doctor").status_code == 403


def test_doctor_200_with_local_header(monkeypatch):
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    with TestClient(_app(), headers={"X-Wavr-Local": "1"}) as c:
        r = c.get("/api/health/doctor")
        assert r.status_code == 200


# ---- response shape ------------------------------------------------------------

def test_doctor_response_shape(monkeypatch):
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    with TestClient(_app(), headers={"X-Wavr-Local": "1"}) as c:
        body = c.get("/api/health/doctor").json()
        assert set(body.keys()) == {"checks", "auto_fixed", "suggestions", "recent_auto_fixes"}
        assert isinstance(body["checks"], list) and body["checks"]
        ids = {c_["id"] for c_ in body["checks"]}
        assert {"internet", "dns", "gateway_identity", "rogue_dhcp",
                "mdns_advertise", "inventory_freshness", "signal_freshness"} <= ids


def test_doctor_default_off_never_fixes_even_with_query_param(monkeypatch):
    # WAVR_NET_DOCTOR_AUTOFIX unset (default OFF) -- auto_fix=true in the query
    # string alone must NOT be enough (two-factor gate).
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    monkeypatch.delenv("WAVR_NET_DOCTOR_AUTOFIX", raising=False)
    with TestClient(_app(), headers={"X-Wavr-Local": "1"}) as c:
        body = c.get("/api/health/doctor?auto_fix=true").json()
        assert body["auto_fixed"] == []


# ---- end-to-end auto-fix: a stalled source actually gets revived --------------

def _flaky_source_factory():
    """First events() call ends immediately (simulating a stalled/crashed
    source, enabled=True but not active) -- every call after that behaves
    like a normal SimulatedSource, so the auto-fix's restart-cycle can be
    observed to actually bring it back to active=True."""
    state = {"calls": 0}

    class _FlakySource:
        async def events(self):
            state["calls"] += 1
            if state["calls"] == 1:
                return
                yield   # unreachable; keeps this an async generator function
            async for ev in SimulatedSource(interval=0.01).events():
                yield ev

    return lambda: _FlakySource()


def test_doctor_autofix_revives_a_stalled_enabled_source(monkeypatch):
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    monkeypatch.setenv("WAVR_NET_DOCTOR_AUTOFIX", "1")
    app = create_app(
        sources=[("flaky", _flaky_source_factory(), True)],
        storage=Storage(":memory:"), hub=Hub(), fusion=FusionEngine(),
        camera_store=CameraStore(":memory:"), health_resolvers={}, health_check=_up,
    )
    with TestClient(app, headers={"X-Wavr-Local": "1"}) as c:
        time.sleep(0.1)   # let the first (self-terminating) events() call complete
        pre = c.get("/api/system").json()
        flaky_pre = next(s for s in pre["sources"] if s["name"] == "flaky")
        assert flaky_pre["enabled"] is True and flaky_pre["active"] is False

        body = c.get("/api/health/doctor?auto_fix=true").json()
        assert any(c_["id"] == "capture_stalled:flaky" for c_ in body["checks"])
        assert any(a["target"] == "flaky" and a["kind"] == "restart_source"
                   for a in body["auto_fixed"])
        assert any(a["target"] == "flaky" for a in body["recent_auto_fixes"])

        post = c.get("/api/system").json()
        flaky_post = next(s for s in post["sources"] if s["name"] == "flaky")
        assert flaky_post["active"] is True


def test_doctor_autofix_off_only_suggests_for_a_stalled_source(monkeypatch):
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    monkeypatch.delenv("WAVR_NET_DOCTOR_AUTOFIX", raising=False)   # default OFF
    app = create_app(
        sources=[("flaky", _flaky_source_factory(), True)],
        storage=Storage(":memory:"), hub=Hub(), fusion=FusionEngine(),
        camera_store=CameraStore(":memory:"), health_resolvers={}, health_check=_up,
    )
    with TestClient(app, headers={"X-Wavr-Local": "1"}) as c:
        time.sleep(0.1)
        body = c.get("/api/health/doctor?auto_fix=true").json()
        assert body["auto_fixed"] == []
        assert any(s["id"] == "capture_stalled:flaky" for s in body["suggestions"])

        post = c.get("/api/system").json()
        flaky_post = next(s for s in post["sources"] if s["name"] == "flaky")
        assert flaky_post["active"] is False   # never touched


# ---- discovery_reach (CL-02, PR1): endpoint wires the structured verdict ------

class _FakeInv:
    """Inventory with N ARP-visible devices, enough to cross DISCOVERY_MIN_ARP so the
    probe seam is actually consulted (a real inventory is empty in tests -> small-net)."""
    def __init__(self, n):
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


def test_doctor_discovery_reach_names_multicast_dead_end_to_end(monkeypatch):
    # 15 devices reachable via ARP but the injected multicast probe says 0 answered ->
    # the endpoint returns the STRUCTURED verdict (never a flat string), no real socket.
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)

    async def _probe():
        return 0

    app = _app(net_inventory=_FakeInv(15), net_mcast_probe=_probe)
    with TestClient(app, headers={"X-Wavr-Local": "1"}) as c:
        body = c.get("/api/health/doctor").json()
    dr = next(x for x in body["checks"] if x["id"] == "discovery_reach")
    assert dr["ok"] is False
    assert dr["verdict"]["cause"] == "MULTICAST_DEAD_UNKNOWN"
    assert dr["verdict"]["arp_count"] == 15 and dr["verdict"]["mcast_responders"] == 0
    assert dr["verdict"]["copy_key"] == "discovery_multicast_dead"
    # report-only: the pathology is NEVER auto-fixed (no router touch)
    assert not any(a.get("target") == "discovery_reach" for a in body["auto_fixed"])


def test_doctor_discovery_reach_healthy_when_probe_answers(monkeypatch):
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)

    async def _probe():
        return 6

    app = _app(net_inventory=_FakeInv(15), net_mcast_probe=_probe)
    with TestClient(app, headers={"X-Wavr-Local": "1"}) as c:
        body = c.get("/api/health/doctor").json()
    dr = next(x for x in body["checks"] if x["id"] == "discovery_reach")
    assert dr["ok"] is True and dr["verdict"]["cause"] is None
