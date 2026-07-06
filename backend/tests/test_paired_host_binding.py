"""Adversarial tests for the LIVE IP-correlation "IDENTIFIED on its network host"
overlay (blueprint item 4, privacy centerpiece).

A paired + GREEN-consented device that is actively POSTing telemetry is NAMED on its
LAN host -- but ONLY while the correlation is simultaneously FRESH, still-GREEN
(re-checked at read time), UNAMBIGUOUS (one green device per IP) and MAC-CONSISTENT
(the IP's current MAC == the MAC captured at record time). These tests are the GATE for
the 7 threat-model MUST-constraints: nothing persists a device<->MAC/name map, the
source_ip never lands on a TelemetryReading / SensingEvent / log, a silently-withdrawn
(red, no more POSTs) device is un-named at view time, a DHCP-reassigned IP is never
misnamed, and rogue-suppression is scoped to the exact bound MAC.

Injected clocks only -- no real sleeps. Integration tests forge an in-subnet LAN peer
(TestClient with a client=(host, port) tuple + `_local_ipv4` monkeypatched), exactly
like test_telemetry_sensor.py / test_consent_tier.py, so the REAL middleware + routes +
binder run. Binder-level tests drive PairedHostBinder directly with fixed datetimes.
"""
import inspect
import logging
import sqlite3
from dataclasses import fields
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import wavr.host_binding as host_binding_mod
import wavr.netinventory as netinv_mod
from wavr.app import create_app
from wavr.camera_store import CameraStore
from wavr.events import SensingEvent
from wavr.fusion import _DEFAULT_FRESHNESS_S
from wavr.host_binding import PairedHostBinder
from wavr.netinventory import Device, apply_recognition, guess_device_type
from wavr.netinventory_service import NetworkInventoryService
from wavr.recog import recognize, _WEIGHTS
from wavr.sources.simulated import SimulatedSource
from wavr.storage import Storage
from wavr.telemetry import TelemetryReading

CSRF = {"X-Wavr-Local": "1"}            # loopback root's CSRF header
_FRESHNESS = _DEFAULT_FRESHNESS_S       # 30s -- the SAME window fusion prunes against
_BASE = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)
_PEER_IP = "192.168.1.50"
_PEER_MAC = "00:11:22:33:44:55"         # universally-administered (bit 0x02 clear)
_ATTACKER_MAC = "66:77:88:99:aa:bb"


# --------------------------------------------------------------------------------------
# Integration harness: a real multidevice app whose LAN inventory is a seeded fake so the
# scan loop never has to run. `net_inventory` is create_app's documented test seam.
# --------------------------------------------------------------------------------------
class _FakeInv:
    """Minimal inventory seam. Holds a mutable device list the test seeds; used by the
    telemetry record path (_mac_of_ip), the view-time resolver and the rogue predicate."""

    def __init__(self, devices=None):
        self.devices = list(devices or [])

    def latest_inventory(self):
        return list(self.devices)

    def recent_alerts(self, limit=50):
        return []


def _dev(ip, mac, vendor="unknown", device_type="unknown", known=False):
    return Device(mac=mac, ip=ip, vendor=vendor, device_type=device_type, known=known)


def _make_app(tmp_path, monkeypatch, inv):
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "md.db"))
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    app = create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"),
        net_inventory=inv)
    return app


def _pair(app, role="sensor"):
    """Central (loopback root) mints a code; a forged in-subnet LAN peer at _PEER_IP
    redeems it. Returns (peer_client, auth_headers, device_id). Peer defaults to green."""
    central = TestClient(app)
    code = central.post("/api/pair-code", json={"role": role}, headers=CSRF).json()["code"]
    peer = TestClient(app, client=(_PEER_IP, 12345))
    body = peer.post("/api/pair", json={"code": code, "device_name": "augusto-phone"}).json()
    return peer, {"Authorization": f"Bearer {body['token']}"}, body["device_id"]


def _payload(device="phone"):
    return {"device": device, "battery_pct": 70, "charging": "yes", "rssi": -50,
            "ssid": "home", "bssid": "aa:bb:cc:dd:ee:ff"}


def _view_for(app, ip):
    """GET /api/inventory (loopback root) and return the device view dict for `ip`."""
    central = TestClient(app)
    devices = central.get("/api/inventory", headers=CSRF).json()["devices"]
    return next((d for d in devices if d["ip"] == ip), None)


