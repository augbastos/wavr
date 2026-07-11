from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import re
import socket
import sys
from datetime import datetime, timezone
from typing import AsyncIterator, Awaitable, Callable

from wavr.events import Identity, SensingEvent

# Matches a MAC with either "-" (Windows arp) or ":" (Unix) separators.
_MAC_RE = re.compile(r"(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}")


def parse_arp_table(arp_output: str) -> set[str]:
    """Extract every MAC from raw `arp -a` output, normalized to lowercase
    colon form. Separator-agnostic (Windows uses '-', Unix ':')."""
    macs = set()
    for m in _MAC_RE.findall(arp_output):
        macs.add(m.replace("-", ":").lower())
    return macs


def _local_ipv4() -> str | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no packet sent; just picks the outbound iface
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


async def _run(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    out, _ = await proc.communicate()
    return out.decode(errors="replace")


def ping_argv(host: str, timeout_ms: int = 1000) -> tuple[str, ...]:
    """OS-appropriate 'one ping, bounded wait' argv. Windows uses ms (-n/-w);
    Linux uses whole-second -W; macOS uses -t (total seconds). Mirrors bonded.py's
    platform split so a Wavr Core running on Linux (a Pi / phone-in-proot appliance,
    not just a Windows dev box) actually pings: the old hard-coded Windows flags
    (`-n 1 -w …`) silently no-op'd on Linux, breaking the ARP sweep, the inventory
    sweep, and the gateway health check (Core reported 'critical' while healthy)."""
    if os.name == "nt":
        return ("ping", "-n", "1", "-w", str(timeout_ms), host)
    secs = max(1, (timeout_ms + 999) // 1000)  # ceil to >= 1 whole second
    if sys.platform == "darwin":
        return ("ping", "-c", "1", "-t", str(secs), host)
    return ("ping", "-c", "1", "-W", str(secs), host)  # Linux / other Unix


async def arp_scan() -> set[str]:
    """Default real scan: ping-sweep the local /24 to warm the ARP cache, then
    parse `arp -a`, returning every MAC currently on the LAN. Best-effort — a
    failed ping never raises. Windows-flavored ping flags (`-n 1 -w 200`)."""
    ip = _local_ipv4()
    if ip:
        net = ipaddress.ip_network(ip + "/24", strict=False)
        sem = asyncio.Semaphore(32)   # cap concurrent ping subprocesses (was up to 254)
        async def ping(addr: str) -> None:
            async with sem:
                with contextlib.suppress(Exception):
                    await _run(*ping_argv(addr, 200))
        await asyncio.gather(*(ping(str(h)) for h in net.hosts()))
    return parse_arp_table(await _run("arp", "-a"))


_IPV4_RE = r"\d{1,3}(?:\.\d{1,3}){3}"
_GW_LINUX_RE = re.compile(r"\bdefault\s+via\s+(" + _IPV4_RE + r")")
_GW_MAC_RE = re.compile(r"\bgateway:\s+(" + _IPV4_RE + r")")


def _valid_nonzero_ipv4(ip: str) -> bool:
    """True for a dotted IPv4 quad with every octet 0-255 and not all-zero
    (0.0.0.0 is the placeholder Windows prints for a gateway-less interface)."""
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        octets = [int(p) for p in parts]
    except ValueError:
        return False
    return all(0 <= o <= 255 for o in octets) and any(octets)


def _parse_win_gateway(output: str) -> "str | None":
    """Windows `ipconfig`: the first real IPv4 after a "Default Gateway ... :"
    label. Handles the dual-stack layout where an IPv6 gateway sits on the label
    line and the IPv4 on an indented continuation line below it. Skips empty /
    0.0.0.0 gateways and IPv6-only entries. Under-claims (returns None) rather
    than guessing on an unusual layout -- is_gateway is then honestly False,
    never a false positive."""
    lines = output.splitlines()
    for i, line in enumerate(lines):
        if "Default Gateway" not in line or ":" not in line:
            continue
        m = re.search(_IPV4_RE, line.split(":", 1)[1])
        if m and _valid_nonzero_ipv4(m.group(0)):
            return m.group(0)
        # The IPv4 may continue on indented, label-less lines under this one; an
        # IPv4 continuation starts with a digit, an IPv6 one may too ("2001:") --
        # keep scanning digit-first lines, stop at a new labelled field or an
        # IPv6-letter-first line ("fe80:"), both of which start with a non-digit.
        for cont in lines[i + 1:]:
            stripped = cont.strip()
            if not stripped or not stripped[0].isdigit():
                break
            m = re.search(_IPV4_RE, cont)
            if m and _valid_nonzero_ipv4(m.group(0)):
                return m.group(0)
    return None


def parse_default_gateway(output: str) -> "str | None":
    """Extract the first real IPv4 default-gateway address from routing-table
    text -- Linux `ip route` ("default via X"), macOS/BSD `route -n get default`
    ("gateway: X"), or Windows `ipconfig` ("Default Gateway ... : X"). Returns
    None when none is present. Pure/offline, defensive -- never raises."""
    for rx in (_GW_LINUX_RE, _GW_MAC_RE):
        for m in rx.finditer(output):
            if _valid_nonzero_ipv4(m.group(1)):
                return m.group(1)
    return _parse_win_gateway(output)


async def default_gateway() -> "str | None":
    """Best-effort REAL default-gateway IP, read from THIS host's own routing
    table via a subprocess (Windows `ipconfig`, else Linux `ip route` then
    macOS/BSD `route -n get default`). Never a guess -- contrast
    wavr.internet_monitor.guess_gateway's ".1" heuristic. LOCAL-ONLY: reads this
    host's routing state, zero network egress, touches no other host. Returns
    None when it can't be determined (honest: callers must never fabricate a
    gateway). Reuses the _run subprocess seam so it is mock-testable."""
    cmds = ([("ipconfig",)] if os.name == "nt"
            else [("ip", "route"), ("route", "-n", "get", "default")])
    for cmd in cmds:
        try:
            out = await _run(*cmd)
        except Exception:
            continue
        gw = parse_default_gateway(out)
        if gw:
            return gw
    return None


class NetworkSource:
    """House-level presence from the LAN. Emits room='casa', modality='network'.
    Presence = any known MAC seen; debounced by `grace` consecutive misses so a
    phone briefly dropping off ARP doesn't flap the state."""

    def __init__(self, known_macs: set[str],
                 scan: Callable[[], Awaitable[set[str]]] | None = None,
                 room: str = "casa", interval: float = 15.0,
                 grace: int = 2, present_confidence: float = 0.8,
                 known: dict[str, str] | None = None,
                 emit_identity: bool = False,
                 known_provider: Callable[[], dict[str, str]] | None = None,
                 detail_provider: Callable[[], set[str]] | None = None):
        self._known = {m.replace("-", ":").lower() for m in known_macs}
        # LIVE consent registry seam: when a provider is injected it is re-read at the
        # start of every scan cycle; its {mac: person} entries are UNIONED into both
        # the presence MAC set and the identity labels, so a network device REGISTERED
        # (or opted-out) via the identity registry counts (or stops counting) toward
        # presence on the next cycle -- no restart. Default None -> unchanged.
        self._known_provider = known_provider
        self._scan = scan or arp_scan
        self._room = room
        self._interval = interval
        self._grace = grace
        self._conf = present_confidence
        # Optional MAC->person label map for non-biometric "who is home". Presence
        # still runs off `self._known` (the MAC set) so nothing regresses; the label
        # is attached only when `emit_identity` is on. Default OFF -> no Identity is
        # ever created, so no PII enters the event/state/DB path.
        self._known_labels = {m.replace("-", ":").lower(): p
                              for m, p in (known or {}).items()}
        self._emit_identity = emit_identity
        # LIVE per-device opt-in seam (consent #2, IdentityStore.detailed_net_addresses):
        # re-read every cycle, same pattern as `known_provider`. A MAC in this set gets
        # its label emitted even when `emit_identity` (the GLOBAL flag) is off -- but
        # ONLY for that device, never every present device. Default None -> empty set
        # every cycle -> the emit gate below reduces to `self._emit_identity` alone,
        # i.e. byte-identical to before this seam existed.
        self._detail_provider = detail_provider
        self._missed = grace + 1  # start "absent" until first sighting

    def _current(self) -> tuple[set[str], dict[str, str]]:
        """(presence MAC set, {mac: person} labels) for THIS cycle: the frozen
        construction values unioned with the live provider result when injected."""
        if self._known_provider is None:
            return self._known, self._known_labels
        prov = {m.replace("-", ":").lower(): p
                for m, p in self._known_provider().items()}
        return self._known | set(prov), {**self._known_labels, **prov}

    async def events(self) -> AsyncIterator[SensingEvent]:
        while True:
            known, known_labels = self._current()
            if known:
                try:
                    seen = await self._scan()
                except Exception:
                    logging.warning("NetworkSource scan failed", exc_info=True)
                    seen = set()
            else:
                seen = set()
            if known & seen:
                self._missed = 0
            else:
                self._missed += 1
            present = self._missed <= self._grace
            # House-level "who is home": name currently-seen labelled devices only
            # when the gate is on -- either the GLOBAL flag, or (per-device) this
            # MAC opted into consent #2 via the live detail_provider. rssi is None —
            # an ARP scan gives no signal strength. Empty during a grace-held miss
            # (nothing seen this cycle).
            identities: tuple = ()
            if present and known_labels:
                detail_macs = self._detail_provider() if self._detail_provider is not None else set()
                identities = tuple(
                    Identity(person=known_labels[m], source="network", rssi=None)
                    for m in sorted(known_labels)
                    if m in seen and known_labels.get(m)
                    and (self._emit_identity or m in detail_macs)
                )
            yield SensingEvent(
                room=self._room, modality="network", presence=present,
                motion=0.0, breathing_bpm=None, heart_bpm=None,
                confidence=self._conf if present else 0.0,
                ts=datetime.now(timezone.utc).isoformat(),
                identities=identities,
            )
            if self._interval:
                await asyncio.sleep(self._interval)
