"""Bundled port -> service / device-type hint table.

Authored by Wavr from PUBLIC data only: the IANA Service Name and Transport
Protocol Port Number Registry plus common-knowledge service conventions
(Bonjour/SSDP RFCs, vendor-documented defaults like Plex 32400 or Roku ECP
8060). No third-party product's port database, wording, or curated scan list
was copied -- this table is Wavr's own selection in Wavr's own words.

Two tiers, mirroring the quick/full split every LAN scanner converges on:
- ``QUICK_SCAN_PORTS`` -- ~15 high-signal TCP ports for the fast default pass.
- ``DEVICE_TYPE_HINTS`` -- port -> (device_type | None, note). The type is one
  of wavr.data.deviceclass.DEVICE_TYPES when the port is genuinely
  type-diagnostic, else None (informative note only, e.g. SNMP/discovery).

PURE DATA + pure helpers -- zero network I/O in this module. The actual
connect-only, OPT-IN, DEFAULT-OFF probe lives in wavr.netutils.annotate_ports
(gated by WAVR_NET_PORTSCAN, same seam as the existing risky-port awareness).
"""
from __future__ import annotations

# Fast default sweep: TCP-connect-checkable, high device-type yield. UDP
# discovery ports (5353 mDNS / 1900 SSDP) are deliberately NOT here -- they
# can't be meaningfully TCP-connect-checked; they belong to future passive
# protocol collectors (see DEVICE_TYPE_HINTS notes).
QUICK_SCAN_PORTS: tuple[int, ...] = (
    22, 23, 53, 80, 139, 443, 445, 554, 3389, 5000, 8009, 8080, 9100,
    32400, 62078,
)

# port -> (device_type hint or None, human note). All type values MUST be in
# wavr.data.deviceclass.DEVICE_TYPES (unit-tested); None = the open port is
# informative but not type-diagnostic on its own.
DEVICE_TYPE_HINTS: dict[int, tuple[str | None, str]] = {
    21:    (None, "FTP file transfer"),
    22:    (None, "SSH remote shell"),
    23:    (None, "Telnet (legacy remote login)"),
    53:    ("router", "DNS server -- router or DNS appliance (e.g. Pi-hole)"),
    80:    (None, "HTTP web interface"),
    139:   ("desktop", "NetBIOS session -- Windows file sharing"),
    161:   (None, "SNMP agent -- managed network device"),
    443:   (None, "HTTPS web interface"),
    445:   ("desktop", "SMB file sharing -- Windows PC or NAS"),
    515:   ("printer", "LPR/LPD print spooler"),
    548:   ("nas", "AFP -- Apple file sharing (Mac or NAS)"),
    554:   ("camera", "RTSP video stream -- IP camera or NVR"),
    631:   ("printer", "IPP printing (CUPS / AirPrint)"),
    1883:  ("gateway", "MQTT broker -- home-automation hub"),
    1900:  (None, "SSDP/UPnP discovery (UDP -- passive collector territory)"),
    3389:  ("desktop", "RDP remote desktop -- Windows PC"),
    5000:  ("nas", "Synology DSM web UI (also generic UPnP/dev servers)"),
    5353:  (None, "mDNS/Bonjour discovery (UDP -- passive collector territory)"),
    7000:  ("streaming_stick", "AirPlay receiver (Apple TV and others)"),
    8008:  ("streaming_stick", "Google Cast web port (Chromecast)"),
    8009:  ("streaming_stick", "Google Cast control (Chromecast / cast TV)"),
    8060:  ("streaming_stick", "Roku External Control Protocol"),
    8080:  (None, "alternate HTTP admin UI"),
    8443:  (None, "alternate HTTPS admin UI"),
    8554:  ("camera", "alternate RTSP stream port"),
    9100:  ("printer", "JetDirect / raw network printing"),
    32400: ("nas", "Plex Media Server (always-on media box, often a NAS)"),
    62078: ("phone", "iPhone/iPad Wi-Fi sync (usbmuxd)"),
}

# Most-diagnostic-first order for picking ONE type hint from a set of open
# ports (an iPhone sync port outweighs generic SMB; a printer port outweighs
# an RDP guess). Only ports with a non-None type appear here.
_HINT_PRIORITY: tuple[int, ...] = (
    62078, 9100, 631, 515, 554, 8554, 8009, 8008, 8060, 7000, 32400, 548,
    5000, 1883, 3389, 445, 139, 53,
)


def port_type_hint(open_ports) -> tuple[str, str] | None:
    """Strongest device-type hint for a set of open ports.

    Returns (device_type, note) from the most diagnostic open port, or None
    when nothing type-diagnostic is open. Pure/offline -- no probing here.
    """
    if not open_ports:
        return None
    opens = set(open_ports)
    for port in _HINT_PRIORITY:
        if port in opens:
            dtype, note = DEVICE_TYPE_HINTS[port]
            if dtype:
                return dtype, f"{note} ({port})"
    return None
