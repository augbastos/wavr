"""Capability Manifest: honest tristates, bounded parsing, and a recommendation
an operator can argue with."""
import pytest

from wavr.capabilities import (
    CAPABILITY_KEYS, ROLE_CLIENT, ROLE_CORE, ROLE_NODE, TIER_HIGH, TIER_LOW,
    TIER_MEDIUM, TIER_MICRO, CapabilityManifest, _compute_tier, recommend,
    scan_host,
)


def _manifest(**kw) -> CapabilityManifest:
    base = dict(platform="linux", functions_supported=(ROLE_CORE, ROLE_NODE, ROLE_CLIENT),
                capabilities={}, ram_mb=8000, cpu_count=8, compute_tier=TIER_HIGH)
    caps = kw.pop("capabilities", None)
    base.update(kw)
    if caps is not None:
        base["capabilities"] = caps
    return CapabilityManifest(**base)


# -- The honesty rule --------------------------------------------------------

def test_absent_capability_is_unknown_not_false():
    # The load-bearing rule: a probe that did not run must read as None, so the
    # UI can say "unknown" instead of claiming the hardware is absent.
    m = _manifest(capabilities={"wifi": True})
    assert m.capability("wifi") is True
    assert m.capability("camera") is None
    assert m.capability("camera") is not False


def test_from_dict_rejects_truthy_junk_as_unknown():
    # "1"/"yes"/[] are what a device that didn't read the spec sends. Coercing
    # them to True would manufacture a capability out of sloppiness.
    m = CapabilityManifest.from_dict({"capabilities": {
        "wifi": "1", "ble": "yes", "camera": [], "gpu": 1, "usb": True}})
    assert m.capability("wifi") is None
    assert m.capability("ble") is None
    assert m.capability("camera") is None
    assert m.capability("gpu") is None
    assert m.capability("usb") is True


def test_from_dict_drops_unknown_capability_keys():
    m = CapabilityManifest.from_dict({"capabilities": {"wifi": True, "mind_reading": True}})
    assert m.capability("wifi") is True
    assert "mind_reading" not in m.capabilities


def test_from_dict_bounds_every_field():
    m = CapabilityManifest.from_dict({
        "platform": "solaris",           # not in PLATFORMS
        "arch": "x" * 500,
        "os_version": "y" * 500,
        "model": "z" * 500,
        "ram_mb": 10 ** 12,              # absurd
        "cpu_count": -5,
        "compute_tier": "cosmic",
        "functions_supported": ["core", "toaster"],
        "protocol_version": 0,
    })
    assert m.platform == "unknown"
    assert len(m.arch) <= 32 and len(m.os_version) <= 64 and len(m.model) <= 96
    assert m.ram_mb == 4_194_304
    assert m.cpu_count == 0
    assert m.compute_tier == TIER_LOW
    assert m.functions_supported == ("core",)
    assert m.protocol_version == 1


def test_from_dict_rejects_bool_as_a_number():
    # True is an int in Python; a manifest saying ram_mb=True must not become 1 MB.
    m = CapabilityManifest.from_dict({"ram_mb": True, "cpu_count": False})
    assert m.ram_mb is None
    assert m.cpu_count is None


def test_from_dict_rejects_non_object():
    with pytest.raises(ValueError):
        CapabilityManifest.from_dict(["not", "a", "manifest"])


def test_round_trip_through_dict():
    m = _manifest(capabilities={"wifi": True, "camera": False, "gpu": None})
    again = CapabilityManifest.from_dict(m.to_dict())
    assert again.capabilities == m.capabilities
    assert again.functions_supported == m.functions_supported
    assert again.compute_tier == m.compute_tier


# -- Compute tiers -----------------------------------------------------------

@pytest.mark.parametrize("ram,cpus,expect", [
    (16000, 8, TIER_HIGH),
    (8000, 4, TIER_HIGH),
    (8000, 2, TIER_MEDIUM),    # plenty of RAM but too few cores for HIGH
    (4000, 4, TIER_MEDIUM),
    (1000, 4, TIER_LOW),
    (256, 1, TIER_MICRO),
])
def test_compute_tier_buckets(ram, cpus, expect):
    assert _compute_tier(ram, cpus, "linux") == expect


def test_unknown_ram_degrades_to_low_not_high():
    # Guessing HIGH on unknown hardware tells the operator to run camera
    # inference on a Pi Zero; guessing LOW merely under-sells.
    assert _compute_tier(None, 8, "linux") == TIER_LOW


