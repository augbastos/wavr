"""Wavr SNMP collector -- best local signal for infra (switch/printer/UPS/router/NAS).

Unlike mDNS/SSDP (which are pure passive multicast LISTENERS -- Wavr sends
nothing), SNMP has no multicast discovery mechanism to eavesdrop on. This
module is therefore an ACTIVE, TARGETED unicast probe: one SNMPv1 GET-Request
per caller-supplied target IP (e.g. hosts the ARP inventory already knows
about -- this module never invents its own target list/subnet sweep), asking
for exactly four well-known MIB-II scalars:
    sysDescr    1.3.6.1.2.1.1.1.0
    sysObjectID 1.3.6.1.2.1.1.2.0
    sysName     1.3.6.1.2.1.1.5.0
    sysServices 1.3.6.1.2.1.1.7.0
READ-ONLY by construction -- this module only ever encodes a GET-Request; it
has no SET-Request encoder at all, so there is no code path that could write
to a device even if misconfigured. The community string defaults to the
conventional read-only "public" (per the task spec) and is caller-configurable
(the integration layer is expected to wire it from a
`WAVR_NET_SNMP_COMMUNITY` config value -- never hardcode a write community).

OPT-IN, default OFF -- like every Wavr source, this module does not read the
environment itself; the integration step wires a `WAVR_NET_SNMP` flag gating
whether `SNMPCollector` is ever constructed, and should treat a non-empty
target list as a SEPARATE precondition (no flag => no targets => no probes).
Because this is an active per-host probe (real LAN traffic to hosts that may
not expect it), it is a strictly more active technique than passive mDNS/SSDP
listening -- see the netutils.WAVR_NET_PORTSCAN docstring for the identical
"active scanning has real LAN impact" rationale Wavr already applies there.

Minimal stdlib BER/ASN.1 encoder+decoder (no `pysnmp` dependency) scoped to
exactly what an SNMPv1 GET-Request/GetResponse needs -- same "no third-party
protocol library" ethos as sources.mdns's hand-rolled DNS parser. Parsing is
fully defensive: any malformed/truncated/hostile response yields {} rather
than raising (a rogue/misbehaving SNMP agent must never crash the collector).

Produces a per-host dict shaped for wavr.recog's `snmp` self-description hook:
    {"device_type": taxonomy?, "make": str?, "model": str?, "os": str?}
(recog.py only ever reads those four keys -- capped at "medium" confidence
ALONE, same spoofability threat model as every protocol self-description
signal, see recog.py's module docstring). `sys_descr`/`sys_name`/
`sys_object_id`/`sys_services` ride along as extra evidence for a future
richer inventory/explain view. `model` is deliberately left unset: sysDescr is
a free-form string with no reliable separate model field, and inventing one
would violate the "never invented" rule recog.py documents for model/os.

The sysObjectID -> vendor table below is a SMALL, individually-verified
subset of the PUBLIC IANA Private Enterprise Numbers registry
(iana.org/assignments/enterprise-numbers) -- not copied from any third-party
recognition database. Enterprise 2021 (`ucdavis`) is the net-snmp/UCD-SNMP
MIB's own registration, a reliable proxy for "this agent is net-snmp, which
only ships on Unix/Linux" -- used as an OS hint, not a vendor.
"""
from __future__ import annotations

import asyncio
import contextlib
import socket
from typing import Awaitable, Callable, Iterable

from wavr.data.deviceclass import hostname_type

SNMP_PORT = 161

# Injectable transport: given (ip, request_bytes), return the raw response
# datagram bytes. Default: one fresh UDP socket per call (send + recv), same
# executor-thread pattern as sources.ssdp's `_default_fetch` (portable across
# platforms/Python versions, unlike relying on ProactorEventLoop UDP support
# on Windows). Tests inject a canned async function -- zero real sockets.
Prober = Callable[[str, bytes], Awaitable[bytes]]

_DEFAULT_TIMEOUT = 2.0

# Defensive cap on how many hosts one collect() call will probe -- bounds
# worst-case LAN fan-out/latency if a caller accidentally hands this a huge
# target list (e.g. a full /16 instead of the ARP-known /24).
_MAX_TARGETS = 512

_OID_SYS_DESCR = "1.3.6.1.2.1.1.1.0"
_OID_SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
_OID_SYS_NAME = "1.3.6.1.2.1.1.5.0"
_OID_SYS_SERVICES = "1.3.6.1.2.1.1.7.0"

_UCDAVIS_PREFIX = "1.3.6.1.4.1.2021."

# sysObjectID enterprise-number (the component right after 1.3.6.1.4.1.) ->
# vendor name. Individually verified against the public IANA PEN registry --
# see module docstring. Intentionally small; extend only with verified
# entries, never guessed ones (a wrong "medium"-confidence make is a real
# inventory bug even though recog caps it below "high" on its own).
_ENTERPRISE_VENDOR: dict[int, str] = {
    9: "Cisco",
    11: "Hewlett-Packard",
    63: "Apple",
    171: "D-Link",
    311: "Microsoft",
    890: "Zyxel",
    1065: "Canon",
    1248: "Epson",
}

