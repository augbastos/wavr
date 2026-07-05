"""Gateway-MAC-identity tracker (gateway-identity-rogue-dhcp, inventory feature #2):
two-factor debounce, throttled + escalating severity, persisted-across-restart
baseline, and the wiring into NetworkInventoryService / /api/alerts. All
mock-tested -- zero real network, an injectable monotonic clock for throttle
timing, an in-memory sqlite store for persistence."""
import tempfile
from pathlib import Path

from wavr.gateway_monitor import (DEFAULT_DEBOUNCE_CYCLES, DEFAULT_THROTTLE_S,
                                  GatewayAlert, GatewayBindingStore,
                                  GatewayIdentityMonitor)

GW = "192.168.0.1"
LEGIT = "aa:bb:cc:dd:ee:ff"
ROGUE = "de:ad:be:ef:00:01"
ROGUE2 = "11:22:33:44:55:66"


def _monitor(**kw):
    clock = kw.pop("clock", None)
    if clock is not None:
        kw["now"] = lambda: clock[0]
    fired = []
    kw.setdefault("on_alert", fired.append)
    m = GatewayIdentityMonitor(**kw)
    return m, fired


# ---- Wavr's OWN derived constants (not a proprietary tool's MSP-fleet numbers) ----------

def test_default_constants_are_wavr_own_home_values():
    # ~30s scan cadence -> 2 consecutive cycles (~60s) debounce; 30-min throttle.
    # Explicitly NOT a proprietary tool's 4h window / 1h throttle (see module docstring).
    assert DEFAULT_DEBOUNCE_CYCLES == 2
    assert DEFAULT_THROTTLE_S == 1800.0


# ---- Factor 2: two-factor debounce -----------------------------------------

def test_first_determination_settles_baseline_without_alerting():
    m, fired = _monitor(debounce_cycles=2)
    m.observe(GW, LEGIT)
    assert fired == []
    assert m.status()["trusted_bindings"] == {GW: LEGIT}


def test_change_must_persist_across_debounce_cycles_before_firing():
    m, fired = _monitor(debounce_cycles=2)
    m.observe(GW, LEGIT)            # baseline
    m.observe(GW, ROGUE)           # anomaly cycle 1 -> silent (Factor 2 unmet)
    assert fired == []
    m.observe(GW, ROGUE)           # anomaly cycle 2 -> fires
    assert len(fired) == 1
    a = fired[0]
    assert (a.gateway_ip, a.trusted_mac, a.observed_mac) == (GW, LEGIT, ROGUE)
    assert a.severity == "alert"   # first debounced detection, never critical


def test_router_reboot_same_mac_never_fires():
    # A reboot / firmware update keeps the SAME NIC MAC -> Factor 1 never met.
    m, fired = _monitor(debounce_cycles=2)
    m.observe(GW, LEGIT)
    for _ in range(10):
        m.observe(GW, LEGIT)
    assert fired == []


def test_single_blip_that_clears_next_cycle_never_fires():
    # One anomalous cycle then back to trusted -> debounce (Factor 2) never met.
    m, fired = _monitor(debounce_cycles=2)
    m.observe(GW, LEGIT)
    m.observe(GW, ROGUE)           # blip, 1 cycle
    m.observe(GW, LEGIT)           # cleared
    m.observe(GW, ROGUE)           # blip again, only 1 consecutive -> still silent
    assert fired == []


def test_neutral_no_gateway_cycle_neither_advances_nor_resets():
    m, fired = _monitor(debounce_cycles=2)
    m.observe(GW, LEGIT)
    m.observe(GW, ROGUE)           # anomaly cycle 1
    m.observe(None, None)          # no gateway resolved -> neutral
    m.observe("", "")              # also neutral
    assert fired == []
    m.observe(GW, ROGUE)           # anomaly cycle 2 (neutral did not reset it)
    assert len(fired) == 1


def test_different_new_mac_restarts_the_debounce_counter():
    m, fired = _monitor(debounce_cycles=2)
    m.observe(GW, LEGIT)
    m.observe(GW, ROGUE)           # rogue A, cycle 1
    m.observe(GW, ROGUE2)          # rogue B (different) -> counter restarts at 1
    assert fired == []
    m.observe(GW, ROGUE2)          # rogue B, cycle 2 -> fires for B
    assert len(fired) == 1
    assert fired[0].observed_mac == ROGUE2


# ---- throttle + honest severity escalation ---------------------------------