def test_esp32_is_always_micro():
    assert _compute_tier(999999, 64, "esp32") == TIER_MICRO


# -- Recommendation ----------------------------------------------------------

def test_laptop_with_no_core_in_space_is_recommended_primary():
    m = _manifest(capabilities={"display": True, "wifi": True,
                                "battery": False, "permanent_power": True})
    rec = recommend(m, space_has_core=False)
    assert ROLE_CORE in rec.functions
    assert ROLE_CLIENT in rec.functions
    assert not rec.prefer_standby
    assert rec.reasons, "a recommendation with no reasons is a guess"


def test_same_laptop_joins_when_the_space_already_has_a_core():
    m = _manifest(capabilities={"display": True, "wifi": True})
    rec = recommend(m, space_has_core=True)
    assert ROLE_CORE not in rec.functions
    assert ROLE_CORE in rec.also_possible
    assert any("already has a Core" in r for r in rec.reasons)


def test_battery_core_is_flagged_portable_not_primary():
    m = _manifest(platform="android", capabilities={
        "display": True, "wifi": True, "battery": True, "permanent_power": False})
    rec = recommend(m, space_has_core=False)
    assert ROLE_CORE in rec.functions
    assert rec.prefer_standby
    assert "Portable Core" in rec.label
    assert any("battery" in r.lower() for r in rec.reasons)


def test_headless_pi_gets_core_and_node_but_not_client():
    m = _manifest(platform="linux", compute_tier=TIER_MEDIUM, ram_mb=2000,
                  capabilities={"display": False, "wifi": True, "ble": True,
                                "permanent_power": True})
    rec = recommend(m, space_has_core=False)
    assert ROLE_CORE in rec.functions
    assert ROLE_NODE in rec.functions
    assert ROLE_CLIENT not in rec.functions
    assert any("No screen" in r for r in rec.reasons)


def test_unknown_display_still_gets_client():
    # None (unknown) must not be treated as "no screen".
    m = _manifest(capabilities={"wifi": True})
    assert ROLE_CLIENT in recommend(m).functions


def test_device_that_supports_nothing_falls_back_to_client_with_a_reason():
    m = CapabilityManifest(platform="unknown", functions_supported=(),
                           capabilities={"display": False})
    rec = recommend(m)
    assert rec.functions == (ROLE_CLIENT,)
    assert any("couldn't detect" in r for r in rec.reasons)


def test_recommendation_serialises_for_the_ui():
    d = recommend(_manifest(capabilities={"display": True, "camera": True})).to_dict()
    assert set(d) == {"functions", "label", "reasons", "also_possible", "prefer_standby"}
    assert isinstance(d["reasons"], list)


# -- The real host scan ------------------------------------------------------

def test_scan_host_returns_a_valid_manifest_on_this_machine():
    # Runs the REAL probes. It must never raise, whatever this OS is.
    m = scan_host()
    assert m.platform in ("windows", "linux", "macos", "android", "ios", "unknown")
    assert ROLE_NODE in m.functions_supported
    assert set(m.capabilities) <= set(CAPABILITY_KEYS)
    for key, val in m.capabilities.items():
        assert val is None or isinstance(val, bool), f"{key} is a tristate, got {val!r}"


def test_windows_interface_named_plain_wifi_is_detected(monkeypatch):
    # Regression: some Windows laptops name the adapter "WiFi", with no
    # hyphen. An earlier "wi-fi"-only match reported such a machine as having
    # NO Wi-Fi -- a confident False about hardware that was plainly there.
    # Measured on a real machine; which machine is not the point and is not
    # this repository's business.
    import wavr.capabilities as cap

    monkeypatch.setattr(cap.sys, "platform", "win32")
    monkeypatch.setattr(cap, "_run", lambda cmd: (
        "Admin State    State          Type             Interface Name\n"
        "-------------------------------------------------------------\n"
        "Enabled        Connected      Dedicated        WiFi\n"
        "Enabled        Disconnected   Dedicated        Ethernet\n"))
    assert cap._has_ethernet_and_wifi() == (True, True)


@pytest.mark.parametrize("name", ["WiFi", "Wi-Fi", "Wireless Network Connection",
                                  "WLAN", "Wi-Fi 2"])
