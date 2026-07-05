"""wavr.recog -- precedence fusion, consensus bump, evidence trail."""
from wavr.data.deviceclass import DEVICE_TYPES
from wavr.netinventory import guess_device_type
from wavr.recog import DeviceIdentity, recognize


# ---- empty / minimal ----------------------------------------------------------

def test_no_signals_is_honest_unknown():
    ident = recognize({})
    assert ident.device_type == "unknown"
    assert ident.confidence == "low"
    assert ident.sources == ()
    assert ident.make is None and ident.model is None and ident.os is None


def test_single_oui_signal():
    ident = recognize({"vendor": "Sonos"})
    assert ident.device_type == "speaker"
    # M1 audit fix: OUI-alone is capped at "medium" even though the vendor table
    # says "high" -- OUI is the first 3 MAC octets, freely spoofable by any LAN
    # device, so a lone OUI match must never claim "high" by itself.
    assert ident.confidence == "medium"
    assert ident.make == "Sonos"
    assert [s["signal"] for s in ident.sources] == ["oui"]


# ---- precedence order (each rung beats the one below it) -----------------------

def test_user_pin_beats_everything():
    ident = recognize({
        "user_pin": "camera",
        "upnp": {"device_type": "tv", "make": "Sony"},
        "hostname": "office-printer",
        "vendor": "Sonos",
    })
    assert ident.device_type == "camera"
    assert ident.confidence == "high"
    assert ident.sources[0]["signal"] == "user_pin"
    assert ident.sources[0]["weight"] == 1.0


def test_self_description_beats_hostname():
    ident = recognize({
        "upnp": {"device_type": "tv", "make": "Sony", "model": "KD-55X80J"},
        "hostname": "office-printer",
        "vendor": "unknown",
    })
    assert ident.device_type == "tv"
    assert ident.make == "Sony" and ident.model == "KD-55X80J"


def test_hostname_beats_port_hint():
    ident = recognize({"hostname": "bedroom-tv", "open_ports": [9100]})
    assert ident.device_type == "tv"


def test_port_hint_beats_oui_default():
    ident = recognize({"vendor": "TP-Link", "open_ports": [554]})
    assert ident.device_type == "camera"


def test_oui_default_beats_mobile_fallback_by_construction():
    # a vendor in VENDOR_DEFAULT_TYPE never reaches the mobile fallback
    ident = recognize({"vendor": "Espressif"})
    assert ident.device_type == "esp_dev"


def test_invalid_pin_is_ignored_not_crashed():
    ident = recognize({"user_pin": "spaceship", "vendor": "Sonos"})
    assert ident.device_type == "speaker"
    assert all(s["signal"] != "user_pin" for s in ident.sources)


# ---- consensus bump (sensor-consensus ethos) -----------------------------------

def test_two_agreeing_signals_bump_confidence():
    # Wyze OUI says camera (medium); open RTSP port agrees (medium) -> high
    ident = recognize({"vendor": "Wyze", "open_ports": [554]})
    assert ident.device_type == "camera"
    assert ident.confidence == "high"
    assert {s["signal"] for s in ident.sources} == {"oui", "port_hint"}


def test_single_signal_never_bumps():
    ident = recognize({"vendor": "Wyze"})
    assert ident.confidence == "medium"


def test_eufy_oui_resolves_to_camera_via_fusion():
    # unifi.md #2: Eufy/Anker was entirely missing from the OUI table -- an
    # Eufy MAC used to fall through to "unknown". Same shape as the existing
    # Wyze/Ring camera-vendor entries: OUI alone -> medium confidence.
    ident = recognize({"vendor": "Eufy"})
    assert ident.device_type == "camera"
    assert ident.confidence == "medium"


def test_disagreeing_signal_does_not_bump():
    # printer port vs router OUI: winner is the port hint, no agreement
    ident = recognize({"vendor": "Netgear", "open_ports": [9100]})
    assert ident.device_type == "printer"
    assert ident.confidence == "medium"


# ---- M1 fix: OUI-alone confidence cap (spoofable, security-relevant) -----------