def test_sustained_change_refires_as_critical_after_throttle_window():
    clock = [0.0]
    m, fired = _monitor(debounce_cycles=2, throttle_s=100, clock=clock)
    m.observe(GW, LEGIT)
    m.observe(GW, ROGUE)
    m.observe(GW, ROGUE)           # t=0 -> alert
    assert [a.severity for a in fired] == ["alert"]
    m.observe(GW, ROGUE)           # still within throttle window -> suppressed
    assert len(fired) == 1
    clock[0] = 150.0               # past the 100s throttle window
    m.observe(GW, ROGUE)           # STILL rogue -> sustained -> critical
    assert [a.severity for a in fired] == ["alert", "critical"]


def test_selfheal_then_new_rogue_starts_fresh_at_alert_not_critical():
    clock = [0.0]
    m, fired = _monitor(debounce_cycles=2, throttle_s=100, clock=clock)
    m.observe(GW, LEGIT)
    m.observe(GW, ROGUE)
    m.observe(GW, ROGUE)           # -> alert
    m.observe(GW, LEGIT)           # self-heals -> forgets throttle/escalation
    clock[0] = 500.0
    m.observe(GW, ROGUE2)
    m.observe(GW, ROGUE2)          # a genuinely new event -> fresh alert
    assert [a.severity for a in fired] == ["alert", "alert"]


# ---- operator allowlist (router swap / failover pair) ----------------------

def test_allowlisted_gateway_mac_is_always_trusted():
    m, fired = _monitor(debounce_cycles=2, known_macs={"AA-BB-CC-DD-EE-FF"})
    m.observe(GW, LEGIT)           # allowlisted (dash form normalised) -> trusted
    for _ in range(5):
        m.observe(GW, LEGIT)
    assert fired == []


def test_allowlist_swap_stops_alerts_but_unlisted_mac_still_fires():
    m, fired = _monitor(debounce_cycles=2, known_macs={ROGUE2})
    m.observe(GW, LEGIT)           # auto-baseline
    m.observe(GW, ROGUE)           # untrusted
    m.observe(GW, ROGUE)           # -> fires
    assert len(fired) == 1
    m.observe(GW, ROGUE2)          # allowlisted new router -> trusted, no new alert
    m.observe(GW, ROGUE2)
    assert len(fired) == 1


# ---- callback / ring robustness --------------------------------------------

def test_on_alert_exception_is_swallowed():
    def boom(_a):
        raise RuntimeError("notifier down")
    m = GatewayIdentityMonitor(debounce_cycles=1, on_alert=boom)
    m.observe(GW, LEGIT)
    m.observe(GW, ROGUE)           # debounce_cycles=1 -> fires immediately
    # a raising callback must not propagate; the alert is still logged
    assert len(m.recent_alerts()) == 1


def test_alert_ring_is_bounded():
    m = GatewayIdentityMonitor(debounce_cycles=1, throttle_s=0, max_alerts=3)
    # throttle_s=0 -> every anomalous cycle re-fires; alternate two rogues so
    # each cycle is a "new identity" that re-fires under a zero throttle.
    m.observe(GW, LEGIT)
    for i in range(10):
        m.observe(GW, ROGUE if i % 2 == 0 else ROGUE2)
    assert len(m.recent_alerts()) <= 3


def test_gateway_alert_to_dict_shape():
    a = GatewayAlert(ts="2026-07-04T00:00:00+00:00", gateway_ip=GW,
                     trusted_mac=LEGIT, observed_mac=ROGUE)
    d = a.to_dict()
    assert d["kind"] == "gateway_identity"
    assert d["severity"] == "alert"
    assert set(d) == {"ts", "kind", "severity", "gateway_ip",
                      "trusted_mac", "observed_mac"}


# ---- persistence across restarts (inventory feature #7) ----------------------------

def test_binding_store_roundtrip_and_preserves_first_seen():
    d = str(Path(tempfile.mkdtemp()) / "gw.db")
    s = GatewayBindingStore(d)
    assert s.load() == {}
    s.set(GW, LEGIT)
    assert s.load() == {GW: LEGIT}
    row1 = s._conn.execute(
        "SELECT first_seen, last_seen FROM gateway_binding WHERE gateway_ip=?",
        (GW,)).fetchone()
    s.set(GW, ROGUE)                # upsert to a new MAC
    assert s.load() == {GW: ROGUE}
    row2 = s._conn.execute(
        "SELECT first_seen, last_seen FROM gateway_binding WHERE gateway_ip=?",
        (GW,)).fetchone()
    assert row2["first_seen"] == row1["first_seen"]   # first_seen preserved
    s.close()


