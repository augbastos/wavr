"""Wavr NetBIOS (NBSTAT) collector -- strongest Windows-PC identity signal.

Like sources.snmp, this is an ACTIVE, TARGETED unicast probe, not a passive
multicast listener: NetBIOS Name Service (NBNS, RFC 1001/1002) has no
multicast discovery equivalent to eavesdrop on, so this module sends one
NBSTAT ("Node Status") query datagram to each caller-supplied target IP (e.g.
hosts the ARP inventory already knows about -- this module never invents its
own target list/subnet sweep) and parses the reply's name table.

OPT-IN, default OFF -- like every Wavr source, this module does not read the
environment itself; the integration step wires a `WAVR_NET_NETBIOS` flag
gating whether `NetBIOSCollector` is ever constructed. Being an active
per-host probe, it carries the same "active scanning has real LAN impact"
caveat as sources.snmp -- see that module's docstring.

NBSTAT wire format (RFC 1002 4.2.18): the query's question name is the
literal wildcard "*" padded with NUL bytes to 16 raw bytes, first-level
encoded (RFC 1001 4.1: each raw byte -> two ASCII nibble characters 'A'-'P'),
query type 0x21 (NBSTAT), class 0x01 (IN). The response's RDATA is
NUM_NAMES(1) followed by that many 18-byte NetBIOS-name entries (15-byte
space-padded name + 1-byte "suffix" service code + 2-byte flags), then a
vendor-defined statistics block whose first 6 bytes are the responder's own
MAC address. Suffix codes used here (all IBM/Microsoft-documented, public,
decades-old NetBIOS conventions -- not any third-party product's table):
    0x00 unique  -> the host's own registered computer name
    0x00 group   -> the workgroup/domain name
    0x03 unique  -> legacy Messenger-service name (historically the logged-on
                    user on older Windows -- "net send" target)
    0x1B unique  -> Domain Master Browser (typically the PDC)
    0x1C group   -> Domain Controllers group membership
    0x1D unique  -> Local Master Browser
    0x1E group   -> Browser Election Service group

Threat model: exactly like every protocol self-description (mDNS/SSDP/SNMP),
NBSTAT is a device answering FOR ITSELF -- any host can claim any name,
workgroup, or "file server" flag, and the response's own embedded MAC address
is likewise self-reported. That embedded MAC is therefore carried along ONLY
as extra evidence (`mac` in the signal dict), never used to key the result --
keying uses the caller-supplied `ip_to_mac` mapping (ARP ground truth) same
as every other collector here, exactly the M1-audit spoofability rule
recog.py's module docstring documents for OUI-alone verdicts.

Produces a per-host dict:
    {"name": str?, "workgroup": str?, "user": str?, "is_file_server": bool,
     "is_domain_controller": bool, "mac": str?, "device_type": taxonomy?,
     "make": None, "model": None, "os": None}
`make`/`model`/`os` are always None -- NetBIOS carries no such fields; kept
for shape symmetry with the other self-description hooks (upnp/bonjour/snmp)
recog.py documents. `device_type` is set ONLY via hostname_type() on the
host's own computer name (same conservative regex tier every other collector
uses) -- the file-server/domain-controller flags are deliberately NOT mapped
to a device_type: file sharing is commonly enabled on plain Windows PCs, not
just NAS/server appliances, and guessing "nas" from that flag alone would be
exactly the kind of overclaim recog.py's docstring warns against.

recog.py has no dedicated `netbios` precedence key yet (only
upnp/bonjour/snmp/dhcp per its current docstring) -- this module's fields are
new evidence for the integration/fusion layer to wire in, e.g. by feeding
`name` into the existing top-level `hostname` signal (NetBIOS's computer name
is an authoritative Windows hostname, the same tier as a DHCP-option-12 name)
and/or by extending recog._candidates with a `netbios` key at a chosen weight
-- this module intentionally does not decide that policy.
"""
from __future__ import annotations

import asyncio
import contextlib
import socket
import struct
from typing import Awaitable, Callable, Iterable

from wavr.data.deviceclass import hostname_type

NBNS_PORT = 137

