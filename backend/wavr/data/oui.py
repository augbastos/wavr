"""Bundled, offline OUI-prefix -> manufacturer table.

A curated subset of the IEEE OUI registry shipped IN the repo so vendor
resolution needs NO network call and NO online lookup -- this preserves Wavr's
zero-cloud-egress invariant. It is intentionally a small, high-signal slice
(common home / IoT vendors), not the full ~35k-row database.

Every prefix is a PUBLIC IEEE MA-L registration (standards-oui.ieee.org);
nothing here derives from any third-party product's database. A larger table
can be regenerated at DEV TIME from the public IEEE CSV via
``scripts/gen_oui.py`` (emits ``wavr/data/oui_generated.py``, used as a
fallback when present) -- the generator is never run at runtime and Wavr never
fetches anything.

Prefixes are the 24-bit OUI (first 3 octets) in lowercase colon form.
Manufacturer-assigned unicast MACs whose OUI is not in this subset resolve to
"unknown"; that is expected and correct for a curated table.
"""
from __future__ import annotations

# OUI (first 3 octets, lowercase colon form) -> canonical vendor name.
OUI_VENDORS: dict[str, str] = {}

try:  # optional dev-time generated table (scripts/gen_oui.py); absent by default
    from wavr.data.oui_generated import OUI_VENDORS_GENERATED  # type: ignore
except ImportError:  # pragma: no cover - default state: no generated table
    OUI_VENDORS_GENERATED: dict[str, str] = {}


def _register(vendor: str, *prefixes: str) -> None:
    for p in prefixes:
        OUI_VENDORS[p.lower()] = vendor


