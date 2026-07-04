import json

from wavr.data.oui import is_locally_administered, lookup_vendor, oui_prefix
from wavr.netinventory import (
    Device,
    build_inventory,
    guess_device_type,
    parse_arp_inventory,
    scan_inventory,
)
from wavr.rules import RulesEngine

# Raw Windows `arp -a` output with a mix of vendors + a broadcast/multicast row.
WINDOWS_ARP = """
Interface: 192.168.0.10 --- 0x5
  Internet Address      Physical Address      Type
  192.168.0.1           A4-83-E7-11-22-33     dynamic
  192.168.0.23          24-0A-C4-AA-BB-CC     dynamic
  192.168.0.42          DE-AD-BE-EF-00-01     dynamic
  192.168.0.255         FF-FF-FF-FF-FF-FF     static
  224.0.0.22            01-00-5E-00-00-16     static
"""


# ---- OUI -> vendor -----------------------------------------------------------

def test_oui_prefix_normalizes_separators_and_case():
    assert oui_prefix("A4-83-E7-11-22-33") == "a4:83:e7"
    assert oui_prefix("a4:83:e7:11:22:33") == "a4:83:e7"


def test_lookup_vendor_resolves_known_prefixes_offline():
    assert lookup_vendor("a4:83:e7:11:22:33") == "Apple"
    assert lookup_vendor("24-0A-C4-aa-bb-cc") == "Espressif"   # separator/case agnostic
    assert lookup_vendor("b8:27:eb:00:00:01") == "Raspberry Pi"


def test_lookup_vendor_unknown_prefix_returns_unknown():
    assert lookup_vendor("de:ad:be:ef:00:01") == "unknown"


def test_locally_administered_bit_detection():
    assert is_locally_administered("02:00:00:00:00:01") is True   # bit 0x02 set
    assert is_locally_administered("a4:83:e7:11:22:33") is False  # globally unique


# ---- device-type guess (thin wrapper over wavr.recog) -------------------------

def test_guess_device_type_from_vendor():
    # taxonomy values now: mobile-heavy Apple -> phone; Espressif -> esp_dev
    assert guess_device_type("Apple") == "phone"
    assert guess_device_type("Espressif") == "esp_dev"
    assert guess_device_type("Sonos") == "speaker"


def test_guess_device_type_hostname_overrides_vendor():
    # hostname is a stronger signal than the OUI's silicon maker
    assert guess_device_type("Apple", hostname="Johns-iPhone") == "phone"
    assert guess_device_type("unknown", hostname="living-room-tv") == "tv"
    assert guess_device_type("Sonos", hostname="office-printer") == "printer"


def test_guess_device_type_unknown_vendor_defaults_unknown():
    assert guess_device_type("unknown") == "unknown"


def test_guess_device_type_randomized_mac_heuristic():
    # unknown OUI + locally-administered bit -> likely a privacy phone
    assert guess_device_type("unknown", mac="02:11:22:33:44:55") == "phone"


# ---- ARP inventory parsing ---------------------------------------------------

def test_parse_arp_inventory_pairs_ip_and_mac_and_filters_multicast():
    pairs = parse_arp_inventory(WINDOWS_ARP)
    by_mac = dict((m, ip) for ip, m in pairs)
    assert by_mac["a4:83:e7:11:22:33"] == "192.168.0.1"
    assert by_mac["24:0a:c4:aa:bb:cc"] == "192.168.0.23"
    assert by_mac["de:ad:be:ef:00:01"] == "192.168.0.42"
    # broadcast + IPv4 multicast rows dropped
    assert "ff:ff:ff:ff:ff:ff" not in by_mac
    assert "01:00:5e:00:00:16" not in by_mac
    assert len(pairs) == 3


def test_parse_arp_inventory_dedupes_by_mac_first_ip_wins():
    text = "10.0.0.5 a4-83-e7-11-22-33 dynamic\n10.0.0.9 a4-83-e7-11-22-33 dynamic\n"
    assert parse_arp_inventory(text) == [("10.0.0.5", "a4:83:e7:11:22:33")]


# ---- inventory shape ---------------------------------------------------------