def test_persisted_baseline_catches_spoof_at_restart():
    d = str(Path(tempfile.mkdtemp()) / "gw.db")
    s1 = GatewayBindingStore(d)
    m1 = GatewayIdentityMonitor(store=s1, debounce_cycles=2)
    m1.observe(GW, LEGIT)          # legit baseline persisted
    s1.close()
    # restart while an attacker is spoofing the gateway MAC right now:
    s2 = GatewayBindingStore(d)
    m2, fired = _monitor(store=s2, debounce_cycles=2)
    assert m2.status()["trusted_bindings"] == {GW: LEGIT}
    m2.observe(GW, ROGUE)
    m2.observe(GW, ROGUE)          # still fires despite the restart
    assert len(fired) == 1
    assert fired[0].trusted_mac == LEGIT
    s2.close()


def test_steady_state_does_not_rewrite_store():
    class CountingStore:
        def __init__(self):
            self.sets = 0
            self.data = {}
        def load(self):
            return dict(self.data)
        def set(self, ip, mac):
            self.sets += 1
            self.data[ip] = mac
    cs = CountingStore()
    m = GatewayIdentityMonitor(store=cs, debounce_cycles=2)
    m.observe(GW, LEGIT)           # first settle -> exactly one write
    for _ in range(5):
        m.observe(GW, LEGIT)       # steady state -> no further writes
    assert cs.sets == 1


def test_store_load_failure_degrades_to_in_memory():
    class BrokenStore:
        def load(self):
            raise RuntimeError("db locked")
        def set(self, ip, mac):
            pass
    m, fired = _monitor(store=BrokenStore(), debounce_cycles=1)
    m.observe(GW, LEGIT)           # baseline (no persisted state loaded)
    m.observe(GW, ROGUE)           # still works in-memory -> fires
    assert len(fired) == 1


# ---- wiring into NetworkInventoryService + /api/alerts ---------------------

async def test_service_feeds_is_gateway_binding_and_fires_on_change():
    from wavr.netinventory_service import NetworkInventoryService
    mon = GatewayIdentityMonitor(debounce_cycles=2)
    state = {"mac": LEGIT}

    async def scan():
        return f"192.168.0.1  {state['mac'].replace(':', '-')}  dynamic\n"

    async def gw():
        return "192.168.0.1"

    svc = NetworkInventoryService(known_macs={LEGIT}, scan=scan, interval=0,
                                  gateway_detect_enabled=True, gateway_detector=gw,
                                  gateway_monitor=mon)
    await svc.scan_once()          # baseline settle
    assert mon.status()["trusted_bindings"] == {"192.168.0.1": LEGIT}
    state["mac"] = ROGUE
    await svc.scan_once()          # anomaly cycle 1
    assert mon.recent_alerts() == []
    await svc.scan_once()          # anomaly cycle 2 -> fires
    alerts = mon.recent_alerts()
    assert len(alerts) == 1
    assert alerts[0].observed_mac == ROGUE
    assert alerts[0].trusted_mac == LEGIT
    assert alerts[0].severity == "alert"


async def test_rogue_device_severity_info_for_randomized_note_for_real():
    from wavr.netinventory_service import NetworkInventoryService

    async def scan():
        # 02:.. has the locally-administered bit set (randomized/guest phone);
        # a4:83:e7:.. is a real globally-unique OUI.
        return ("192.168.0.50  02-11-22-33-44-55  dynamic\n"
                "192.168.0.51  a4-83-e7-11-22-33  dynamic\n")

    svc = NetworkInventoryService(known_macs=set(), scan=scan, interval=0)
    await svc.scan_once()
    by_mac = {a.mac: a for a in svc.recent_alerts()}
    assert by_mac["02:11:22:33:44:55"].severity == "info"
    assert by_mac["a4:83:e7:11:22:33"].severity == "note"
    assert by_mac["02:11:22:33:44:55"].to_dict()["severity"] == "info"


def test_alerts_endpoint_merges_gateway_identity_kind():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from wavr.api_inventory import build_inventory_router
    from wavr.netinventory_service import NetworkInventoryService

    async def scan():
        return ""

    service = NetworkInventoryService(known_macs=set(), scan=scan, interval=0)
    mon = GatewayIdentityMonitor(debounce_cycles=1)
    mon.observe(GW, LEGIT)         # baseline
    mon.observe(GW, ROGUE)         # debounce_cycles=1 -> fires
    assert mon.recent_alerts()

    app = FastAPI()
    app.include_router(build_inventory_router(service, gateway_monitor=mon))
    with TestClient(app) as client:
        body = client.get("/api/alerts").json()
    gw_alerts = [a for a in body["alerts"] if a["kind"] == "gateway_identity"]
    assert len(gw_alerts) == 1
    assert gw_alerts[0]["severity"] == "alert"
    assert gw_alerts[0]["gateway_ip"] == GW
    assert set(gw_alerts[0]) >= {"ts", "kind", "severity", "gateway_ip",
                                 "trusted_mac", "observed_mac"}
