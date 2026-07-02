"""Bundled, offline OUI-prefix -> manufacturer table.

A curated subset of the IEEE OUI registry shipped IN the repo so vendor
resolution needs NO network call and NO online lookup -- this preserves Wavr's
zero-cloud-egress invariant. It is intentionally a small, high-signal slice
(common home / IoT vendors), not the full ~35k-row database.

Prefixes are the 24-bit OUI (first 3 octets) in lowercase colon form.
Manufacturer-assigned unicast MACs whose OUI is not in this subset resolve to
"unknown"; that is expected and correct for a curated table.
"""
from __future__ import annotations

# OUI (first 3 octets, lowercase colon form) -> canonical vendor name.
OUI_VENDORS: dict[str, str] = {}


def _register(vendor: str, *prefixes: str) -> None:
    for p in prefixes:
        OUI_VENDORS[p.lower()] = vendor


_register(
    "Apple",
    "00:03:93", "00:0a:27", "00:0a:95", "00:1b:63", "00:1e:c2", "00:1f:f3",
    "00:23:12", "00:25:00", "00:26:bb", "28:cf:e9", "3c:07:54", "3c:15:c2",
    "40:6c:8f", "60:33:4b", "68:a8:6d", "70:cd:60", "78:4f:43", "88:63:df",
    "90:72:40", "a4:83:e7", "a8:66:7f", "ac:bc:32", "b8:17:c2", "d0:23:db",
    "dc:a9:04", "f0:18:98", "f4:0f:24", "f8:1e:df",
)
_register(
    "Samsung",
    "00:00:f0", "00:07:ab", "00:12:fb", "00:15:99", "00:16:32", "00:1a:8a",
    "00:21:19", "08:37:3d", "34:23:87", "5c:0a:5b", "78:1f:db", "8c:77:12",
    "bc:44:86", "e8:50:8b", "f4:09:d8", "fc:a1:3e",
)
_register(
    "Google",
    "00:1a:11", "08:9e:08", "20:df:b9", "3c:5a:b4", "48:d6:d5", "54:60:09",
    "6c:ad:f8", "94:eb:2c", "a4:77:33", "da:a1:19", "f4:f5:d8", "f4:f5:e8",
)
_register(
    "Amazon",
    "00:bb:3a", "08:a6:bc", "0c:47:c9", "34:d2:70", "40:b4:cd", "44:65:0d",
    "50:dc:e7", "68:37:e9", "68:54:fd", "74:c2:46", "84:d6:d0", "a0:02:dc",
    "ac:63:be", "b0:fc:0d", "f0:d2:f1", "fc:65:de", "fc:a1:83",
)
_register(
    "Xiaomi",
    "00:9e:c8", "0c:1d:af", "10:2a:b3", "14:f6:5a", "18:59:36", "28:6c:07",
    "34:ce:00", "50:64:2b", "64:09:80", "64:cc:2e", "78:11:dc", "8c:be:be",
    "98:fa:e3", "f0:b4:29", "f8:a4:5f", "fc:64:ba",
)
_register(
    "Espressif",  # ESP32 / ESP8266 -- the dominant DIY-IoT silicon
    "18:fe:34", "24:0a:c4", "24:6f:28", "24:b2:de", "2c:3a:e8", "30:ae:a4",
    "3c:71:bf", "48:3f:da", "54:5a:a6", "5c:cf:7f", "60:01:94", "7c:9e:bd",
    "84:0d:8e", "84:cc:a8", "8c:aa:b5", "90:38:0c", "a0:20:a6", "a4:cf:12",
    "ac:d0:74", "b4:e6:2d", "bc:dd:c2", "c4:4f:33", "c8:2b:96", "cc:50:e3",
    "d8:a0:1d", "dc:4f:22", "ec:fa:bc",
)
_register(
    "Raspberry Pi",
    "28:cd:c1", "2c:cf:67", "b8:27:eb", "d8:3a:dd", "dc:a6:32", "e4:5f:01",
)
_register(
    "TP-Link",
    "00:1d:0f", "14:cc:20", "30:de:4b", "50:c7:bf", "54:af:97", "60:32:b1",
    "98:da:c4", "a4:2b:b0", "ac:84:c6", "b0:48:7a", "c0:06:c3", "ec:08:6b",
    "f4:f2:6d",
)
_register(
    "Intel",
    "00:1b:21", "00:1e:64", "00:21:6a", "00:24:d7", "34:41:5d", "34:e6:d7",
    "3c:a9:f4", "44:85:00", "7c:5c:f8", "8c:16:45", "94:65:9c", "a0:88:69",
    "ac:7b:a1", "b4:6b:fc", "e4:a7:a0", "f8:63:3f",
)
_register(
    "Ubiquiti",
    "00:15:6d", "04:18:d6", "18:e8:29", "24:5a:4c", "24:a4:3c", "44:d9:e7",
    "68:d7:9a", "74:83:c2", "78:8a:20", "80:2a:a8", "b4:fb:e4", "dc:9f:db",
    "f0:9f:c2", "fc:ec:da",
)
_register(
    "Netgear",
    "00:09:5b", "00:14:6c", "00:1e:2a", "20:e5:2a", "28:c6:8e", "2c:30:33",
    "44:94:fc", "9c:d3:6d", "a0:40:a0", "c0:3f:0e", "c4:04:15",
)
_register(
    "Sonos",
    "00:0e:58", "34:7e:5c", "48:a6:b8", "54:2a:1b", "5c:aa:fd", "78:28:ca",
    "94:9f:3e", "b8:e9:37", "e4:1c:41", "f0:f6:c1",
)
_register(
    "Microsoft",
    "00:12:5a", "00:15:5d", "00:17:fa", "28:18:78", "30:59:b7", "50:1a:c5",
    "60:45:bd", "7c:1e:52", "98:5f:d3", "9c:aa:1b", "c8:3f:26", "dc:a2:66",
)
_register(
    "Sony",
    "00:13:a9", "00:1a:80", "00:24:be", "30:f9:ed", "78:c8:81", "a8:e3:ee",
    "bc:60:a7", "f8:d0:ac", "fc:0f:e6",
)
_register(
    "Nintendo",
    "00:09:bf", "00:17:ab", "00:19:1d", "00:1a:e9", "18:2a:7b", "34:af:2c",
    "40:f4:07", "58:bd:a3", "78:a2:a0", "8c:cd:e8", "98:b6:e9", "e8:4e:ce",
)
_register(
    "Huawei",
    "00:66:4b", "00:e0:fc", "04:bd:70", "28:31:52", "48:46:fb", "4c:54:99",
    "70:72:3c", "80:fb:06", "9c:28:ef", "ac:e2:15", "e0:24:7f", "f4:c7:14",
)
_register(
    "HP",
    "00:1b:78", "00:21:5a", "3c:d9:2b", "70:5a:0f", "94:57:a5", "98:e7:f4",
    "a0:b3:cc", "a0:d3:c1", "d0:bf:9c", "ec:8e:b5",
)
_register(
    "Dell",
    "00:14:22", "00:21:9b", "00:24:e8", "14:18:77", "18:66:da", "24:b6:fd",
    "34:17:eb", "44:a8:42", "b8:2a:72", "b8:ca:3a", "d0:67:e5", "f8:bc:12",
)
_register(
    "LG",
    "00:1c:62", "00:1e:75", "00:22:a9", "10:68:3f", "2c:54:cf", "34:fc:ef",
    "40:b0:fa", "58:a2:b5", "60:e3:ac", "a8:16:b2", "c4:36:6c",
)
_register(
    "Lenovo",
    "00:59:07", "20:89:84", "54:ee:75", "68:f7:28", "70:72:0d", "c8:5b:76",
    "d8:5d:e2",
)
_register(
    "Asus",
    "00:1b:fc", "04:d4:c4", "08:60:6e", "1c:87:2c", "2c:56:dc", "38:d5:47",
    "50:46:5d", "60:45:cb", "74:d0:2b", "ac:22:0b", "bc:ee:7b", "d8:50:e6",
    "f0:2f:74",
)
_register("Nvidia", "00:04:4b", "00:41:5a", "48:b0:2d", "e0:63:da")
_register(
    "Roku",
    "00:0d:4b", "08:05:81", "ac:3a:7a", "b0:a7:37", "b8:3e:59", "cc:6d:a0",
    "d0:4d:2c", "dc:3a:5e",
)
_register("Wyze", "2c:aa:8e", "7c:78:b2", "a4:da:22", "d0:3f:27")
_register("Fitbit", "08:df:1f", "20:9b:a5", "6c:20:56", "e0:03:e1")
_register("Realtek", "00:e0:4c", "52:54:ab")