def test_oui_alone_capped_at_medium_even_when_table_says_high():
    # Hikvision's own VENDOR_DEFAULT_TYPE entry is ("camera", "high"), but a rogue
    # device can freely self-select ANY OUI prefix (it's just the first 3 MAC
    # octets) -- so that "high" must never surface from OUI alone.
    ident = recognize({"vendor": "Hikvision"})
    assert ident.device_type == "camera"
    assert ident.confidence == "medium"
    assert [s["signal"] for s in ident.sources] == ["oui"]


def test_oui_plus_second_signal_still_reaches_high():
    # A second, independent signal agreeing with the OUI-implied type earns "high"
    # via the normal consensus bump -- only the OUI-ALONE case is capped.
    ident = recognize({"vendor": "Hikvision", "open_ports": [554]})
    assert ident.device_type == "camera"
    assert ident.confidence == "high"
    assert {s["signal"] for s in ident.sources} == {"oui", "port_hint"}


def test_oui_alone_low_or_medium_table_confidence_is_unaffected():
    # the cap only ever lowers "high" -> "medium"; it must not raise a genuinely
    # low-confidence vendor guess.
    assert recognize({"vendor": "Raspberry Pi"}).confidence == "low"
    assert recognize({"vendor": "Wyze"}).confidence == "medium"


# ---- collector threat model: self-description ALONE caps at medium ------------

def test_bonjour_alone_capped_at_medium():
    # mDNS/SSDP/SNMP are self-broadcast on the open LAN multicast group -- any
    # host can announce anything, so ONE self-description signal must never
    # claim "high" alone (same rationale as the M1 OUI-alone cap).
    ident = recognize({"bonjour": {"device_type": "camera", "make": "Wyze"}})
    assert ident.device_type == "camera"
    assert ident.confidence == "medium"
    assert [s["signal"] for s in ident.sources] == ["bonjour"]


def test_upnp_alone_capped_at_medium():
    ident = recognize({"upnp": {"device_type": "tv", "make": "Sony"}})
    assert ident.confidence == "medium"


def test_snmp_alone_capped_at_medium():
    ident = recognize({"snmp": {"device_type": "router"}})
    assert ident.confidence == "medium"


def test_netbios_alone_capped_at_medium():
    # NetBIOS is the strongest Windows-PC signal but is still a device
    # SELF-description (NBSTAT), same spoofability threat model as
    # mDNS/SSDP/SNMP -- capped at "medium" alone (collectors-lote2).
    ident = recognize({"netbios": {"device_type": "desktop"}})
    assert ident.device_type == "desktop"
    assert ident.confidence == "medium"
    assert [s["signal"] for s in ident.sources] == ["netbios"]


def test_netbios_beats_dhcp_but_loses_to_snmp():
    # Precedence order: snmp > netbios > dhcp (collectors-lote2).
    ident = recognize({"netbios": {"device_type": "desktop"}, "dhcp": {"device_type": "router"}})
    assert ident.device_type == "desktop"
    ident2 = recognize({"snmp": {"device_type": "router"}, "netbios": {"device_type": "desktop"}})
    assert ident2.device_type == "router"


def test_self_description_plus_second_signal_reaches_high():
    # A 2nd independent signal agreeing on the same type restores "high" via
    # the normal consensus bump -- only the ALONE case is capped.
    ident = recognize({"bonjour": {"device_type": "camera"}, "open_ports": [554]})
    assert ident.device_type == "camera"
    assert ident.confidence == "high"
    assert {s["signal"] for s in ident.sources} == {"bonjour", "port_hint"}


# ---- audit fix #2: consensus bump requires INDEPENDENT families, not names -----

def test_two_self_reports_agreeing_stay_medium_not_forged_high():
    # snmp + netbios are BOTH the `self_report` family -- a single rogue LAN
    # host can answer both simultaneously (all attacker-set), so two
    # self-descriptions agreeing must NOT forge "high" on their own (the
    # exact defect the family-gated consensus bump closes).
    ident = recognize({"snmp": {"device_type": "router"}, "netbios": {"device_type": "router"}})
    assert ident.device_type == "router"
    assert ident.confidence == "medium"


def test_upnp_bonjour_agreeing_stay_medium_not_forged_high():
    ident = recognize({"upnp": {"device_type": "camera"}, "bonjour": {"device_type": "camera"}})
    assert ident.device_type == "camera"
    assert ident.confidence == "medium"