def test_build_inventory_shape_and_fields():
    entries = parse_arp_inventory(WINDOWS_ARP)
    known = {"a4:83:e7:11:22:33"}
    inv = build_inventory(entries, known_macs=known,
                          hostnames={"24:0a:c4:aa:bb:cc": "esp32-sensor"})
    assert all(isinstance(d, Device) for d in inv)
    assert set(inv[0].to_dict().keys()) == {
        "mac", "ip", "vendor", "device_type", "known", "hostname", "risks",
        "type_confidence", "make", "model", "os", "open_ports", "sources",
    }
    assert inv[0].to_dict()["risks"] == []      # empty until the opt-in port pass runs
    assert inv[0].to_dict()["open_ports"] == []
    by_mac = {d.mac: d for d in inv}
    apple = by_mac["a4:83:e7:11:22:33"]
    assert apple.vendor == "Apple" and apple.ip == "192.168.0.1"
    assert apple.device_type == "phone" and apple.known is True
    assert apple.type_confidence == "low"       # mobile-heavy fallback is honest-low
    assert apple.make == "Apple"                # OUI vendor doubles as the make guess
    esp = by_mac["24:0a:c4:aa:bb:cc"]
    assert esp.vendor == "Espressif" and esp.known is False
    assert esp.device_type == "esp_dev"         # esp32-* hostname pattern
    assert esp.type_confidence == "high"        # hostname + vendor default agree
    assert any(s["signal"] == "hostname" for s in esp.sources)
    rogue = by_mac["de:ad:be:ef:00:01"]
    assert rogue.vendor == "unknown" and rogue.known is False
    # 0xde has the locally-administered bit set -> randomized-MAC heuristic
    assert rogue.device_type == "phone" and rogue.type_confidence == "low"
    assert rogue.make is None


def test_build_inventory_user_pin_wins_recognition():
    entries = parse_arp_inventory(WINDOWS_ARP)
    inv = build_inventory(entries, pins={"de:ad:be:ef:00:01": "camera"})
    pinned = next(d for d in inv if d.mac == "de:ad:be:ef:00:01")
    assert pinned.device_type == "camera"
    assert pinned.type_confidence == "high"
    assert pinned.sources[0]["signal"] == "user_pin"


async def test_scan_inventory_uses_injected_transport_no_network():
    async def fake_scan():
        return WINDOWS_ARP
    inv = await scan_inventory(known_macs={"a4:83:e7:11:22:33"}, scan=fake_scan)
    assert {d.mac for d in inv} == {
        "a4:83:e7:11:22:33", "24:0a:c4:aa:bb:cc", "de:ad:be:ef:00:01",
    }
    assert next(d for d in inv if d.mac == "a4:83:e7:11:22:33").known is True


# ---- rogue-device alerting (RulesEngine) ------------------------------------

def _engine():
    msgs = []
    eng = RulesEngine(lambda t, p, r: msgs.append((t, p, r)),
                      known_macs={"a4:83:e7:11:22:33"})
    return eng, msgs


def test_unknown_mac_triggers_exactly_one_alert_even_on_rescan():
    eng, msgs = _engine()
    rogue = {"mac": "de:ad:be:ef:00:01", "ip": "192.168.0.42",
             "vendor": "unknown", "device_type": "unknown", "known": False}
    eng.handle_devices([rogue])
    eng.handle_devices([rogue])   # a second scan must NOT re-alert
    alerts = [m for m in msgs if m[0] == "wavr/security/rogue"]
    assert len(alerts) == 1
    topic, payload, retain = alerts[0]
    assert retain is False                                   # edge event
    body = json.loads(payload)
    assert body["mac"] == "de:ad:be:ef:00:01"
    assert body["ip"] == "192.168.0.42"


def test_known_and_allowlisted_macs_never_alert():
    eng, msgs = _engine()
    # on the engine allowlist
    eng.handle_devices([{"mac": "a4:83:e7:11:22:33", "known": False}])
    # flagged known by the inventory itself
    eng.handle_devices([{"mac": "11:22:33:44:55:66", "known": True}])
    assert [m for m in msgs if m[0] == "wavr/security/rogue"] == []


def test_handle_devices_accepts_device_objects_from_inventory():
    eng, msgs = _engine()
    inv = build_inventory(parse_arp_inventory(WINDOWS_ARP),
                          known_macs={"a4:83:e7:11:22:33"})
    eng.handle_devices(inv)
    alerts = [json.loads(p)["mac"] for t, p, _ in msgs if t == "wavr/security/rogue"]
    # Apple is allowlisted -> only the two unknown/unlisted hosts alert
    assert set(alerts) == {"24:0a:c4:aa:bb:cc", "de:ad:be:ef:00:01"}