# --------------------------------------------------------------------------------------
# 1. GREEN device is NAMED on its host: label + paired source + device_type "phone".
# --------------------------------------------------------------------------------------
def test_green_device_named_on_its_host(tmp_path, monkeypatch):
    inv = _FakeInv([_dev(_PEER_IP, _PEER_MAC)])
    app = _make_app(tmp_path, monkeypatch, inv)
    peer, auth, device_id = _pair(app)

    assert peer.post("/api/telemetry", json=_payload(), headers=auth).json()["accepted"] is True

    view = _view_for(app, _PEER_IP)
    assert view["label"] == "augusto-phone"
    assert view["paired"] is True
    assert view["device_type"] == "phone"          # set from paired (no user pin)
    assert view["type_confidence"] == "high"
    assert view["sources"][0]["signal"] == "paired"
    assert _PEER_IP in view["sources"][0]["value"]  # "live-correlated from <ip>"


# --------------------------------------------------------------------------------------
# 2. YELLOW is anonymous: a previously-green NAMED host reverts to normal recog.
# --------------------------------------------------------------------------------------
def test_yellow_is_anonymous_and_reverts(tmp_path, monkeypatch):
    inv = _FakeInv([_dev(_PEER_IP, _PEER_MAC)])
    app = _make_app(tmp_path, monkeypatch, inv)
    peer, auth, _ = _pair(app)

    peer.post("/api/telemetry", json=_payload(), headers=auth)     # green -> named
    assert _view_for(app, _PEER_IP)["label"] == "augusto-phone"

    peer.post("/api/consent", json={"level": "yellow"}, headers=auth)
    assert peer.post("/api/telemetry", json=_payload(), headers=auth).json()["accepted"] is True

    view = _view_for(app, _PEER_IP)
    assert "label" not in view and "paired" not in view          # anonymous again
    assert view["device_type"] == "unknown"                      # reverted to normal recog


# --------------------------------------------------------------------------------------
# 3. RED is unlinked: dropped server-side, never named.
# --------------------------------------------------------------------------------------
def test_red_is_unlinked(tmp_path, monkeypatch):
    inv = _FakeInv([_dev(_PEER_IP, _PEER_MAC)])
    app = _make_app(tmp_path, monkeypatch, inv)
    peer, auth, _ = _pair(app)

    peer.post("/api/telemetry", json=_payload(), headers=auth)   # green -> named
    assert _view_for(app, _PEER_IP)["label"] == "augusto-phone"

    peer.post("/api/consent", json={"level": "red"}, headers=auth)
    body = peer.post("/api/telemetry", json=_payload(), headers=auth).json()
    assert body["accepted"] is False                             # dropped server-side

    view = _view_for(app, _PEER_IP)
    assert "label" not in view and "paired" not in view          # never named


def test_silent_red_withdrawal_un_names_at_view(tmp_path, monkeypatch):
    """Constraint 2 (the GDPR-red backstop): a device posts green (bound + named), then
    withdraws to red via /api/consent and NEVER posts again. /api/consent does NOT drop the
    binder, so the green-recorded entry LINGERS -- yet the next view re-checks consent inside
    resolve() and strips the name. Withdrawal must not wait for a later POST."""
    inv = _FakeInv([_dev(_PEER_IP, _PEER_MAC)])
    app = _make_app(tmp_path, monkeypatch, inv)
    peer, auth, device_id = _pair(app)

    peer.post("/api/telemetry", json=_payload(), headers=auth)   # green -> bound + named
    assert _view_for(app, _PEER_IP)["label"] == "augusto-phone"

    peer.post("/api/consent", json={"level": "red"}, headers=auth)   # withdraw, NO further POST
    assert app.state.host_binder._entries.get(device_id) is not None  # entry NOT dropped by consent
    view = _view_for(app, _PEER_IP)
    assert "label" not in view and "paired" not in view          # view-time re-check un-names


# --------------------------------------------------------------------------------------
# 4. DHCP MOVE re-correlates: bound at Y, next post from X -> named at X, NOT at Y.
# --------------------------------------------------------------------------------------
def test_dhcp_move_recorrelates_and_frees_old_ip():
    binder = PairedHostBinder(get_label=lambda d: "Phone", get_consent=lambda d: "green")
    y, x = "192.168.1.20", "192.168.1.77"
    binder.record("dev1", y, _PEER_MAC, _BASE)
    binder.record("dev1", x, _PEER_MAC, _BASE + timedelta(seconds=1))   # moved to X

    mac_of_ip = {y: _PEER_MAC, x: _PEER_MAC}
    got = binder.resolve(_BASE + timedelta(seconds=1), lambda ip: mac_of_ip.get(ip))
    assert got == {x: "Phone"}          # named at X only; old Y entry was overwritten/freed