def test_oui_plus_port_hint_still_reaches_high_cross_family():
    # oui + port_hint span 2 DISTINCT families (`oui` + `observed`) -> still
    # earns "high" -- the family gate only blocks same-family agreement.
    ident = recognize({"vendor": "Wyze", "open_ports": [554]})
    assert ident.device_type == "camera"
    assert ident.confidence == "high"


# ---- audit fix #3: dhcp-fp is chaddr-keyed (unauthenticated) -> lower trust ---

def test_dhcp_alone_is_unverified_and_weighted_below_oui():
    ident = recognize({"dhcp": {"device_type": "router"}})
    assert ident.device_type == "router"
    assert ident.confidence == "medium"
    assert ident.sources[0]["signal"] == "dhcp"
    assert ident.sources[0]["unverified"] is True
    assert ident.sources[0]["weight"] < 0.4   # below oui's 0.4


def test_dhcp_loses_to_oui_when_they_disagree():
    # Down-weighted below oui (audit fix #3): an off-path chaddr-spoofed dhcp
    # signal must not outrank even a lone (spoofable-but-ARP-observed) OUI
    # guess on a type disagreement.
    ident = recognize({"vendor": "Sonos", "dhcp": {"device_type": "router"}})
    assert ident.device_type == "speaker"


def test_only_non_dhcp_sources_carry_no_unverified_key():
    # Backward-compatible shape: a source NOT flagged unverified omits the
    # key entirely (rather than carrying `"unverified": False`).
    ident = recognize({"vendor": "Sonos"})
    assert "unverified" not in ident.sources[0]


# ---- make / model / os --------------------------------------------------------

def test_make_falls_back_to_known_vendor():
    assert recognize({"vendor": "Apple"}).make == "Apple"
    assert recognize({"vendor": "unknown"}).make is None


def test_os_comes_from_dhcp_hook():
    ident = recognize({"vendor": "Dell", "dhcp": {"os": "Windows"}})
    assert ident.os == "Windows"


def test_self_description_make_beats_vendor_fallback():
    ident = recognize({"vendor": "TP-Link", "bonjour": {"make": "Tapo"}})
    assert ident.make == "Tapo"


def test_self_described_strings_are_length_bounded():
    # defensive bound (M1 fix) on make/model/os pulled from a self-describing
    # collector, before any such collector actually exists in production.
    huge = "A" * 5000
    ident = recognize({"upnp": {"make": huge, "model": huge}, "dhcp": {"os": huge}})
    assert len(ident.make) == 200
    assert len(ident.model) == 200
    assert len(ident.os) == 200


# ---- evidence trail -------------------------------------------------------------

def test_sources_have_shape_and_descend_by_weight():
    ident = recognize({
        "user_pin": "nas",
        "hostname": "synology-ds220",
        "vendor": "Synology",
        "open_ports": [445, 5000],
    })
    assert ident.device_type == "nas"
    weights = [s["weight"] for s in ident.sources]
    assert weights == sorted(weights, reverse=True)
    for s in ident.sources:
        assert set(s) == {"signal", "value", "weight"}


def test_to_dict_roundtrip():
    d = recognize({"vendor": "Sonos"}).to_dict()
    assert set(d) == {"device_type", "confidence", "make", "model", "os", "sources"}
    assert isinstance(d["sources"], list)


def test_all_outputs_stay_inside_the_taxonomy():
    for signals in ({}, {"vendor": "Apple"}, {"vendor": "Wyze"},
                    {"hostname": "esp8266-relay"}, {"open_ports": [62078]},
                    {"mac": "02:00:00:00:00:01", "vendor": "unknown"}):
        assert recognize(signals).device_type in DEVICE_TYPES


# ---- guess_device_type is a stable thin wrapper ---------------------------------

def test_wrapper_matches_recognize():
    assert guess_device_type("Sonos") == "speaker"
    assert guess_device_type("Apple", hostname="Johns-iPhone") == "phone"
    assert guess_device_type("unknown", mac="02:11:22:33:44:55") == "phone"
    assert isinstance(recognize({"vendor": "Sonos"}), DeviceIdentity)
