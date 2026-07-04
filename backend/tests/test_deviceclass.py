"""wavr.data.deviceclass -- taxonomy sanity + the hostname_type tier.

NOTE: the module used to also ship a parallel `classify_device` cascade with
its own ~15 tests, but it had ZERO production callers (the production path is
exclusively `wavr.recog.recognize`) -- that gave false coverage and drift risk
(M... audit fix), so it and its cascade tests were deleted. The taxonomy
sanity tests below are the ones that actually guard the shared contract."""
from wavr.data.deviceclass import (
    CONFIDENCE_LEVELS,
    DEVICE_TYPES,
    HOSTNAME_PATTERNS,
    hostname_type,
)
from wavr.data.oui import MOBILE_HEAVY_VENDORS, OUI_VENDORS, VENDOR_DEFAULT_TYPE
from wavr.data.ports import DEVICE_TYPE_HINTS


# ---- taxonomy sanity (the contract every surface keys off) -------------------

def test_taxonomy_is_the_fixed_18_value_set():
    assert len(DEVICE_TYPES) == 18
    assert len(set(DEVICE_TYPES)) == 18
    assert "unknown" in DEVICE_TYPES
    assert {"router", "gateway", "phone", "camera", "esp_dev"} <= set(DEVICE_TYPES)


def test_every_hostname_pattern_targets_a_taxonomy_value():
    assert all(dtype in DEVICE_TYPES for _, dtype in HOSTNAME_PATTERNS)


def test_every_vendor_default_is_taxonomy_plus_confidence():
    for vendor, (dtype, conf) in VENDOR_DEFAULT_TYPE.items():
        assert dtype in DEVICE_TYPES, vendor
        assert conf in CONFIDENCE_LEVELS, vendor


def test_every_port_hint_type_is_taxonomy_or_none():
    for port, (dtype, note) in DEVICE_TYPE_HINTS.items():
        assert dtype is None or dtype in DEVICE_TYPES, port
        assert note


def test_mobile_heavy_vendors_have_no_forced_default():
    # multi-product vendors must fall through, never get a forced guess
    assert not MOBILE_HEAVY_VENDORS & set(VENDOR_DEFAULT_TYPE)


def test_vendor_tables_stay_in_sync_with_the_oui_registry():
    # L5 audit fix: every vendor referenced by VENDOR_DEFAULT_TYPE/MOBILE_HEAVY_VENDORS
    # must still be a real, resolvable OUI_VENDORS value -- a future vendor-string
    # rename in oui.py would otherwise silently degrade that whole fleet to "unknown"
    # (wrong device_type/confidence) instead of failing loudly here.
    assert (set(VENDOR_DEFAULT_TYPE) | set(MOBILE_HEAVY_VENDORS)) <= set(OUI_VENDORS.values())


# ---- hostname regex tier -------------------------------------------------------

def test_hostname_tier_is_high_confidence():
    assert hostname_type("Johns-iPhone") == "phone"
    assert hostname_type("living-room-tv") == "tv"
    assert hostname_type("Tapo-C210") == "camera"
    assert hostname_type("Tapo-P100") == "smart_plug"
    assert hostname_type("esp32-motion") == "esp_dev"


def test_hostname_regex_scopes_ambiguous_words():
    # "nintendo-switch" must be a console, never a network switch
    assert hostname_type("nintendo-switch") == "console"
    # \btv\b must not fire inside another word
    assert hostname_type("motv-something") is None
    # \bcam\b must not fire inside a name like "camila"
    assert hostname_type("camila-pc-") == "desktop"