# --------------------------------------------------------------------------------------
# 5. DHCP-REASSIGN guard: device stops at Y; a DIFFERENT MAC now answers at Y within
#    freshness -> the name is NOT applied (MAC-consistency).
# --------------------------------------------------------------------------------------
def test_dhcp_reassign_withholds_name_on_mac_mismatch():
    binder = PairedHostBinder(get_label=lambda d: "Phone", get_consent=lambda d: "green")
    y = "192.168.1.20"
    binder.record("dev1", y, _PEER_MAC, _BASE)                  # phone had macA at Y

    # Still within freshness, but a DIFFERENT device (macB) now answers at Y.
    got = binder.resolve(_BASE + timedelta(seconds=5), lambda ip: {y: _ATTACKER_MAC}.get(ip))
    assert got == {}                                           # name withheld


# --------------------------------------------------------------------------------------
# 6. STALE / STOP un-names after the freshness window (pruned in RAM, no new timer).
# --------------------------------------------------------------------------------------
def test_stale_binding_un_names_after_freshness():
    binder = PairedHostBinder(get_label=lambda d: "Phone", get_consent=lambda d: "green")
    binder.record("dev1", _PEER_IP, _PEER_MAC, _BASE)
    mac_of_ip = {_PEER_IP: _PEER_MAC}

    fresh = binder.resolve(_BASE + timedelta(seconds=_FRESHNESS), lambda ip: mac_of_ip.get(ip))
    assert fresh == {_PEER_IP: "Phone"}                        # still fresh at the boundary

    stale = binder.resolve(_BASE + timedelta(seconds=_FRESHNESS + 1), lambda ip: mac_of_ip.get(ip))
    assert stale == {}                                         # aged out -> un-named
    assert binder._entries == {}                              # pruned from RAM entirely


# --------------------------------------------------------------------------------------
# 7. RED-RACE: a binding recorded microseconds before a red flip -> the view-time GREEN
#    re-check strips it (withdrawal must NOT wait for a later POST).
# --------------------------------------------------------------------------------------
def test_red_race_view_time_recheck_strips_binding():
    binder = PairedHostBinder(get_label=lambda d: "Phone", get_consent=lambda d: "green")
    binder.record("dev1", _PEER_IP, _PEER_MAC, _BASE)          # recorded while green
    mac_of_ip = {_PEER_IP: _PEER_MAC}

    # The device flipped to red between record and read; the entry still sits in RAM.
    now_red = binder.resolve(_BASE, lambda ip: mac_of_ip.get(ip), is_green=lambda d: False)
    assert now_red == {}                                       # green re-check fails closed

    # Unknown/None tier is treated the same (fail-closed), never named.
    now_unknown = binder.resolve(_BASE, lambda ip: mac_of_ip.get(ip), is_green=lambda d: None)
    assert now_unknown == {}


# --------------------------------------------------------------------------------------
# 8. AMBIGUOUS IP: two green devices claim one IP -> NO name (conflict surfaced as absence).
# --------------------------------------------------------------------------------------
def test_ambiguous_ip_emits_no_binding():
    binder = PairedHostBinder(
        get_label=lambda d: {"devA": "A", "devB": "B"}.get(d), get_consent=lambda d: "green")
    binder.record("devA", _PEER_IP, _PEER_MAC, _BASE)
    binder.record("devB", _PEER_IP, _PEER_MAC, _BASE)          # same IP, two green devices

    got = binder.resolve(_BASE, lambda ip: {_PEER_IP: _PEER_MAC}.get(ip))
    assert got == {}                                           # collision -> no binding


# --------------------------------------------------------------------------------------
# 9. ANTI-SPOOF: a token binds only its OWN device_id to its OWN source_ip; payload.device
#    is ignored for identity (a phone can never bind another device's IP).
# --------------------------------------------------------------------------------------
def test_anti_spoof_binds_own_device_id_only(tmp_path, monkeypatch):
    inv = _FakeInv([_dev(_PEER_IP, _PEER_MAC)])
    app = _make_app(tmp_path, monkeypatch, inv)
    peer, auth, device_id = _pair(app)

    # payload.device claims to be a victim; the binder must key by the TOKEN's device_id.
    peer.post("/api/telemetry", json=_payload(device="victim-device-id"), headers=auth)

    entries = app.state.host_binder._entries
    assert device_id in entries                                # keyed by the caller's own id
    assert "victim-device-id" not in entries                  # the wire `device` is ignored
    ip, mac, _ = entries[device_id]
    assert ip == _PEER_IP and mac == _PEER_MAC                 # its OWN source_ip + live MAC