# Injectable transport: given (ip, request_bytes), return the raw response
# datagram bytes. Default: one fresh UDP socket per call. Same seam/rationale
# as sources.snmp.Prober -- tests inject a canned async function, zero
# real sockets.
Prober = Callable[[str, bytes], Awaitable[bytes]]

_DEFAULT_TIMEOUT = 2.0

# Defensive cap on hosts probed in one collect() call -- see sources.snmp's
# identical rationale (bounds worst-case LAN fan-out).
_MAX_TARGETS = 512

_QTYPE_NBSTAT = 0x21
_QCLASS_IN = 0x0001

_SUFFIX_WORKSTATION = 0x00
_SUFFIX_MESSENGER = 0x03
_SUFFIX_FILE_SERVER = 0x20
_SUFFIX_DOMAIN_MASTER_BROWSER = 0x1B
_SUFFIX_DOMAIN_CONTROLLERS_GROUP = 0x1C
_SUFFIX_LOCAL_MASTER_BROWSER = 0x1D
_SUFFIX_BROWSER_ELECTION = 0x1E

_GROUP_FLAG = 0x8000


def _encode_nbname(name16: bytes) -> bytes:
    """RFC 1001 4.1 first-level encoding: each of 16 raw bytes -> two ASCII
    nibble characters 'A'-'P' (0x41 + nibble value)."""
    out = bytearray()
    for b in name16:
        out.append(0x41 + (b >> 4))
        out.append(0x41 + (b & 0x0F))
    return bytes(out)


def build_nbstat_query(transaction_id: int = 0x1337) -> bytes:
    """The standard NBSTAT ("Node Status") query datagram: NAME_TRN_ID,
    FLAGS=0 (standard query, unicast, no recursion), QDCOUNT=1, one question
    with the wildcard name "*" + 15 NUL bytes, QTYPE=NBSTAT, QCLASS=IN."""
    name16 = b"*" + b"\x00" * 15
    encoded_name = _encode_nbname(name16)
    question = (
        bytes([len(encoded_name)]) + encoded_name + b"\x00"
        + struct.pack(">HH", _QTYPE_NBSTAT, _QCLASS_IN)
    )
    header = struct.pack(">HHHHHH", transaction_id, 0x0000, 1, 0, 0, 0)
    return header + question


def _skip_name(data: bytes, pos: int) -> int:
    """Advance past one (possibly compressed) NBNS name field. We never need
    the decoded text (the query name is fixed and known, the response name is
    ignored), only the resume offset -- so this is a minimal skip, not a
    decoder. Returns len(data) (an out-of-bounds sentinel the caller must
    bound-check) if the name runs off the end of a truncated/hostile packet."""
    while pos < len(data):
        length = data[pos]
        if length == 0:
            return pos + 1
        if (length & 0xC0) == 0xC0:
            return pos + 2
        pos += 1 + length
    return len(data)


def parse_nbstat_response(data: bytes) -> dict:
    """Parse one NBSTAT response datagram into
    {"entries": [{"name": str, "suffix": int, "is_group": bool}, ...],
     "mac": str|None}.
    Never raises -- any malformed/truncated/hostile datagram yields {}."""
    try:
        if len(data) < 12:
            return {}
        _trn_id, _flags, qdcount, ancount, _nscount, _arcount = struct.unpack(">HHHHHH", data[:12])
        pos = 12
        for _ in range(qdcount):  # defensive: a response should have QDCOUNT=0
            pos = _skip_name(data, pos)
            pos += 4
            if pos > len(data):
                return {}
        if ancount < 1 or pos >= len(data):
            return {}
        pos = _skip_name(data, pos)
        if pos + 10 > len(data):
            return {}
        rtype, _rclass, _ttl, rdlength = struct.unpack(">HHIH", data[pos:pos + 10])
        pos += 10
        if rtype != _QTYPE_NBSTAT or pos + rdlength > len(data):
            return {}
        rdata = data[pos:pos + rdlength]
        if not rdata:
            return {}

        num_names = rdata[0]
        offset = 1
        entries: list[dict] = []
        for _ in range(num_names):
            if offset + 18 > len(rdata):
                break
            raw_name = rdata[offset:offset + 15]
            suffix = rdata[offset + 15]
            flags = struct.unpack(">H", rdata[offset + 16:offset + 18])[0]
            offset += 18
            name = raw_name.decode("ascii", errors="replace").rstrip(" ").rstrip("\x00")
            entries.append({
                "name": name, "suffix": suffix, "is_group": bool(flags & _GROUP_FLAG),
            })

        mac = None
        if offset + 6 <= len(rdata):
            mac_bytes = rdata[offset:offset + 6]
            if any(mac_bytes):
                mac = ":".join(f"{b:02x}" for b in mac_bytes)

        return {"entries": entries, "mac": mac}
    except Exception:
        return {}


