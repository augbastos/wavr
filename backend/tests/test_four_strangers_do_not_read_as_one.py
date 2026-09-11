"""Four unknown devices have to read as four devices.

## What the screen said

Space status listed, stacked, in this order:

    Network  note  unrecognized device on the network (unknown)  · now
    Network  note  unrecognized device on the network (unknown)  · now
    Network  note  unrecognized device on the network (unknown)  · now
    Network  note  unrecognized device on the network (unknown)  · now

Four separate machines on one network. The caption was built from the alert's
`vendor` alone, and the vendor of a device Wavr has not recognised is very often
the literal string `"unknown"` -- so the field meant to name the device named
nothing, four times.

A list whose rows are identical is not a list of four things. It is one thing
with a count, and it cannot be acted on: a reader cannot tell which row they
already checked, which one they went to the Network tab about, or whether the
fourth is new since yesterday.

## Why it survived

The fixtures said `vendor: "Acme"`. Every test asked whether a caption appeared,
with a vendor that identifies a device -- so the case that reaches real users,
where the lookup failed, was never rendered in a test at all.

`hostname`, `ip` and `mac` were on the alert the whole time and were dropped on
the floor. Nothing here reads the network, and nothing leaves the Space: the
Network tab one click away lists all three in full, for the same reader, on the
same authenticated dashboard.
"""
from __future__ import annotations

import pytest

from wavr.house_status import _network_what


def _alert(**over):
    a = {"kind": "rogue_device", "vendor": "unknown", "ip": "", "mac": "",
         "hostname": None}
    a.update(over)
    return a


def test_devices_that_differ_get_captions_that_differ():
    """The defect, stated as the screen showed it."""
    seen = [_network_what(_alert(ip=ip, mac=mac), "rogue_device")
            for ip, mac in (("192.168.1.1", "18:ef:c0:11:34:60"),
                            ("192.168.1.4", "50:3d:d1:46:eb:08"),
                            ("192.168.1.9", "00:15:5d:aa:bb:cc"),
                            ("192.168.1.22", "78:20:51:00:11:22"))]
    assert len(set(seen)) == len(seen), (
        "four devices, fewer than four captions:\n  " + "\n  ".join(seen))


@pytest.mark.parametrize("alert,expected,why", [
    (_alert(hostname="nas.local", vendor="Acme", ip="192.168.1.7"),
     "nas.local", "a name a person may recognise beats a vendor"),
    (_alert(vendor="Microsoft", ip="192.168.1.9"),
     "Microsoft", "a vendor that identifies is still the friendliest label"),
    (_alert(ip="192.168.1.4", mac="50:3d:d1:46:eb:08"),
     "192.168.1.4", "the address they would search the Network tab for"),
    (_alert(mac="50:3d:d1:46:eb:08"),
     "50:3d:d1:46:eb:08", "the hardware address, when nothing else is known"),
    (_alert(),
     "unknown vendor", "and when truly nothing is known, say so plainly"),
])
def test_it_uses_the_most_useful_thing_it_actually_has(alert, expected, why):
    assert f"({expected})" in _network_what(alert, "rogue_device"), why


def test_a_failed_vendor_lookup_is_not_treated_as_a_vendor():
    """`"unknown"` is a lookup that failed. Printing it names nothing, and the
    device's address — which does name something — was right there."""
    for vendor in ("unknown", "Unknown", "UNKNOWN", "unknown vendor"):
        out = _network_what(_alert(vendor=vendor, ip="192.168.1.4"),
                            "rogue_device")
        assert "192.168.1.4" in out, f"{vendor!r} was used as if it identified something"


def test_the_sentence_is_still_one_a_catalogue_can_hold():
    """The control for the fix.

    This module splits the literal from the data on purpose: a caption of
    `f"unrecognized device on the network ({vendor})"` puts a household's
    network data inside the string a translating client looks up, so the
    sentence exists in no source file and stays English forever. Making the
    bracket more useful must not undo that.
    """
    out = _network_what(_alert(ip="192.168.1.4"), "rogue_device")
    assert out.startswith("unrecognized device on the network ("), out
    assert out.endswith(")"), out
    # The translatable half is a constant: same sentence, different device.
    other = _network_what(_alert(hostname="nas.local"), "rogue_device")
    assert out.split(" (")[0] == other.split(" (")[0]