# Coarse device-type per vendor. Deliberately imprecise ("computer/mobile"):
# a MAC OUI identifies the silicon maker, not the exact product, so we only
# claim the vendor's typical device class(es).
VENDOR_DEVICE_TYPE: dict[str, str] = {
    "Apple": "computer/mobile",
    "Samsung": "mobile/appliance",
    "Google": "smart-home",
    "Amazon": "smart-speaker/iot",
    "Xiaomi": "mobile/iot",
    "Espressif": "iot-embedded",
    "Raspberry Pi": "sbc/iot",
    "TP-Link": "network-gear",
    "Intel": "computer",
    "Ubiquiti": "network-gear",
    "Netgear": "network-gear",
    "Sonos": "speaker",
    "Microsoft": "computer/console",
    "Sony": "console/entertainment",
    "Nintendo": "console",
    "Huawei": "mobile/network-gear",
    "HP": "computer/printer",
    "Dell": "computer",
    "LG": "mobile/tv",
    "Lenovo": "computer",
    "Asus": "computer/network-gear",
    "Nvidia": "computer/console",
    "Roku": "streaming",
    "Wyze": "camera/iot",
    "Fitbit": "wearable",
    "Realtek": "computer/nic",
}


def oui_prefix(mac: str) -> str:
    """Return the 24-bit OUI (first 3 octets) of a MAC in lowercase colon form."""
    parts = mac.replace("-", ":").lower().split(":")
    return ":".join(parts[:3])


def lookup_vendor(mac: str) -> str:
    """Resolve a MAC to its manufacturer via the bundled OUI table.

    Offline only -- never hits the network. Returns "unknown" for any OUI not
    in the curated subset (including randomized/locally-administered MACs).
    """
    return OUI_VENDORS.get(oui_prefix(mac), "unknown")


def is_locally_administered(mac: str) -> bool:
    """True if the MAC has the locally-administered bit set (bit 0x02 of the
    first octet). Modern phones rotate such randomized MACs for privacy, so a
    locally-administered address with an unknown OUI is likely a mobile device."""
    try:
        first = int(mac.replace("-", ":").split(":")[0], 16)
    except (ValueError, IndexError):
        return False
    return bool(first & 0x02)