_register(
    "Apple",
    "00:03:93", "00:0a:27", "00:0a:95", "00:16:cb", "00:17:f2", "00:19:e3",
    "00:1b:63", "00:1c:b3", "00:1d:4f", "00:1e:c2", "00:1f:f3", "00:21:e9",
    "00:22:41", "00:23:12", "00:23:32", "00:25:00", "00:25:4b", "00:26:bb",
    "28:cf:e9", "3c:07:54", "3c:15:c2", "40:6c:8f", "60:33:4b", "68:a8:6d",
    "70:cd:60", "78:4f:43", "88:63:df", "8c:85:90", "90:72:40", "a4:83:e7",
    "a8:66:7f", "ac:87:a3", "ac:bc:32", "b0:34:95", "b8:17:c2", "d0:23:db",
    "dc:a9:04", "f0:18:98", "f0:99:bf", "f4:0f:24", "f8:1e:df",
)
_register(
    "Samsung",
    "00:00:f0", "00:07:ab", "00:12:fb", "00:15:99", "00:16:32", "00:1a:8a",
    "00:21:19", "08:37:3d", "34:23:87", "50:cc:f8", "5c:0a:5b", "78:1f:db",
    "8c:77:12", "bc:44:86", "cc:07:ab", "e8:50:8b", "f4:09:d8", "fc:a1:3e",
)
_register(
    "Google",
    "00:1a:11", "08:9e:08", "1c:f2:9a", "20:df:b9", "30:fd:38", "3c:5a:b4",
    "48:d6:d5", "54:60:09", "6c:ad:f8", "94:eb:2c", "a4:77:33", "da:a1:19",
    "e4:f0:42", "f4:f5:d8", "f4:f5:e8",
)
_register(
    "Nest",  # Nest Labs pre-Google-merge registrations (thermostats/cams)
    "18:b4:30", "64:16:66",
)
_register(
    "Amazon",
    "00:bb:3a", "08:a6:bc", "0c:47:c9", "34:d2:70", "40:b4:cd", "44:65:0d",
    "50:dc:e7", "68:37:e9", "68:54:fd", "74:c2:46", "84:d6:d0", "a0:02:dc",
    "ac:63:be", "b0:fc:0d", "f0:d2:f1", "fc:65:de", "fc:a1:83",
)
_register(
    "Ring",  # subset -- newer Ring hardware often ships under Amazon's OUIs
    "9c:76:13",
)
_register(
    "Xiaomi",
    "00:9e:c8", "0c:1d:af", "10:2a:b3", "14:f6:5a", "18:59:36", "28:6c:07",
    "34:ce:00", "50:64:2b", "64:09:80", "64:cc:2e", "78:11:dc", "8c:be:be",
    "98:fa:e3", "f0:b4:29", "f8:a4:5f", "fc:64:ba",
)
_register(
    "Aqara",  # Lumi United Technology (Aqara's legal entity)
    "54:ef:44",
)
_register(
    "Espressif",  # ESP32 / ESP8266 -- the dominant DIY-IoT silicon.
    # NOTE: Sonoff/eWeLink and most Tuya white-label gear ship Espressif (or
    # Realtek) radios, so they usually resolve HERE -- there is no stable
    # "Sonoff OUI"; hostname patterns are the correct signal for that family.
    "10:52:1c", "18:fe:34", "24:0a:c4", "24:6f:28", "24:b2:de", "2c:3a:e8",
    "30:ae:a4", "3c:71:bf", "40:22:d8", "48:3f:da", "54:5a:a6", "58:bf:25",
    "5c:cf:7f", "60:01:94", "68:c6:3a", "7c:9e:bd", "84:0d:8e", "84:cc:a8",
    "8c:aa:b5", "90:38:0c", "94:b9:7e", "a0:20:a6", "a4:cf:12", "ac:d0:74",
    "b4:e6:2d", "bc:dd:c2", "c4:4f:33", "c8:2b:96", "c8:c9:a3", "cc:50:e3",
    "d8:a0:1d", "d8:f1:5b", "dc:4f:22", "e0:98:06", "e8:db:84", "ec:fa:bc",
    "f4:cf:a2",
)
_register(
    "Tuya",  # Tuya Smart's own MA-L blocks (their white-label ecosystem also
    "68:57:2d", "d4:a6:51",  # rides Espressif/Realtek radios -- see above)
)
_register(
    "Raspberry Pi",
    "28:cd:c1", "2c:cf:67", "b8:27:eb", "d8:3a:dd", "dc:a6:32", "e4:5f:01",
)
_register(
    "TP-Link",
    "00:1d:0f", "14:cc:20", "18:d6:c7", "30:de:4b", "50:c7:bf", "50:d4:f7",
    "54:af:97", "60:32:b1", "84:16:f9", "98:da:c4", "a4:2b:b0", "ac:84:c6",
    "b0:48:7a", "c0:06:c3", "d8:07:b6", "e8:48:b8", "ec:08:6b", "f4:f2:6d",
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
    "Linksys",
    "00:25:9c", "48:f8:b3", "c0:c1:c0",
)
_register(
    "D-Link",
    "00:17:9a", "00:1b:11", "14:d6:4d", "1c:7e:e5", "84:c9:b2",
)
_register(
    "Sonos",
    "00:0e:58", "34:7e:5c", "48:a6:b8", "54:2a:1b", "5c:aa:fd", "78:28:ca",
    "94:9f:3e", "b8:e9:37", "e4:1c:41", "f0:f6:c1",
)
_register(
    "Bose",
    "04:52:c7",
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
    "Sony PlayStation",  # Sony Interactive Entertainment registrations
    "00:d9:d1", "28:0d:fc",
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
_register("Brother", "00:80:77")
_register("Canon", "00:1e:8f")
_register("Epson", "00:26:ab")
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
_register(
    "Hikvision",  # Hangzhou Hikvision has 80+ MA-L blocks; high-signal subset
    "0c:75:d2", "24:48:45", "28:57:be", "44:19:b6", "54:8c:81", "bc:ad:28",
    "c4:2f:90",
)
_register(
    "Dahua",
    "08:ed:ed", "14:a7:8b", "74:c9:29", "9c:14:63", "e4:24:6c",
)
_register("Reolink", "ec:71:db")
_register("Wyze", "2c:aa:8e", "7c:78:b2", "a4:da:22", "d0:3f:27")
_register("Fitbit", "08:df:1f", "20:9b:a5", "6c:20:56", "e0:03:e1")
_register("Garmin", "10:c6:fc")
_register("OnePlus", "94:65:2d", "c0:ee:fb")
_register("Motorola", "f8:cf:c5")
_register("Synology", "00:11:32", "90:09:d0")
_register("QNAP", "24:5e:be")
_register("Western Digital", "00:90:a9")
_register(
    "Philips Hue",  # Philips Lighting / Signify (Hue bridges + luminaires)
    "00:17:88", "ec:b5:fa",
)
_register("LIFX", "d0:73:d5")
_register(
    "Belkin",  # incl. the Wemo smart-plug line
    "94:10:3e", "b4:75:0e", "ec:1a:59",
)
_register("Ecobee", "44:61:32")
_register("Realtek", "00:e0:4c", "52:54:ab")


# Vendor -> (default device_type, confidence). SINGLE-DOMINANT-PRODUCT vendors
# only: a multi-product vendor (Apple, Samsung, Sony, HP, ...) with no other
# signal deliberately falls through rather than getting a forced guess --
# never fake precision the data doesn't have. Types are the fixed taxonomy in
# wavr.data.deviceclass.DEVICE_TYPES; confidence is "high" | "medium" | "low".
VENDOR_DEFAULT_TYPE: dict[str, tuple[str, str]] = {
    # single-product vendors -> high
    "Sonos": ("speaker", "high"),
    "Bose": ("speaker", "high"),
    "Roku": ("streaming_stick", "high"),
    "Nintendo": ("console", "high"),
    "Sony PlayStation": ("console", "high"),
    "Hikvision": ("camera", "high"),
    "Dahua": ("camera", "high"),
    "Reolink": ("camera", "high"),
    "Fitbit": ("wearable", "high"),
    "Synology": ("nas", "high"),
    "QNAP": ("nas", "high"),
    "Brother": ("printer", "high"),
    "Epson": ("printer", "high"),
    "OnePlus": ("phone", "high"),
    # dominant-but-not-only product lines -> medium
    "Wyze": ("camera", "medium"),          # also sells plugs/locks
    "Ring": ("camera", "medium"),          # doorbells/cams
    "Aqara": ("iot_sensor", "medium"),     # sensors are the plurality; hostname overrides hubs/plugs
    "Ecobee": ("iot_sensor", "medium"),    # thermostats/sensors
    "Nest": ("iot_sensor", "medium"),      # thermostats/protect/cams
    "Ubiquiti": ("router", "medium"),
    "Netgear": ("router", "medium"),
    "TP-Link": ("router", "medium"),
    "Linksys": ("router", "medium"),
    "D-Link": ("router", "medium"),
    "Asus": ("router", "medium"),          # router fleet dominant on LAN scans
    "Espressif": ("esp_dev", "medium"),    # silicon signal; DIY-IoT is the dominant use
    "Amazon": ("speaker", "medium"),       # Echo fleet dominant; hostname overrides FireTV/Ring
    "Philips Hue": ("smart_plug", "medium"),  # smart lighting -> closest taxonomy bucket
    "LIFX": ("smart_plug", "medium"),
    "Belkin": ("smart_plug", "medium"),    # Wemo line dominant on home LANs
    "Garmin": ("wearable", "medium"),
    "Canon": ("printer", "medium"),        # their LAN presence is printers
    "Motorola": ("phone", "medium"),
    "Western Digital": ("nas", "medium"),
    # genuinely ambiguous -> honest low
    "Raspberry Pi": ("iot_sensor", "low"), # could be NAS/hub/desktop/sensor node
    "Tuya": ("smart_plug", "low"),         # white-label anything
    "Realtek": ("unknown", "low"),         # pure silicon vendor, no signal at all
}

# Vendors whose MACs on a home LAN are, in the plurality, phones -- used as a
# LOW-confidence fallback when no better signal exists. (These vendors sell
# across 3+ categories, so they get no VENDOR_DEFAULT_TYPE entry.)
MOBILE_HEAVY_VENDORS: frozenset[str] = frozenset(
    {"Apple", "Samsung", "Xiaomi", "Google", "Huawei", "LG"}
)


def oui_prefix(mac: str) -> str:
    """Return the 24-bit OUI (first 3 octets) of a MAC in lowercase colon form."""
    parts = mac.replace("-", ":").lower().split(":")
    return ":".join(parts[:3])


def lookup_vendor(mac: str) -> str:
    """Resolve a MAC to its manufacturer via the bundled OUI table.

    Offline only -- never hits the network. Curated table first, then the
    optional dev-time generated table (if present). Returns "unknown" for any
    OUI in neither (including randomized/locally-administered MACs).
    """
    prefix = oui_prefix(mac)
    return OUI_VENDORS.get(prefix) or OUI_VENDORS_GENERATED.get(prefix, "unknown")


def is_locally_administered(mac: str) -> bool:
    """True if the MAC has the locally-administered bit set (bit 0x02 of the
    first octet). Modern phones rotate such randomized MACs for privacy, so a
    locally-administered address with an unknown OUI is likely a mobile device."""
    try:
        first = int(mac.replace("-", ":").split(":")[0], 16)
    except (ValueError, IndexError):
        return False
    return bool(first & 0x02)