async def _default_probe(ip: str, request: bytes, timeout: float = _DEFAULT_TIMEOUT) -> bytes:
    """Default real transport: one UDP send + recv to (ip, 137). Same
    fresh-socket-per-call pattern as sources.snmp._default_probe."""
    loop = asyncio.get_event_loop()

    def _query() -> bytes:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.settimeout(timeout)
            sock.sendto(request, (ip, NBNS_PORT))
            data, _addr = sock.recvfrom(4096)
            return data
        finally:
            sock.close()

    return await loop.run_in_executor(None, _query)


class NetBIOSCollector:
    """Active, targeted NBSTAT collector. `targets` is the caller-supplied
    list of host IPs to probe (e.g. from the ARP inventory). `prober` is the
    injectable transport (default: `_default_probe`). `ip_to_mac` optionally
    maps source IP -> MAC so results key by MAC like every other recog signal
    (unmapped hosts key by IP instead)."""

    def __init__(self, targets: Iterable[str] = (),
                 ip_to_mac: dict[str, str] | None = None,
                 prober: Prober | None = None):
        self._targets = list(targets)[:_MAX_TARGETS]
        self._ip_to_mac = {
            ip: mac.replace("-", ":").lower()
            for ip, mac in (ip_to_mac or {}).items()
        }
        self._prober = prober or _default_probe

    async def collect(self) -> dict[str, dict]:
        """Probe every target concurrently; a timed-out/unreachable/malformed
        host is silently skipped (best-effort, never raises for the caller)."""
        request = build_nbstat_query()
        out: dict[str, dict] = {}

        async def _one(ip: str) -> None:
            try:
                response = await self._prober(ip, request)
                parsed = parse_nbstat_response(response)
            except Exception:
                return
            if not parsed.get("entries"):
                return
            key = self._ip_to_mac.get(ip, ip)
            out[key] = self._to_signal(parsed)

        with contextlib.suppress(Exception):
            await asyncio.gather(*(_one(ip) for ip in self._targets))
        return out

    def _to_signal(self, parsed: dict) -> dict:
        name: str | None = None
        workgroup: str | None = None
        user: str | None = None
        is_file_server = False
        is_domain_controller = False

        for entry in parsed.get("entries", []):
            nm = entry["name"]
            if not nm:
                continue
            suffix = entry["suffix"]
            is_group = entry["is_group"]
            if suffix == _SUFFIX_WORKSTATION:
                if is_group:
                    workgroup = workgroup or nm
                else:
                    name = name or nm
            elif suffix == _SUFFIX_FILE_SERVER and not is_group:
                is_file_server = True
            elif suffix == _SUFFIX_DOMAIN_CONTROLLERS_GROUP and is_group:
                is_domain_controller = True
            elif suffix == _SUFFIX_DOMAIN_MASTER_BROWSER and not is_group:
                is_domain_controller = True
            elif suffix in (_SUFFIX_LOCAL_MASTER_BROWSER, _SUFFIX_BROWSER_ELECTION) and is_group:
                workgroup = workgroup or nm
            elif suffix == _SUFFIX_MESSENGER and not is_group and nm != name:
                user = user or nm

        signal: dict = {
            "name": name,
            "workgroup": workgroup,
            "user": user,
            "is_file_server": is_file_server,
            "is_domain_controller": is_domain_controller,
            "mac": parsed.get("mac"),
            "make": None,
            "model": None,
            "os": None,
        }
        dtype = hostname_type(name)
        if dtype:
            signal["device_type"] = dtype
        return signal