def test_every_common_windows_wifi_spelling_is_recognised(monkeypatch, name):
    import wavr.capabilities as cap

    monkeypatch.setattr(cap.sys, "platform", "win32")
    monkeypatch.setattr(cap, "_run", lambda cmd: f"Enabled Connected Dedicated {name}\n")
    assert cap._has_ethernet_and_wifi()[1] is True


def test_windows_non_match_is_unknown_not_a_confident_no(monkeypatch):
    # Interface names are renameable and localised, so failing to recognise one
    # proves nothing. It must read as "couldn't tell", never as "absent".
    import wavr.capabilities as cap

    monkeypatch.setattr(cap.sys, "platform", "win32")
    monkeypatch.setattr(cap, "_run", lambda cmd: "Enabled Connected Dedicated Rede\n")
    assert cap._has_ethernet_and_wifi() == (None, None)


def test_windows_probe_failure_is_unknown(monkeypatch):
    import wavr.capabilities as cap

    monkeypatch.setattr(cap.sys, "platform", "win32")
    monkeypatch.setattr(cap, "_run", lambda cmd: None)
    assert cap._has_ethernet_and_wifi() == (None, None)


def test_the_capability_scan_never_imports_a_heavy_optional_extra():
    # Regression: an earlier version answered "has Bluetooth?" by importing
    # bleak, which drags the whole WinRT stack into the process during a
    # first-run probe -- and broke test_ble.py's assertion that merely importing
    # Wavr code never pulls bleak in. Presence is resolved with find_spec now.
    import sys

    heavy = ("bleak", "serial", "zeroconf", "torch", "cv2", "ultralytics",
             "paho", "paho.mqtt")
    already = {m for m in heavy if m in sys.modules}
    scan_host()
    pulled = {m for m in heavy if m in sys.modules} - already
    assert not pulled, f"the capability scan imported {sorted(pulled)}"


def test_module_present_is_a_tristate():
    from wavr.capabilities import _module_present

    # A module that certainly exists, and one that certainly does not.
    assert _module_present("json") is True
    assert _module_present("wavr_definitely_not_a_real_module") is False


def test_macos_wifi_is_unknown_not_a_confident_no(monkeypatch):
    # macOS names its Wi-Fi interface en0/en1, never wl*, so the Linux name
    # heuristic returned a confident False on every Mac -- the same fabricated
    # negative already fixed for Windows, one branch down.
    import wavr.capabilities as cap

    monkeypatch.setattr(cap.sys, "platform", "darwin")
    monkeypatch.setattr(cap.socket, "if_nameindex",
                        lambda: [(1, "lo0"), (2, "en0"), (3, "en1")])
    eth, wifi = cap._has_ethernet_and_wifi()
    assert eth is True
    assert wifi is None, "a Mac's Wi-Fi is not absent just because it isn't wl*"


def test_linux_keeps_its_confident_negative(monkeypatch):
    # Kernel names on Linux ARE predictable, so a negative stays defensible.
    import wavr.capabilities as cap

    monkeypatch.setattr(cap.sys, "platform", "linux")
    monkeypatch.setattr(cap.os, "listdir", lambda p: ["lo", "eth0"])
    assert cap._has_ethernet_and_wifi() == (True, False)


def test_mmwave_is_never_inferred_from_a_python_package():
    # `pyserial` being importable says Wavr COULD talk to a serial radar. It
    # does not say one is plugged in, and its absence does not say one isn't.
    # `recommend()` turns a True here into "it has a radar sensor".
    assert scan_host().capability("mmwave") is None


def test_scan_host_survives_a_probe_that_explodes(monkeypatch):
    # It is called from inside first-run Space creation; an exception here used
    # to abort the setup itself.
    import wavr.capabilities as cap

    monkeypatch.setattr(cap, "_ram_mb", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    m = cap.scan_host()
    assert isinstance(m, CapabilityManifest)
    assert ROLE_NODE in m.functions_supported


def test_module_present_is_unknown_when_resolution_breaks(monkeypatch):
    # The docstring always promised None (not False) for a broken install; the
    # code returned False for every ImportError.
    import importlib.util

    import wavr.capabilities as cap

    def _boom(name):
        raise ImportError("half-installed native extension")

    monkeypatch.setattr(importlib.util, "find_spec", _boom)
    assert cap._module_present("anything") is None


def test_scan_host_never_claims_permanent_power_it_cannot_prove():
    m = scan_host()
    if m.capability("battery") is None:
        assert m.capability("permanent_power") is None
