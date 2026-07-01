from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import re
import socket
from datetime import datetime, timezone
from typing import AsyncIterator, Awaitable, Callable

from wavr.events import SensingEvent

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


async def arp_scan() -> set[str]:
    """Default real scan: ping-sweep the local /24 to warm the ARP cache, then
    parse `arp -a`, returning every MAC currently on the LAN. Best-effort — a
    failed ping never raises. Windows-flavored ping flags (`-n 1 -w 200`)."""
    ip = _local_ipv4()
    if ip:
        net = ipaddress.ip_network(ip + "/24", strict=False)
        async def ping(addr: str) -> None:
            with contextlib.suppress(Exception):
                await _run("ping", "-n", "1", "-w", "200", addr)
        await asyncio.gather(*(ping(str(h)) for h in net.hosts()))
    return parse_arp_table(await _run("arp", "-a"))


class NetworkSource:
    """House-level presence from the LAN. Emits room='casa', modality='network'.
    Presence = any known MAC seen; debounced by `grace` consecutive misses so a
    phone briefly dropping off ARP doesn't flap the state."""

    def __init__(self, known_macs: set[str],
                 scan: Callable[[], Awaitable[set[str]]] | None = None,
                 room: str = "casa", interval: float = 15.0,
                 grace: int = 2, present_confidence: float = 0.8):
        self._known = {m.replace("-", ":").lower() for m in known_macs}
        self._scan = scan or arp_scan
        self._room = room
        self._interval = interval
        self._grace = grace
        self._conf = present_confidence
        self._missed = grace + 1  # start "absent" until first sighting

    async def events(self) -> AsyncIterator[SensingEvent]:
        while True:
            try:
                seen = await self._scan()
            except Exception:
                seen = set()
            if self._known & seen:
                self._missed = 0
            else:
                self._missed += 1
            present = self._missed <= self._grace
            yield SensingEvent(
                room=self._room, modality="network", presence=present,
                motion=0.0, breathing_bpm=None, heart_bpm=None,
                confidence=self._conf if present else 0.0,
                ts=datetime.now(timezone.utc).isoformat(),
            )
            if self._interval:
                await asyncio.sleep(self._interval)
