"""App-level wiring for the rogue/multiple-DHCP-server detector (defensive-inventory
#7, collectors-lote2): opt-in via config or direct injection, `/api/status`
`features.rogue_dhcp`, `/api/alerts` merge with the rogue-device alert log,
and start/stop through the real create_app lifespan. Mirrors
test_internet_monitor_wiring.py/test_notifier_wiring.py's style -- an
injected monitor always wins over cfg, same as notify/rules_publish/narrator.

An injected `RogueDhcpMonitor` (built with a fake `collect`, zero real
sockets) is used throughout rather than driving the cfg-triggered real
`wavr.sources.dhcp.DHCPCollector` path, which would open a real UDP socket --
that lazy real-collector path is covered by test_dhcp_monitor.py/
test_dhcp_source.py's own unit tests instead."""
import asyncio

from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.camera_store import CameraStore
from wavr.dhcp_monitor import RogueDhcpMonitor
from wavr.netinventory_service import NetworkInventoryService


def _build(dhcp_monitor=None, notify=None):
    return create_app(
        sources=[], camera_store=CameraStore(":memory:"),
        dhcp_monitor=dhcp_monitor, notify=notify,
    )


# ---- opt-in default-off: inert, feature flag false, no dhcp alerts ----------

def test_rogue_dhcp_off_by_default_inert(monkeypatch):
    monkeypatch.delenv("WAVR_NET_DHCP_MONITOR", raising=False)
    app = _build()
    with TestClient(app) as client:
        body = client.get("/api/status").json()
        assert body["features"]["rogue_dhcp"] is False
        assert client.get("/api/alerts").json() == {"alerts": []}


def test_features_rogue_dhcp_reflects_config_flag(monkeypatch):
    monkeypatch.setenv("WAVR_NET_DHCP_MONITOR", "1")
    try:
        # Inject a harmless fake monitor so the cfg-driven real-DHCP-snoop
        # pathway in create_app is bypassed (an injected monitor always wins,
        # same rule as notify/rules_publish/narrator/internet_monitor) -- this
        # only checks that `features.rogue_dhcp` mirrors cfg.
        async def collect():
            return {}
        m = RogueDhcpMonitor(collect=collect)
        app = _build(dhcp_monitor=m)
        with TestClient(app) as client:
            assert client.get("/api/status").json()["features"]["rogue_dhcp"] is True
    finally:
        monkeypatch.delenv("WAVR_NET_DHCP_MONITOR", raising=False)


# ---- injected monitor: starts/stops with lifespan, alerts merge -------------

async def test_injected_monitor_starts_with_lifespan_and_runs_checks():
    calls = []

    async def collect():
        calls.append(1)
        return {}
    m = RogueDhcpMonitor(collect=collect, interval=0.01)
    app = _build(dhcp_monitor=m)
    async with app.router.lifespan_context(app):
        for _ in range(50):
            if calls:
                break
            await asyncio.sleep(0.01)
        assert calls                                   # background task ran >=1 check
    # lifespan exit above must have stopped the task cleanly (cancel-safe stop()
    # never raises) -- reaching here without an exception proves it.


async def test_injected_monitor_rogue_alert_merges_into_api_alerts():
    # First cycle settles the baseline (known_servers=None auto-adopts {}),
    # then an extra server id showing up for >=alert_threshold cycles fires.
    responses = iter([{}, {"10.0.0.99": {}}, {"10.0.0.99": {}}])

    async def collect():
        try:
            return next(responses)
        except StopIteration:
            return {"10.0.0.99": {}}
    m = RogueDhcpMonitor(collect=collect, interval=0.005, alert_threshold=2)
    app = _build(dhcp_monitor=m)
    with TestClient(app) as client:
        async with app.router.lifespan_context(app):
            for _ in range(300):
                if m.recent_alerts():
                    break
                await asyncio.sleep(0.01)
            assert m.recent_alerts()                    # sanity: the monitor itself fired
        body = client.get("/api/alerts").json()
    dhcp_alerts = [a for a in body["alerts"] if a["kind"] == "rogue_dhcp"]
    assert len(dhcp_alerts) == 1
    assert dhcp_alerts[0]["extra_server"] == "10.0.0.99"
    assert set(dhcp_alerts[0]) >= {"ts", "kind", "severity", "extra_server",
                                   "known_servers", "observed_servers"}


async def test_alerts_merge_rogue_device_and_rogue_dhcp_sorted_by_ts():
    # Rogue-device alert (via a real NetworkInventoryService with an unknown
    # MAC) + a rogue-DHCP alert (via the injected monitor) both surface,
    # tagged by `kind`, in one chronologically-sorted list.
    async def scan():
        return "192.168.0.23  24-0A-C4-AA-BB-CC     dynamic\n"

    inventory = NetworkInventoryService(known_macs=set(), scan=scan, interval=0)
    await inventory.scan_once()   # seeds one rogue-device alert (unknown OUI)

    async def collect():
        return {"10.0.0.99": {}}
    m = RogueDhcpMonitor(collect=collect, known_servers=set(), alert_threshold=1, interval=0)
    await m.check_once()          # fires immediately (alert_threshold=1)

    from wavr.api_inventory import build_inventory_router
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(build_inventory_router(inventory, dhcp_monitor=m))
    with TestClient(app) as client:
        body = client.get("/api/alerts").json()
    kinds = {a["kind"] for a in body["alerts"]}
    assert kinds == {"rogue_device", "rogue_dhcp"}
    ts_values = [a["ts"] for a in body["alerts"]]
    assert ts_values == sorted(ts_values)


def test_dhcp_monitor_absent_alerts_endpoint_unchanged_shape():
    # No dhcp_monitor wired in -> /api/alerts keeps its pre-collectors-lote2
    # shape (rogue-device alerts only, each additionally tagged "kind").
    from wavr.api_inventory import build_inventory_router
    from fastapi import FastAPI

    async def scan():
        return "192.168.0.23  24-0A-C4-AA-BB-CC     dynamic\n"

    inventory = NetworkInventoryService(known_macs=set(), scan=scan, interval=0)
    asyncio.run(inventory.scan_once())
    app = FastAPI()
    app.include_router(build_inventory_router(inventory))
    with TestClient(app) as client:
        body = client.get("/api/alerts").json()
    assert len(body["alerts"]) == 1
    assert body["alerts"][0]["kind"] == "rogue_device"


def test_dhcp_fp_and_rogue_monitor_bind_distinct_ports():
    # Audit fix #7: BUILD-NOTES' self-flagged RISK #1 ("dhcp_fp/rogue-DHCP-
    # monitor both bind UDP port 67, double-bind risk if both are enabled
    # together") is factually wrong and REFUTED -- dhcp_fp is a passive
    # listener on the DHCP *server* port (67, where client broadcasts land);
    # the rogue-monitor's DHCPCollector listens on the DHCP *client* port
    # (68, where server OFFER/ACK land). Different ports, no collision, no
    # double-bind possible. Network-free guard: just the two constants.
    from wavr.sources.dhcp import DHCP_CLIENT_PORT, DHCP_SERVER_PORT as MONITOR_SERVER_PORT
    from wavr.sources.dhcp_fp import DHCP_SERVER_PORT as FP_SERVER_PORT

    assert FP_SERVER_PORT == 67
    assert DHCP_CLIENT_PORT == 68
    assert MONITOR_SERVER_PORT == 67
    assert FP_SERVER_PORT != DHCP_CLIENT_PORT