# --------------------------------------------------------------------------------------
# 10. RECOG PRECEDENCE (unit): paired sets label + is high, but user_pin STILL wins
#     device_type (the LABEL authority is not the device_type authority).
# --------------------------------------------------------------------------------------
def test_recog_paired_sets_label_but_user_pin_wins_type():
    # paired alone -> phone + label + its own "paired" evidence family, weight below user_pin.
    alone = recognize({"paired": {"label": "Augusto phone", "device_type": "phone"}})
    assert alone.device_type == "phone" and alone.label == "Augusto phone"
    assert _WEIGHTS["paired"] < _WEIGHTS["user_pin"]

    # paired + user_pin -> the pin wins the TYPE, the paired label still wins the NAME.
    both = recognize({"paired": {"label": "Augusto phone"}, "user_pin": "laptop"})
    assert both.device_type == "laptop"                        # user pin is never overridden
    assert both.label == "Augusto phone"                       # label authority is the paired one
    assert any(s["signal"] == "paired" for s in both.sources)


def test_scan_path_never_feeds_recog_a_paired_signal(monkeypatch):
    """Guard (item-4 reviewers): the `paired` recog branch is INERT server-side.
    The ONLY producer of a `paired` signal is the consent-gated view-time resolver
    (wavr.api_inventory via PairedHostBinder). The scan/inventory recognition path
    (guess_device_type / apply_recognition) must NEVER inject a `paired` key into
    recog's signals dict -- so no client- or scan-controlled `paired` reaches recog
    server-side. TEST-ONLY: recog.py behavior is unchanged; this pins the invariant."""
    captured: list[dict] = []

    real_recognize = netinv_mod.recognize

    def _spy(signals):
        captured.append(dict(signals))
        return real_recognize(signals)

    # Patch the name `apply_recognition`/`guess_device_type` actually call (both use
    # the module-level `recognize` imported into wavr.netinventory).
    monkeypatch.setattr(netinv_mod, "recognize", _spy)

    dev = Device(mac="aa:bb:cc:dd:ee:ff", ip="192.168.1.50", vendor="Apple",
                 device_type="unknown", known=True, hostname="augusto-iphone",
                 open_ports=(80,))
    # Exercise BOTH scan-path entry points, including every optional collector kwarg
    # apply_recognition accepts -- none is `paired`, and none may smuggle one in.
    guess_device_type("Apple", "augusto-iphone", "aa:bb:cc:dd:ee:ff")
    apply_recognition(dev, pin="phone",
                      bonjour={"device_type": "phone"}, upnp={"device_type": "phone"},
                      snmp={"device_type": "router"}, netbios={"device_type": "pc"},
                      dhcp={"device_type": "phone"}, ha={"device_type": "phone"})

    assert captured, "spy must have observed at least one recognize() call"
    for signals in captured:
        assert "paired" not in signals, (
            "the scan/inventory path must NEVER pass a `paired` signal into recog -- "
            "the only legitimate producer is the consent-gated api_inventory view overlay")


def test_user_type_pin_still_wins_device_type_at_view(tmp_path, monkeypatch):
    """The api_inventory view-level mirror of the precedence: a paired GREEN host gets the
    label, but an explicit owner type-pin keeps the device_type."""
    inv = _FakeInv([_dev(_PEER_IP, _PEER_MAC)])
    app = _make_app(tmp_path, monkeypatch, inv)
    peer, auth, _ = _pair(app)
    central = TestClient(app)
    central.put("/api/inventory/type", json={"mac": _PEER_MAC, "device_type": "laptop"},
                headers=CSRF)

    peer.post("/api/telemetry", json=_payload(), headers=auth)
    view = _view_for(app, _PEER_IP)
    assert view["label"] == "augusto-phone" and view["paired"] is True   # label overlay applied
    assert view["device_type"] == "laptop"                               # pin NOT overridden


# --------------------------------------------------------------------------------------
# 11. NOT-PERSISTED + source_ip on NO TelemetryReading / SensingEvent / log.
# --------------------------------------------------------------------------------------
def test_source_ip_never_on_reading_or_event():
    reading_fields = {f.name for f in fields(TelemetryReading)}
    event_fields = {f.name for f in fields(SensingEvent)}
    for leak in ("ip", "source_ip", "client_ip", "peer"):
        assert leak not in reading_fields, f"{leak} must not be a TelemetryReading field"
        assert leak not in event_fields, f"{leak} must not be a SensingEvent field"


