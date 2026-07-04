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
