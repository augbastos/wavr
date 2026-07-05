"""A5.2 ARP-based device blocking -- the roadmap's SINGLE active-LAN-attack primitive.

Everywhere else Wavr only READS the network (it parses `arp -a`, it listens). Blocking
is different in kind: it SENDS crafted/gratuitous ARP replies to poison a target's ARP
entry for the gateway, cutting that one device off the LAN. That is exactly the technique
an on-LAN attacker uses, pointed INWARD at the owner's own network. It therefore ships
ONLY under the full guardrail set (enforced partly here, partly at the route):

  * Triple gate (route):   WAVR_NET_BLOCKING default OFF -> 503 . require_local CSRF .
                           per-call confirm=true -> 409 without it. Never default-on.
  * Never agent-reachable: excluded from MCP by construction (no block/arp @server.tool);
                           block/arp/spoof/poison added to the MCP sensitive-hint denylist.
  * Target denylist (here): the MAC MUST already be in the current inventory; the gateway
                           is hard-denied (blocking it = whole-LAN DoS); target IP must be
                           private + inside the host's own /24, never link-local /
                           metadata (169.254.169.254) / multicast / broadcast / our own IP.
  * Reversibility (here):  every block auto-expires (TTL); unblock sends a corrective ARP;
                           service shutdown unblocks ALL and corrects. No stranded devices.
  * Privilege honesty:     raw ARP send needs elevated raw-socket/npcap access, which breaks
                           Wavr's stdlib-no-privilege convention. The send transport is an
                           injected/optional dependency; when absent (the normal run) the
                           blocker is UNAVAILABLE and the route 503s with a clear reason --
                           NEVER a silent no-op that fakes "blocked".
  * Audit:                 every block/unblock/expiry/shutdown-restore is recorded
                           (mac/ip/ts/reason -- topology only, no occupancy/PII) and logged.
  * Injectable transport:  ZERO real packets in CI.

DELIBERATE SECURITY DEVIATION FROM THE SPEC: blocks are held IN-MEMORY ONLY and do NOT
survive a restart. Persisting an active-attack set risks a forgotten block silently
resuming poisoning after a reboot; the fail-safe posture is that any restart STOPS all
attacks (the target's ARP cache self-heals within minutes). This also avoids a wavr.db
schema change / a new PII-adjacent table. Flagged as a hardening choice, not an omission.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

_LOG = logging.getLogger(__name__)

# ARP send transport: send(target_ip, target_mac, gateway_ip, *, restore) -> None.
# restore=False -> poison (tell the target the gateway is at an unreachable MAC);
# restore=True  -> corrective (heal the target's binding back to the real gateway).
# The real transport (elevated) crafts the packets; tests inject a recorder. When None
# the blocker is UNAVAILABLE -> the route returns 503 (never a silent success).
ArpSend = Callable[..., None]

_METADATA_IPS = frozenset({"169.254.169.254", "fd00:ec2::254"})


def blocking_enabled() -> bool:
    """True only if WAVR_NET_BLOCKING is explicitly enabled. OFF by default."""
    return os.getenv("WAVR_NET_BLOCKING", "").strip().lower() in ("1", "true", "yes", "on")


def _norm_mac(mac: str) -> str:
    return (mac or "").strip().replace("-", ":").replace(".", ":").lower()


def _same_slash24(ip: str, local_ip: str) -> bool:
    try:
        net = ipaddress.ip_network(local_ip + "/24", strict=False)
        return ipaddress.ip_address(ip) in net
    except ValueError:
        return False


def validate_target(mac: str, *, inventory: list, gateway, local_ip: str,
                    gateway_ip: str | None = None) -> tuple:
    """Resolve+validate a block target. Returns (norm_mac, ip) or raises ValueError.

    `inventory` is the current list of Device records (each has .mac/.ip/.is_gateway).
    `gateway` is the Device flagged is_gateway (or None). `gateway_ip` is an
    INDEPENDENTLY derived gateway IP (e.g. the '.1' heuristic, read WITHOUT relying on
    the inventory flag) -- it is folded into the gateway deny-set so the single most
    load-bearing guard does not rest solely on a best-effort detector. `local_ip` is
    THIS host's LAN IP. Every rejection is a ValueError -> the route maps it to 400.
    This is the target denylist that stops the primitive becoming an arbitrary-packet /
    attack-a-neighbor tool (AP1/AP2 of the red-team).

    A5 hardening -- FAIL CLOSED: both the host's own LAN IP AND a positively identified
    gateway are REQUIRED. When either is unknown (best-effort detection failed) we
    refuse the block rather than silently dropping the self-host / same-/24 / gateway
    guards, which would degrade the denylist to merely 'private + in inventory' and let
    the real gateway (always private, in-/24, present in the ARP inventory) through =
    whole-LAN DoS."""
    m = _norm_mac(mac)
    if not m:
        raise ValueError("mac is required")
    lip = (local_ip or "").strip()
    if not lip:
        # Fail closed: without our own LAN IP the self-host and same-/24 guards cannot
        # be enforced. Refuse rather than widen the allowed set to any inventory MAC.
        raise ValueError("cannot determine this host's LAN IP; refusing to block for safety")
    if gateway is None:
        # Fail closed: the gateway hard-deny (blocking it = whole-LAN DoS) must not
        # depend on a best-effort detector silently degrading to 'no gateway found'.
        raise ValueError("gateway not positively identified this scan; refusing to block for safety")
    # Gateway deny-set: the flagged gateway's IP PLUS any independently derived gateway
    # IP, so the catastrophic-action guard doesn't rest on the inventory flag alone.
    gw_mac = _norm_mac(getattr(gateway, "mac", ""))
    denied_gw_ips = {ip for ip in (
        (getattr(gateway, "ip", "") or "").strip(),
        (gateway_ip or "").strip(),
    ) if ip}
    if gw_mac and gw_mac == m:
        raise ValueError("refusing to block the gateway (would DoS the whole LAN)")
    target = next((d for d in inventory if _norm_mac(getattr(d, "mac", "")) == m), None)
    if target is None:
        raise ValueError("target MAC is not in the current inventory")
    if getattr(target, "is_gateway", False):
        raise ValueError("refusing to block the gateway (would DoS the whole LAN)")
    ip = (getattr(target, "ip", "") or "").strip()
    if not ip:
        raise ValueError("target has no resolved IP")
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        raise ValueError("target IP is not a valid address")
    # Canonicalize before the cloud-metadata check so a differently-cased / zero-padded
    # textual form of the same address (e.g. 'FD00:EC2::254') is still recognized.
    if addr.compressed in _METADATA_IPS or ip in _METADATA_IPS:
        raise ValueError("refusing cloud-metadata address")
    if not addr.is_private or addr.is_loopback or addr.is_link_local \
            or addr.is_multicast or addr.is_reserved or addr.is_unspecified:
        raise ValueError("target IP must be a private LAN address")
    if ip == lip:
        raise ValueError("refusing to block this host")
    if ip in denied_gw_ips:
        raise ValueError("refusing to block the gateway (would DoS the whole LAN)")
    if not _same_slash24(ip, lip):
        raise ValueError("target IP is outside this host's /24")
    return m, ip


@dataclass
class _Block:
    mac: str
    ip: str
    gateway_ip: str
    expires_at: float


@dataclass
class _Event:
    ts: str
    kind: str          # block | unblock | reassert | expire | restore | error
    mac: str
    ip: str
    detail: str = ""


class ArpBlocker:
    """Owns the active-block set, the single re-assert task, and the audit ring.

    `send` is the ARP transport (None -> unavailable -> route 503). All timing is via
    `clock`/`sleep` seams so tests are deterministic and packet-free."""

    def __init__(self, send: Optional[ArpSend] = None, *,
                 reassert_interval: float = 10.0, ttl: float = 300.0,
                 max_blocks: int = 16, max_events: int = 200,
                 clock: Callable[[], float] = time.monotonic,
                 sleep=asyncio.sleep):
        self._send = send
        self._interval = max(1.0, float(reassert_interval))
        self._ttl = max(1.0, float(ttl))
        self._max_blocks = max(1, int(max_blocks))
        self._max_events = max(1, int(max_events))
        self._clock = clock
        self._sleep = sleep
        self._blocks: dict = {}
        self._events: list = []
        self._task: Optional[asyncio.Task] = None

    def available(self) -> bool:
        """True only when a real ARP-send transport is wired. Unavailable is the
        default, honest state (elevated raw sockets / npcap not present)."""
        return self._send is not None

    def _record(self, kind: str, mac: str, ip: str, detail: str = "") -> None:
        ev = _Event(ts=datetime.now(timezone.utc).isoformat(), kind=kind,
                    mac=mac, ip=ip, detail=detail)
        self._events.append(ev)
        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events:]
        # Topology only (mac/ip already in /api/inventory) -- never occupancy/PII.
        _LOG.info("arp_block %s mac=%s ip=%s %s", kind, mac, ip, detail)

    def recent_events(self, limit: int = 50) -> list:
        return [asdict(e) for e in self._events[-max(1, limit):]]

    def list_blocks(self) -> list:
        now = self._clock()
        return [{"mac": b.mac, "ip": b.ip,
                 "expires_in": round(max(0.0, b.expires_at - now), 1)}
                for b in self._blocks.values()]

    def _emit(self, ip: str, mac: str, gateway_ip: str, *, restore: bool) -> None:
        if self._send is None:
            return
        try:
            self._send(ip, mac, gateway_ip, restore=restore)
        except TypeError:
            self._send(ip, mac, gateway_ip)   # positional-only seam fallback

    async def block(self, mac: str, *, inventory: list, gateway, local_ip: str,
                    gateway_ip: str | None = None) -> dict:
        m, ip = validate_target(mac, inventory=inventory, gateway=gateway,
                                local_ip=local_ip, gateway_ip=gateway_ip)
        gw_ip = (getattr(gateway, "ip", "") or "").strip() if gateway is not None else ""
        if m not in self._blocks and len(self._blocks) >= self._max_blocks:
            raise ValueError("too many active blocks")
        now = self._clock()
        self._blocks[m] = _Block(mac=m, ip=ip, gateway_ip=gw_ip, expires_at=now + self._ttl)
        self._emit(ip, m, gw_ip, restore=False)
        self._record("block", m, ip, "ttl=%ds" % int(self._ttl))
        self._ensure_task()
        return {"blocked": True, "mac": m, "ip": ip, "ttl": int(self._ttl),
                "note": "best-effort ARP disruption; effect not guaranteed on hardened targets"}

    async def unblock(self, mac: str, *, inventory: list, gateway, local_ip: str) -> dict:
        m = _norm_mac(mac)
        b = self._blocks.pop(m, None)
        if b is None:
            return {"blocked": False, "mac": m, "note": "was not blocked"}
        self._emit(b.ip, m, b.gateway_ip, restore=True)   # corrective ARP to heal
        self._record("unblock", m, b.ip, "corrective ARP sent")
        return {"blocked": False, "mac": m, "ip": b.ip}

    def _ensure_task(self) -> None:
        if self._task is None or self._task.done():
            try:
                self._task = asyncio.create_task(self._reassert_loop())
            except RuntimeError:
                self._task = None   # no running loop (pure unit test) -- fine

    def _tick(self) -> None:
        """One re-assert/expiry cycle (synchronous so TTL is deterministically
        testable). Expires past-TTL blocks (corrective ARP) and re-asserts the rest."""
        now = self._clock()
        for m, b in list(self._blocks.items()):
            if now >= b.expires_at:
                self._blocks.pop(m, None)
                self._emit(b.ip, m, b.gateway_ip, restore=True)
                self._record("expire", m, b.ip, "TTL reached; corrective ARP sent")
            else:
                self._emit(b.ip, m, b.gateway_ip, restore=False)
                self._record("reassert", m, b.ip)

    async def _reassert_loop(self) -> None:
        """The SINGLE owned loop: re-assert live blocks, auto-expire past-TTL ones.
        Fixed conservative interval; never spawns per-request loops (AP5)."""
        while self._blocks:
            try:
                self._tick()
            except Exception:
                _LOG.warning("arp reassert cycle failed", exc_info=True)
            await self._sleep(self._interval)

    async def stop(self) -> None:
        """Shutdown: cancel the loop and CLEANLY undo every active block (corrective
        ARP) so the LAN is never left poisoned after Wavr exits (AP4)."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        for m, b in list(self._blocks.items()):
            self._blocks.pop(m, None)
            self._emit(b.ip, m, b.gateway_ip, restore=True)
            self._record("restore", m, b.ip, "shutdown; corrective ARP sent")