def test_binder_is_never_persisted():
    # In-memory only: no sqlite import/use, no file writes, no device_meta.set_name.
    src = inspect.getsource(host_binding_mod)
    assert "import sqlite3" not in src
    assert ".execute(" not in src and ".connect(" not in src
    assert "set_name" not in src
    assert "open(" not in src
    # A fresh binder (a "restart") shares NO state with a previously-recorded one.
    b1 = PairedHostBinder(get_consent=lambda d: "green")
    b1.record("dev1", _PEER_IP, _PEER_MAC, _BASE)
    assert PairedHostBinder()._entries == {}
    assert not hasattr(b1, "_conn")


def test_no_device_mac_or_source_ip_column_persisted(tmp_path, monkeypatch):
    inv = _FakeInv([_dev(_PEER_IP, _PEER_MAC)])
    db_path = str(tmp_path / "md.db")
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", db_path)
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    app = create_app(sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
                     storage=Storage(":memory:"), camera_store=CameraStore(":memory:"),
                     net_inventory=inv)
    peer, auth, _ = _pair(app)
    peer.post("/api/telemetry", json=_payload(), headers=auth)   # records a binding

    # The devices table must NOT have grown any binding/MAC/source_ip column, and no table
    # anywhere may carry a source_ip column (constraint 4: no durable device<->MAC/operator).
    conn = sqlite3.connect(db_path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for table in tables:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            assert "source_ip" not in cols
            if table == "devices":
                assert "mac" not in cols and "ip" not in cols
    finally:
        conn.close()


def test_source_ip_is_never_logged(tmp_path, monkeypatch, caplog):
    inv = _FakeInv([_dev(_PEER_IP, _PEER_MAC)])
    app = _make_app(tmp_path, monkeypatch, inv)
    peer, auth, _ = _pair(app)
    with caplog.at_level(logging.DEBUG):
        peer.post("/api/telemetry", json=_payload(), headers=auth)
    assert _PEER_IP not in caplog.text                          # the source_ip is never logged


# --------------------------------------------------------------------------------------
# 12. ROGUE-SUPPRESSION scoped to the exact bound green host; an UNPAIRED attacker at a
#     different MAC still alerts.
# --------------------------------------------------------------------------------------
async def test_rogue_suppression_scoped_to_bound_mac():
    binder = PairedHostBinder(get_label=lambda d: "Augusto", get_consent=lambda d: "green")
    binder.record("dev1", _PEER_IP, _PEER_MAC, _BASE)

    # Two unknown hosts this scan: the bound green phone (.50) and an attacker (.99).
    arp = f"{_PEER_IP}   00-11-22-33-44-55\n192.168.1.99   66-77-88-99-aa-bb\n"

    async def fake_scan():
        return arp

    def is_bound(ip):
        mac_of_ip = {_PEER_IP: _PEER_MAC, "192.168.1.99": _ATTACKER_MAC}
        return ip in binder.resolve(_BASE, lambda i: mac_of_ip.get(i))

    svc = NetworkInventoryService(known_macs=[], scan=fake_scan, interval=0, is_bound=is_bound)
    await svc.scan_once()

    alerted = {a.mac for a in svc.recent_alerts()}
    assert _PEER_MAC not in alerted            # bound green host: rogue alert suppressed
    assert _ATTACKER_MAC in alerted            # unpaired attacker at a different MAC: still alerts


async def test_rogue_suppression_lapses_when_binding_evaporates():
    """The bound host is NOT added to the edge-trigger dedup set, so once its binding lapses
    (consent withdrawn / device gone) it alerts on the next scan -- suppression is transient,
    never a permanent allowlist."""
    tier = {"dev1": "green"}
    binder = PairedHostBinder(get_label=lambda d: "Augusto",
                              get_consent=lambda d: tier.get(d))
    binder.record("dev1", _PEER_IP, _PEER_MAC, _BASE)

    async def fake_scan():
        return f"{_PEER_IP}   00-11-22-33-44-55\n"

    def is_bound(ip):
        return ip in binder.resolve(_BASE, lambda i: {_PEER_IP: _PEER_MAC}.get(i))

    svc = NetworkInventoryService(known_macs=[], scan=fake_scan, interval=0, is_bound=is_bound)
    await svc.scan_once()
    assert not svc.recent_alerts()             # suppressed while green + bound

    tier["dev1"] = "red"                        # silent withdrawal -> binding no longer resolves
    await svc.scan_once()
    assert {a.mac for a in svc.recent_alerts()} == {_PEER_MAC}   # now alerts (not permanently allowlisted)