# sysDescr substrings commonly self-identifying the OS/platform (free-form
# per RFC1213 -- these are widely-observed conventions, not a proprietary
# catalog). Order does not matter -- the needles are mutually exclusive.
_OS_HINTS: tuple[tuple[str, str], ...] = (
    ("routeros", "RouterOS"), ("linux", "Linux"), ("windows", "Windows"),
    ("darwin", "Darwin"), ("freebsd", "FreeBSD"), ("vxworks", "VxWorks"),
)


# --- minimal BER/ASN.1 (SNMPv1 GET-Request/GetResponse only) ---------------

def _ber_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    out = bytearray()
    while n:
        out.insert(0, n & 0xFF)
        n >>= 8
    return bytes([0x80 | len(out)]) + bytes(out)


def _tlv(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + _ber_len(len(content)) + content


def _encode_int(value: int) -> bytes:
    if value == 0:
        content = b"\x00"
    else:
        b = bytearray()
        v = value
        while v:
            b.insert(0, v & 0xFF)
            v >>= 8
        if b[0] & 0x80:
            b.insert(0, 0x00)
        content = bytes(b)
    return _tlv(0x02, content)


def _encode_oid(oid: str) -> bytes:
    parts = [int(p) for p in oid.strip(".").split(".")]
    body = bytearray([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        if p == 0:
            body.append(0)
            continue
        chunk = []
        v = p
        while v:
            chunk.insert(0, v & 0x7F)
            v >>= 7
        for i in range(len(chunk) - 1):
            chunk[i] |= 0x80
        body.extend(chunk)
    return _tlv(0x06, bytes(body))


def _decode_oid(data: bytes) -> str:
    if not data:
        return ""
    first = data[0]
    parts = [first // 40, first % 40]
    val = 0
    for b in data[1:]:
        val = (val << 7) | (b & 0x7F)
        if not (b & 0x80):
            parts.append(val)
            val = 0
    return ".".join(str(p) for p in parts)


def _build_get_request(community: str, oids: list[str], request_id: int = 1) -> bytes:
    """SNMPv1 (version=0) GET-Request for the given OIDs, each varbind's
    value a placeholder NULL (0x05 0x00) as the protocol requires for a GET."""
    varbinds = b"".join(_tlv(0x30, _encode_oid(oid) + _tlv(0x05, b"")) for oid in oids)
    varbind_list = _tlv(0x30, varbinds)
    pdu_body = _encode_int(request_id) + _encode_int(0) + _encode_int(0) + varbind_list
    pdu = _tlv(0xA0, pdu_body)  # GetRequest-PDU
    message_body = _encode_int(0) + _tlv(0x04, community.encode("utf-8", "replace")) + pdu
    return _tlv(0x30, message_body)


def _read_tlv(data: bytes, pos: int) -> tuple[int, bytes, int]:
    tag = data[pos]
    pos += 1
    length = data[pos]
    pos += 1
    if length & 0x80:
        num_bytes = length & 0x7F
        if num_bytes == 0 or pos + num_bytes > len(data):
            raise ValueError("truncated BER length")
        length = int.from_bytes(data[pos:pos + num_bytes], "big")
        pos += num_bytes
    if pos + length > len(data):
        raise ValueError("truncated BER content")
    content = data[pos:pos + length]
    pos += length
    return tag, content, pos


def _decode_value(tag: int, content: bytes) -> object:
    if tag == 0x02:  # INTEGER
        return int.from_bytes(content, "big", signed=True) if content else 0
    if tag == 0x04:  # OCTET STRING
        return content.decode("utf-8", errors="replace")
    if tag == 0x06:  # OBJECT IDENTIFIER
        return _decode_oid(content)
    if tag == 0x05:  # NULL
        return None
    return None  # NoSuchObject/NoSuchInstance/EndOfMibView (SNMPv2 context tags) or unknown


def parse_snmp_response(data: bytes) -> dict[str, object]:
    """Parse one SNMPv1 GetResponse datagram into {oid_str: decoded_value}.
    Never raises -- any malformed/truncated/hostile response yields {}."""
    try:
        _tag, content, _ = _read_tlv(data, 0)
        pos = 0
        _vtag, _vcontent, pos = _read_tlv(content, pos)   # version
        _ctag, _ccontent, pos = _read_tlv(content, pos)   # community
        ptag, pcontent, _ = _read_tlv(content, pos)       # PDU
        if ptag != 0xA2:  # GetResponse-PDU only
            return {}
        ppos = 0
        _, _, ppos = _read_tlv(pcontent, ppos)  # request-id
        _, _, ppos = _read_tlv(pcontent, ppos)  # error-status
        _, _, ppos = _read_tlv(pcontent, ppos)  # error-index
        _vbtag, vblist, _ = _read_tlv(pcontent, ppos)

        result: dict[str, object] = {}
        vpos = 0
        while vpos < len(vblist):
            _seqtag, seqcontent, vpos = _read_tlv(vblist, vpos)
            spos = 0
            _oidtag, oidcontent, spos = _read_tlv(seqcontent, spos)
            valtag, valcontent, _ = _read_tlv(seqcontent, spos)
            result[_decode_oid(oidcontent)] = _decode_value(valtag, valcontent)
        return result
    except Exception:
        return {}


async def _default_probe(ip: str, request: bytes, timeout: float = _DEFAULT_TIMEOUT) -> bytes:
    """Default real transport: one UDP send + recv to (ip, 161). A fresh
    socket per call keeps this trivially safe to run concurrently across
    many targets (see SNMPCollector.collect)."""
    loop = asyncio.get_event_loop()

    def _query() -> bytes:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.settimeout(timeout)
            sock.sendto(request, (ip, SNMP_PORT))
            data, _addr = sock.recvfrom(4096)
            return data
        finally:
            sock.close()

    return await loop.run_in_executor(None, _query)


def _os_from_descr(sys_descr: str | None, sys_object_id: str | None) -> str | None:
    if sys_descr:
        low = sys_descr.lower()
        for needle, name in _OS_HINTS:
            if needle in low:
                return name
    if sys_object_id and sys_object_id.startswith(_UCDAVIS_PREFIX):
        return "Linux"  # net-snmp/UCD-SNMP MIB registration -- Unix/Linux-only agent
    return None


def _vendor_from_object_id(sys_object_id: str | None) -> str | None:
    prefix = "1.3.6.1.4.1."
    if not sys_object_id or not sys_object_id.startswith(prefix):
        return None
    try:
        enterprise = int(sys_object_id[len(prefix):].split(".")[0])
    except (ValueError, IndexError):
        return None
    return _ENTERPRISE_VENDOR.get(enterprise)


class SNMPCollector:
    """Active, targeted SNMPv1 GET collector. `targets` is the caller-supplied
    list of host IPs to probe (e.g. from the ARP inventory -- this module
    never invents its own subnet sweep). `prober` is the injectable transport
    (default: `_default_probe`). `ip_to_mac` optionally maps source IP -> MAC
    so results key by MAC like every other recog signal (unmapped hosts key
    by IP instead, same convention as sources.mdns/sources.ssdp)."""

    def __init__(self, targets: Iterable[str] = (), community: str = "public",
                 ip_to_mac: dict[str, str] | None = None,
                 prober: Prober | None = None):
        self._targets = list(targets)[:_MAX_TARGETS]
        self._community = community
        self._ip_to_mac = {
            ip: mac.replace("-", ":").lower()
            for ip, mac in (ip_to_mac or {}).items()
        }
        self._prober = prober or _default_probe

    async def collect(self) -> dict[str, dict]:
        """Probe every target concurrently; a timed-out/unreachable/malformed
        host is silently skipped (best-effort, never raises for the caller)."""
        request = _build_get_request(
            self._community,
            [_OID_SYS_DESCR, _OID_SYS_OBJECT_ID, _OID_SYS_NAME, _OID_SYS_SERVICES],
        )
        out: dict[str, dict] = {}

        async def _one(ip: str) -> None:
            try:
                response = await self._prober(ip, request)
                varbinds = parse_snmp_response(response)
            except Exception:
                return
            if not varbinds:
                return
            key = self._ip_to_mac.get(ip, ip)
            out[key] = self._to_signal(varbinds)

        with contextlib.suppress(Exception):
            await asyncio.gather(*(_one(ip) for ip in self._targets))
        return out

    def _to_signal(self, varbinds: dict[str, object]) -> dict:
        sys_descr = varbinds.get(_OID_SYS_DESCR)
        sys_name = varbinds.get(_OID_SYS_NAME)
        sys_object_id = varbinds.get(_OID_SYS_OBJECT_ID)
        sys_services = varbinds.get(_OID_SYS_SERVICES)
        sys_descr = sys_descr if isinstance(sys_descr, str) else None
        sys_name = sys_name if isinstance(sys_name, str) else None
        sys_object_id = sys_object_id if isinstance(sys_object_id, str) else None
        sys_services = sys_services if isinstance(sys_services, int) else None

        signal: dict = {
            "sys_descr": sys_descr,
            "sys_name": sys_name,
            "sys_object_id": sys_object_id,
            "sys_services": sys_services,
            "make": _vendor_from_object_id(sys_object_id),
            "model": None,
            "os": _os_from_descr(sys_descr, sys_object_id),
        }
        dtype = hostname_type(sys_name) or hostname_type(sys_descr)
        if dtype:
            signal["device_type"] = dtype
        return signal
