import importlib
import sys

import pytest

from wavr.sources.ble import BLESource

KNOWN = {"aa:bb:cc:dd:ee:ff": "alice"}


async def _first_n(source, n):
    out = []
    agen = source.events()
    try:
        async for ev in agen:
            out.append(ev)
            if len(out) == n:
                break
    finally:
        await agen.aclose()
    return out


async def test_known_address_above_threshold_is_present():
    async def scan():
        return {"aa:bb:cc:dd:ee:ff": -55, "00:00:00:00:00:99": -40}
    src = BLESource(KNOWN, scan=scan, interval=0, rssi_min=-80)
    [ev] = await _first_n(src, 1)
    assert ev.room == "casa"
    assert ev.modality == "ble"
    assert ev.presence is True
    assert ev.confidence == 0.7
    assert ev.motion == 0.0
    assert ev.breathing_bpm is None and ev.heart_bpm is None
    assert ev.ts.endswith("+00:00")


async def test_unknown_address_is_ignored():
    async def scan():
        return {"11:22:33:44:55:66": -30}   # strong signal but not in allowlist
    src = BLESource(KNOWN, scan=scan, interval=0, rssi_min=-80)
    [ev] = await _first_n(src, 1)
    assert ev.presence is False
    assert ev.confidence == 0.0


async def test_known_address_below_threshold_counts_as_absent():
    async def scan():
        return {"aa:bb:cc:dd:ee:ff": -95}   # known device but too far / weak
    src = BLESource(KNOWN, scan=scan, interval=0, rssi_min=-80)
    [ev] = await _first_n(src, 1)
    assert ev.presence is False


async def test_grace_holds_presence_then_absence_after_debounce():
    seq = [
        {"aa:bb:cc:dd:ee:ff": -50},  # seen
        {},                          # miss #1
        {},                          # miss #2
        {},                          # miss #3
    ]
    calls = {"i": 0}
    async def scan():
        i = calls["i"]; calls["i"] += 1
        return seq[min(i, len(seq) - 1)]
    src = BLESource(KNOWN, scan=scan, interval=0, rssi_min=-80, grace=2)
    evs = await _first_n(src, 4)
    # seen -> present; miss#1 grace; miss#2 grace; miss#3 -> absent
    assert [e.presence for e in evs] == [True, True, True, False]


async def test_no_known_devices_skips_scan_and_reports_absent():
    called = {"v": False}
    async def scan():
        called["v"] = True
        return {}
    src = BLESource({}, scan=scan, interval=0)
    [ev] = await _first_n(src, 1)
    assert called["v"] is False
    assert ev.presence is False


async def test_address_normalization_matches_dash_and_uppercase():
    # allowlist stored dash/upper; scan reports colon/lower — both normalize equal
    src = BLESource({"AA-BB-CC-DD-EE-FF": "phone"}, interval=0, rssi_min=-80,
                    scan=lambda: _ret({"aa:bb:cc:dd:ee:ff": -60}))
    [ev] = await _first_n(src, 1)
    assert ev.presence is True


async def _ret(value):
    return value


def test_import_succeeds_without_bleak_installed():
    # bleak must be imported lazily (inside bleak_scan), never at module top,
    # so the module loads in an environment with no bleak installed.
    assert "bleak" not in sys.modules
    mod = importlib.reload(importlib.import_module("wavr.sources.ble"))
    assert hasattr(mod, "BLESource")
    assert "bleak" not in sys.modules   # merely importing the module never pulls bleak


def test_config_exposes_ble_defaults(monkeypatch):
    for v in ("WAVR_BLE_KNOWN", "WAVR_BLE_ROOM", "WAVR_BLE_RSSI_MIN", "WAVR_BLE_INTERVAL"):
        monkeypatch.delenv(v, raising=False)
    from wavr.config import load_config
    cfg = load_config()
    assert cfg.ble_known == {}
    assert cfg.ble_room == "casa"
    assert cfg.ble_rssi_min == -80
    assert cfg.ble_interval == 15.0


def test_config_parses_ble_known_csv(monkeypatch):
    monkeypatch.setenv("WAVR_BLE_KNOWN", "AA-BB-CC-DD-EE-FF=alice, 11:22:33:44:55:66=phone")
    from wavr.config import load_config
    cfg = load_config()
    assert cfg.ble_known == {
        "aa:bb:cc:dd:ee:ff": "alice",
        "11:22:33:44:55:66": "phone",
    }
