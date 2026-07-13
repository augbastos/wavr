"""Wavr Net service -- makes the defensive LAN inventory a LIVE, running thing.

Wraps wavr.netinventory.scan_inventory in a periodic asyncio task: it re-scans
the LAN on a config-driven interval, keeps the latest resolved `list[Device]` in
memory, and raises an edge-triggered rogue-device alert the FIRST time a MAC not
on the known allowlist appears (a rescan never re-alerts the same MAC).

DEFENSIVE ONLY (ADR-0004): the scan reads the ARP cache of the LAN this host is
already on -- nothing else. Risky-port awareness stays OFF by default; it only
runs when WAVR_NET_PORTSCAN is explicitly enabled (wavr.netutils gate), and even
then it is connect-only, report-only. OPERATOR WARNING: when on, it connect-scans
EVERY host the ARP inventory discovers on the /24, which on a shared/guest
subnet may include hosts the operator doesn't own -- see wavr.netutils
port_scan_enabled()'s docstring. WAVR_NET_PORTSCAN_SCOPE=known narrows that pass
to the known-MAC allowlist only (port_scan_known_only_enabled()).

Everything is in-memory (bounded alert ring) and the scan transport is injectable
(same seam as wavr.sources.network), so the whole service is mock-tested with
zero real network / zero hardware.

Passive protocol collectors (mDNS/SSDP, defensive-inventory collectors): OPT-IN,
default OFF (`mdns_enabled`/`ssdp_enabled`, wired from `WAVR_NET_MDNS`/
`WAVR_NET_SSDP` in config.py -- this module itself never reads the
environment, same rule as every collector). When on, each scan cycle also
runs the collectors' own bounded listen window (`collect_duration` seconds,
both collectors concurrently) and folds whatever they heard into the SAME
recog re-fuse pass as the port scan, keyed to this cycle's ARP-resolved
MACs via IP -- a signal for a host not already in this cycle's ARP
inventory has nothing to attach to and is dropped, never invented into a
phantom device. `mdns`/`ssdp` are the injectable collector instances (tests
hand in a fake with an async `collect(duration)`; production lazily builds
the real `wavr.sources.mdns.MDNSCollector`/`wavr.sources.ssdp.SSDPCollector`
only once actually enabled, so the multicast sockets are never opened
otherwise). A collector raising is caught and logged, same tolerance as
`on_rogue`/`device_meta` -- one collector's failure must never break scanning.

DHCP fingerprint collector (defensive-inventory #6): same passive-listener shape as
mDNS/SSDP -- `dhcp_fp_enabled`/`dhcp_fp` -- except its output is already
keyed by MAC (parsed straight from the DHCP packet's own `chaddr`, see
`wavr.sources.dhcp_fp`'s docstring for why), so it needs no IP->MAC mapping
step.

NetBIOS/SNMP collectors (defensive-inventory #5/#8): unlike mDNS/SSDP/DHCP-fp, these
are ACTIVE, TARGETED unicast probes, not passive listeners -- there is no
"the real collector" singleton to lazily build once; a NEW
`NetBIOSCollector`/`SNMPCollector` is built EVERY scan cycle, targeted at
exactly this cycle's ARP-resolved host IPs (never a subnet sweep of their
own -- same rule as the opt-in port-scan pass). `netbios_enabled`/
`snmp_enabled` gate whether they run at all (default OFF, unlike the
constructor's OWN `netbios_scope_known_only`/`snmp_scope_known_only`
parameter defaults below, `config.py`'s wiring defaults the SCOPE to
known-only -- audit fix #4: an ACTIVE unicast probe is more intrusive than
passive listening, so widening to every ARP-discovered host on a
shared/guest subnet requires an explicit `SCOPE=all` opt-in). `snmp_community`
is passed straight to `SNMPCollector` (read-only by construction -- it has no
SET-Request encoder) and is NEVER logged, including on collector failure (the
warning log below names only the collector, never its arguments) -- a
NON-default community configured alongside a widened (non-known-only) scope
additionally logs one construction-time warning (audit fix #4: it would be
sent in SNMPv1 cleartext to hosts outside the known-MAC allowlist).
`netbios_prober`/`snmp_prober` are the injectable low-level transports (same
seam as `port_probe` below) -- tests inject a canned async function, zero
real sockets; production leaves them None so each collector opens its own
real UDP socket per target.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Awaitable, Callable

from wavr.alert_severity import SEVERITY_INFO, SEVERITY_NOTE
from wavr.data.oui import is_locally_administered
from wavr.netinventory import Device, apply_recognition, scan_inventory
from wavr.netutils import (annotate_ports, ping_host, port_scan_enabled,
                           port_scan_known_only_enabled)

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class RogueAlert:
    """One rogue-device sighting: an unknown MAC that appeared on the LAN. Kept
    in-memory only. `ts` is ISO-8601 UTC of first sighting. `device_type` /
    `type_confidence` carry the recog fusion verdict (taxonomy value +
    high/medium/low) so alert rows can render the same identity as inventory.
    `severity` rides wavr.alert_severity's ONE ladder (info/note/watch/alert/
    critical) so a benign guest phone and a real intrusion never render alike:
    a randomized (locally-administered) MAC -- the classic hopped-on guest
    phone -- is `info`; any other new unknown device is `note`. It NEVER
    reaches alert/critical here: those top tiers are reserved for the
    network-level rogue_dhcp / gateway_identity events (honesty -- a
    per-device sighting must not overstate its own severity)."""
    ts: str
    mac: str
    vendor: str
    ip: str | None = None
    device_type: str = "unknown"
    hostname: str | None = None
    type_confidence: str = "low"
    severity: str = SEVERITY_NOTE

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "mac": self.mac,
            "vendor": self.vendor,
            "ip": self.ip,
            "device_type": self.device_type,
            "hostname": self.hostname,
            "type_confidence": self.type_confidence,
            "severity": self.severity,
        }


def _norm_macs(macs) -> set[str]:
    return {m.strip().replace("-", ":").lower() for m in (macs or ()) if m.strip()}


class NetworkInventoryService:
    """Periodically scans the LAN, holds the latest inventory, and logs
    edge-triggered rogue-device alerts.

    `scan` is the injectable ARP-text transport handed straight to
    scan_inventory (default: the real local ARP scan) -- inject a coroutine
    returning canned `arp -a` text to run without a network. `port_scan`
    overrides the WAVR_NET_PORTSCAN env gate (tests); leave None for the
    default (OFF) behaviour. `port_scan_known_only` overrides
    WAVR_NET_PORTSCAN_SCOPE=known (tests); leave None for the default (OFF --
    the port pass covers every discovered host, unchanged behaviour); when on,
    only devices already on the known-MAC allowlist are connect-scanned (L3
    audit fix: bounds the pass's footprint on a shared/guest subnet).
    `on_rogue` is an OPTIONAL injectable callback
    `(RogueAlert) -> None` fired at the same edge-triggered moment a new
    rogue MAC is recorded (once per MAC, same rule as the alert log) -- used
    by the opt-in ntfy notifier. Exceptions from it are caught and logged,
    never propagated (a broken callback must not break scanning).

    `device_meta` is an OPTIONAL wavr.device_meta.DeviceMeta -- when given,
    every device in a scan calls `device_meta.seen(mac)` (Feature A: persisted
    first-seen/last-seen). A persistence failure (e.g. disk issue) is caught
    and logged, same tolerance as `on_rogue` -- it must never break scanning.

    `known_provider` is an OPTIONAL callable returning the CURRENT set of
    runtime-known MACs (wavr.known_store.KnownStore.known_macs) -- read
    FRESH at the top of every scan (`_dynamic_known`) and unioned with the
    static `known_macs` allowlist, so a runtime mark-known
    (POST /api/inventory/known) takes effect on the very next scan with no
    restart, exactly like `_type_pins`/`_ha_signals`. Deliberately never
    baked into a static set at construction time. Tolerant: a provider
    failure falls back to the static allowlist only, same rule as every
    other optional collector here.
    """

    def __init__(self, known_macs=None,
                 scan: Callable[[], Awaitable[str]] | None = None,
                 interval: float = 30.0, max_alerts: int = 100,
                 port_scan: bool | None = None,
                 port_scan_known_only: bool | None = None,
                 on_rogue: Callable[[RogueAlert], None] | None = None,
                 device_meta=None, port_probe=None,
                 mdns_enabled: bool = False, ssdp_enabled: bool = False,
                 ssdp_location_enabled: bool = False,
                 mdns=None, ssdp=None, collect_duration: float = 3.0,
                 netbios_enabled: bool = False, netbios_scope_known_only: bool = False,
                 snmp_enabled: bool = False, snmp_community: str = "public",
                 snmp_scope_known_only: bool = False,
                 netbios_prober=None, snmp_prober=None,
                 dhcp_fp_enabled: bool = False, dhcp_fp=None,
                 hostname_resolve_enabled: bool = False, hostname_resolver=None,
                 latency_enabled: bool = False, ping=None,
                 gateway_detect_enabled: bool = False, gateway_detector=None,
                 gateway_monitor=None, ha_store=None, known_provider=None,
                 sensing_allowed=None):
        self._known = _norm_macs(known_macs)
        # Runtime known-MAC provider (wavr.known_store.KnownStore.known_macs) --
        # see the class docstring; None -> unchanged, static-allowlist-only
        # behaviour (today's default).
        self._known_provider = known_provider
        self._scan = scan
        self._interval = interval
        self._max_alerts = max_alerts
        self._port_scan = port_scan
        # L3 audit fix: optionally scope the opt-in port pass to the known-MAC
        # allowlist so a shared/guest-subnet neighbor is never connect-scanned.
        # None (default) -> read WAVR_NET_PORTSCAN_SCOPE (off unless "known").
        self._port_scan_known_only = port_scan_known_only
        self._port_probe = port_probe   # injectable TCP-connect probe (tests)
        self._on_rogue = on_rogue
        self._device_meta = device_meta
        # Passive collectors (opt-in, default OFF -- see module docstring).
        # `mdns`/`ssdp` injected explicitly (tests) win over the enabled flag;
        # otherwise the real collector is built lazily, only once actually used.
        self._mdns_enabled = mdns_enabled
        self._ssdp_enabled = ssdp_enabled
        self._ssdp_location_enabled = ssdp_location_enabled
        self._mdns = mdns
        self._ssdp = ssdp
        self._collect_duration = collect_duration
        # DHCP fingerprint (passive, MAC-keyed -- see module docstring).
        self._dhcp_fp_enabled = dhcp_fp_enabled
        self._dhcp_fp = dhcp_fp
        # Reverse-DNS hostname resolver (gateway-anchored PTR) -- opt-in, default
        # OFF. Feeds the hostnames= build parameter so the recog hostname
        # classifier fires on real device names. Injected resolver wins (tests);
        # otherwise the real one is built lazily, only once actually enabled.
        self._hostname_resolve_enabled = hostname_resolve_enabled
        self._hostname_resolver = hostname_resolver
        # Per-device latency (opt-in, default OFF) + gateway-identity flag.
        # `ping` is the injectable latency probe (tests); `gateway_detector`
        # the injectable default-gateway detector. Latency actively
        # TCP-connects each host so it is gated like the port pass; gateway
        # detection only reads THIS host's routing table (zero egress) so
        # app.py leaves it on unconditionally.
        self._latency_enabled = latency_enabled
        self._ping = ping
        self._gateway_detect_enabled = gateway_detect_enabled
        self._gateway_detector = gateway_detector
        # Gateway-MAC-identity tracker (gateway-identity-rogue-dhcp,
        # inventory feature #2) -- OPTIONAL wavr.gateway_monitor.GatewayIdentityMonitor.
        # When given, each scan feeds this cycle's is_gateway binding into it so
        # a two-factor-debounced gateway-identity change surfaces as its own
        # GatewayAlert. None -> no gateway-identity tracking (unchanged).
        self._gateway_monitor = gateway_monitor
        # HA-import identity (A4.1) -- OPTIONAL wavr.ha_import_store.HAImportStore.
        # User-triggered POST /api/ha/import persists per-MAC make/model/os/type
        # imported from the local Home Assistant registry; each scan folds it back
        # in as the recog `ha` signal (a self_report-family, medium-capped signal --
        # wavr.recog A4.0). Read fresh each cycle (like `_type_pins`) so an import
        # takes effect on the very next scan; tolerant -- a store error must never
        # break scanning.
        self._ha_store = ha_store
        # NetBIOS/SNMP (active, targeted -- see module docstring for the
        # shared-subnet "known-only" mitigation and no-log-community rule).
        self._netbios_enabled = netbios_enabled
        self._netbios_scope_known_only = netbios_scope_known_only
        self._netbios_prober = netbios_prober
        self._snmp_enabled = snmp_enabled
        self._snmp_community = snmp_community
        self._snmp_scope_known_only = snmp_scope_known_only
        self._snmp_prober = snmp_prober
        # Audit fix #4: a NON-DEFAULT community is a credential for the
        # operator's OWN gear; sending it in SNMPv1 cleartext to every
        # ARP-discovered host (scope=all) reaches hosts on a shared/guest
        # subnet the operator doesn't own, where it can be captured. Warn
        # once at construction (never logs the community itself -- same
        # no-log-community rule as the collector-failure warning below).
        if snmp_enabled and snmp_community != "public" and not snmp_scope_known_only:
            _LOG.warning(
                "SNMP scope is widened to every ARP-discovered host "
                "(WAVR_NET_SNMP_SCOPE=all) with a non-default community "
                "configured -- that community will be sent in cleartext to "
                "hosts outside the known-MAC allowlist; set "
                "WAVR_NET_SNMP_SCOPE=known (the default) to avoid this"
            )
        # system-toggles sensing master (feature "system-toggles"): an OPTIONAL
        # () -> bool callable (app.py wires ConnectorStore.sensing_allowed, read
        # fresh every scan -- same "no restart, revocable" contract as
        # known_provider/ha_store above). None -> always on (today's default,
        # byte-identical, and every test double that doesn't pass this kwarg).
        self._sensing_allowed = sensing_allowed
        self._inventory: list[Device] = []
        # ISO-8601 UTC of the most recent completed scan_once() (None before the
        # first scan). Feeds net_doctor's inventory-freshness check; not
        # otherwise consumed by this service.
        self._last_scan_ts: str | None = None
        self._alerts: list[RogueAlert] = []
        self._alerted: set[str] = set()   # MACs already alerted (edge-triggered)
        self._task: asyncio.Task | None = None

    def _sensing_on(self) -> bool:
        """system-toggles sensing master: True (on) unless the operator has
        explicitly blocked network sensing from the System tab. Gates the
        OPTIONAL active/passive collectors only (port scan, mDNS/SSDP/NetBIOS/
        SNMP/DHCP-fp, latency) -- never the base ARP inventory scan itself."""
        return self._sensing_allowed is None or self._sensing_allowed()

    def _port_scan_on(self) -> bool:
        """OFF unless explicitly overridden (tests) or WAVR_NET_PORTSCAN is set --
        AND the system-toggles sensing master is on (see `_sensing_on`)."""
        base = port_scan_enabled() if self._port_scan is None else self._port_scan
        return base and self._sensing_on()

    def _port_scan_known_only_on(self) -> bool:
        """OFF unless explicitly overridden (tests) or WAVR_NET_PORTSCAN_SCOPE=known."""
        return (port_scan_known_only_enabled() if self._port_scan_known_only is None
                else self._port_scan_known_only)

    def _dynamic_known(self) -> set[str]:
        """The static env allowlist UNIONED with the runtime KnownStore (if
        wired), read fresh every scan -- see `known_provider` in the class
        docstring. Tolerant, same rule as `_type_pins`/`_ha_signals`: a
        broken/missing provider must never break scanning, it just falls
        back to the static allowlist only."""
        if not self._known_provider:
            return self._known
        try:
            return self._known | _norm_macs(self._known_provider())
        except Exception:
            _LOG.warning("known_provider failed", exc_info=True)
            return self._known

    async def scan_once(self) -> list[Device]:
        """Run a single scan: refresh the inventory and fold any new unknown MACs
        into the rogue-alert log. Called by the background loop; also directly
        callable (deterministic) for tests."""
        pins = self._type_pins()
        known_at_start = self._dynamic_known()
        devices = await scan_inventory(known_macs=known_at_start, scan=self._scan,
                                       pins=pins, resolve=self._make_hostname_resolver(),
                                       gateway=self._gateway_hook())
        if self._port_scan_on():
            # Opt-in connect-only pass: risk notes + open_ports, then re-fuse
            # identity so port-derived type hints fold into device_type.
            if self._port_scan_known_only_on():
                # Scoped mode (L3 audit fix): only connect-scan devices already on
                # the known-MAC allowlist -- an unknown/rogue host on a shared
                # subnet is left untouched by the port pass (still inventoried and
                # still alerted on, just never connect-scanned).
                known_devs = [d for d in devices if d.known]
                scanned = {d.mac: d for d in await annotate_ports(known_devs, probe=self._port_probe)}
                devices = [scanned.get(d.mac, d) for d in devices]
            else:
                devices = await annotate_ports(devices, probe=self._port_probe)

        signals = await self._collect_protocol_signals(devices)
        ha_sigs = self._ha_signals()

        if self._port_scan_on() or signals or ha_sigs:
            devices = [
                apply_recognition(
                    d, pin=pins.get(d.mac),
                    bonjour=signals.get(d.mac, {}).get("bonjour"),
                    upnp=signals.get(d.mac, {}).get("upnp"),
                    snmp=signals.get(d.mac, {}).get("snmp"),
                    netbios=signals.get(d.mac, {}).get("netbios"),
                    dhcp=signals.get(d.mac, {}).get("dhcp"),
                    ha=ha_sigs.get(d.mac),
                )
                for d in devices
            ]
        # system-toggles sensing master: latency actively TCP-connects each host
        # (like the port pass), so it is gated the same way -- see `_sensing_on`.
        if self._latency_enabled and self._sensing_on():
            devices = await self._annotate_latency(devices)
        # Trust-vs-scan race fix (audit MEDIUM): `known` on every Device here was
        # resolved from `known_at_start`, snapshotted BEFORE the ~seconds of awaits
        # above. If the operator hit POST /api/inventory/known ("Trust"/"Trust all")
        # WHILE this scan was in flight, apply_known_change already patched the live
        # _inventory/_alerts -- but writing `devices` (built from the stale set) to
        # self._inventory below, then running _record_rogues on it, would clobber
        # that patch and RE-ALERT the just-trusted device for one full ~30s cycle
        # ("trust doesn't stick"). Re-derive from a FRESH read (the KnownStore is
        # already authoritative) so both the cache and the rogue check agree with
        # what the operator just did. Only pays the O(n) rebuild when a known-change
        # actually landed mid-scan.
        known_now = self._dynamic_known()
        if known_now != known_at_start:
            devices = [replace(d, known=(d.mac in known_now)) for d in devices]
        self._inventory = devices
        self._last_scan_ts = datetime.now(timezone.utc).isoformat()
        self._observe_gateway(devices)
        self._record_rogues(devices)
        await self._record_seen(devices)
        return devices

    def _get_mdns(self):
        # Lazily built so a real multicast socket is only ever opened once
        # WAVR_NET_MDNS is actually on (tests inject a fake collector instead).
        if self._mdns is None:
            from wavr.sources.mdns import MDNSCollector
            self._mdns = MDNSCollector()
        return self._mdns

    def _get_ssdp(self):
        if self._ssdp is None:
            from wavr.sources.ssdp import SSDPCollector
            self._ssdp = SSDPCollector(fetch_location=self._ssdp_location_enabled)
        return self._ssdp

    def _get_dhcp_fp(self):
        # Lazily built, same rationale as _get_mdns/_get_ssdp -- the UDP/67
        # socket is only ever opened once WAVR_NET_DHCP_FP is actually on.
        if self._dhcp_fp is None:
            from wavr.sources.dhcp_fp import DHCPFingerprintCollector
            self._dhcp_fp = DHCPFingerprintCollector()
        return self._dhcp_fp

    def dhcp_fp_status(self) -> dict:
        """Honest availability signal for the DHCP-fingerprint collector
        (panel-review finding #9/#17): {"available": bool|None, "reason":
        str|None}. None/None when the feature is off, a scan cycle hasn't run
        yet since startup (the collector is lazily built -- see
        `_get_dhcp_fp`), or an injected test double doesn't carry the
        attribute -- callers should treat that exactly like today's behavior
        (no signal either way), never as a false "unavailable"."""
        if self._dhcp_fp is None:
            return {"available": None, "reason": None}
        return {"available": getattr(self._dhcp_fp, "available", None),
                "reason": getattr(self._dhcp_fp, "unavailable_reason", None)}

    def _make_hostname_resolver(self):
        """The reverse-DNS resolve hook passed to scan_inventory, or None when
        the feature is OFF (so zero PTR queries happen by default). An injected
        `hostname_resolver` wins (tests); otherwise the real
        wavr.hostname_resolver.resolve_hostnames is imported lazily so no DNS
        socket is ever opened unless the feature is actually enabled."""
        if not self._hostname_resolve_enabled:
            return None
        if self._hostname_resolver is not None:
            return self._hostname_resolver
        from wavr.hostname_resolver import resolve_hostnames
        return resolve_hostnames

    def _gateway_hook(self):
        """The default-gateway detector passed to scan_inventory, or None when
        gateway detection is off (so no ipconfig/route subprocess runs and no
        device is ever flagged is_gateway). An injected detector wins (tests);
        otherwise the real wavr.sources.network.default_gateway is imported
        lazily. Reading the local routing table is zero-egress and touches no
        other host, so app.py enables it unconditionally -- unlike the active
        latency/port passes it needs no shared-subnet opt-in."""
        if not self._gateway_detect_enabled:
            return None
        if self._gateway_detector is not None:
            return self._gateway_detector
        from wavr.sources.network import default_gateway
        return default_gateway

    def _observe_gateway(self, devices: list[Device]) -> None:
        """Feed this cycle's gateway_ip -> gateway_mac binding (the is_gateway
        device the prior task flags from THIS host's routing table) into the
        optional gateway-identity monitor. Tolerant: a monitor error must never
        break scanning, same rule as on_rogue/device_meta. A cycle with no
        resolved gateway passes None -> the monitor treats it as a neutral
        (non-)event, never a spurious change."""
        if self._gateway_monitor is None:
            return
        gw = next((d for d in devices if d.is_gateway), None)
        try:
            self._gateway_monitor.observe(gw.ip if gw else None,
                                          gw.mac if gw else None)
        except Exception:
            _LOG.warning("gateway identity monitor failed", exc_info=True)

    async def _annotate_latency(self, devices: list[Device]) -> list[Device]:
        """Opt-in per-device LAN latency: TCP-connect round-trip in ms via
        wavr.netutils.ping_host (no ICMP, no elevated privileges), filling
        Device.latency_ms. OFF by default (latency_enabled) -- like the port
        pass it actively connects to each host, so it is gated, not automatic.
        Devices without an IP pass through untouched (latency stays None); a
        probe failure is a None latency, never an exception. Bounded
        concurrency mirrors the ARP ping sweep so a large /24 can't open
        hundreds of sockets at once. `ping` is the injectable probe (tests);
        production uses ping_host. Never mutates the input."""
        ping = self._ping or ping_host
        sem = asyncio.Semaphore(32)

        async def _one(d: Device) -> Device:
            if not d.ip:
                return d
            async with sem:
                try:
                    ms = await ping(d.ip)
                except Exception:
                    _LOG.warning("latency probe failed for %s", d.mac, exc_info=True)
                    ms = None
            return replace(d, latency_ms=ms)

        return list(await asyncio.gather(*(_one(d) for d in devices)))

    def _active_probe_targets(self, devices: list[Device], scope_known_only: bool) -> list[str]:
        """IPs for THIS cycle's active per-host probes (NetBIOS/SNMP) --
        always scoped to hosts the ARP sweep already resolved this cycle
        (never a subnet sweep of their own). `scope_known_only` narrows
        further to the known-MAC allowlist -- the shared-subnet mitigation
        mirroring `netutils.port_scan_known_only_enabled`."""
        pool = [d for d in devices if d.known] if scope_known_only else devices
        return [d.ip for d in pool if d.ip]

    async def _collect_protocol_signals(self, devices: list[Device]) -> dict[str, dict]:
        """Run whichever collectors are enabled (concurrently) and return
        {mac: {"bonjour": dict?, "upnp": dict?, "snmp": dict?, "netbios": dict?,
        "dhcp": dict?}} for macs already present in THIS cycle's ARP-resolved
        `devices` -- a signal for a host not in that set has no device to
        attach to and is dropped (never invented into a phantom entry).
        Tolerant: a collector raising is logged (never its arguments -- the
        SNMP community string in particular is never logged) and simply
        contributes nothing, never aborts the scan."""
        # system-toggles sensing master: suppresses every passive/active protocol
        # collector below when the operator has blocked sensing from the System
        # tab -- checked before the per-collector enabled flags, see `_sensing_on`.
        if not self._sensing_on():
            return {}
        if not (self._mdns_enabled or self._ssdp_enabled or self._netbios_enabled
                or self._snmp_enabled or self._dhcp_fp_enabled):
            return {}
        ip_to_mac = {d.ip: d.mac for d in devices if d.ip}

        async def _mdns_task() -> tuple[str, dict]:
            try:
                return "bonjour", await self._get_mdns().collect(duration=self._collect_duration)
            except Exception:
                _LOG.warning("mdns collector failed", exc_info=True)
                return "bonjour", {}

        async def _ssdp_task() -> tuple[str, dict]:
            try:
                return "upnp", await self._get_ssdp().collect(duration=self._collect_duration)
            except Exception:
                _LOG.warning("ssdp collector failed", exc_info=True)
                return "upnp", {}

        async def _dhcp_fp_task() -> tuple[str, dict]:
            try:
                # Already MAC-keyed (parsed from chaddr) -- see module docstring.
                return "dhcp", await self._get_dhcp_fp().collect(duration=self._collect_duration)
            except Exception:
                _LOG.warning("dhcp fingerprint collector failed", exc_info=True)
                return "dhcp", {}

        async def _netbios_task() -> tuple[str, dict]:
            try:
                from wavr.sources.netbios import NetBIOSCollector
                targets = self._active_probe_targets(devices, self._netbios_scope_known_only)
                collector = NetBIOSCollector(targets=targets, ip_to_mac=ip_to_mac,
                                             prober=self._netbios_prober)
                return "netbios", await collector.collect()
            except Exception:
                _LOG.warning("netbios collector failed", exc_info=True)
                return "netbios", {}

        async def _snmp_task() -> tuple[str, dict]:
            try:
                from wavr.sources.snmp import SNMPCollector
                targets = self._active_probe_targets(devices, self._snmp_scope_known_only)
                # NEVER log self._snmp_community (no-log-community mitigation) --
                # the warning below names only the collector, no arguments.
                collector = SNMPCollector(targets=targets, community=self._snmp_community,
                                          ip_to_mac=ip_to_mac, prober=self._snmp_prober)
                return "snmp", await collector.collect()
            except Exception:
                _LOG.warning("snmp collector failed", exc_info=True)
                return "snmp", {}

        tasks = []
        if self._mdns_enabled:
            tasks.append(_mdns_task())
        if self._ssdp_enabled:
            tasks.append(_ssdp_task())
        if self._dhcp_fp_enabled:
            tasks.append(_dhcp_fp_task())
        if self._netbios_enabled:
            tasks.append(_netbios_task())
        if self._snmp_enabled:
            tasks.append(_snmp_task())

        out: dict[str, dict] = {}
        for kind, raw in await asyncio.gather(*tasks):
            for key, sig in raw.items():
                if kind in ("netbios", "snmp", "dhcp"):
                    # Already MAC-keyed by the collector itself (dhcp parses
                    # chaddr directly; netbios/snmp are only ever targeted at
                    # this cycle's own ip_to_mac, so every target resolves).
                    mac = key
                else:
                    mac = ip_to_mac.get(key)
                    if mac is None:
                        continue   # not resolved by ARP this cycle -- nothing to attach to
                out.setdefault(mac, {})[kind] = sig
        return out

    def _type_pins(self) -> dict:
        """User device-type pins (mac -> taxonomy value) from device_meta --
        the highest-precedence recog signal. Tolerant, same rule as `seen`:
        a broken/legacy store must never break scanning."""
        if not self._device_meta:
            return {}
        try:
            return self._device_meta.type_pins()
        except Exception:
            _LOG.warning("device_meta.type_pins failed", exc_info=True)
            return {}

    def _ha_signals(self) -> dict:
        """Per-MAC HA-imported identity as {mac: {device_type?, make?, model?,
        os?}} from the optional ha_store -- the recog `ha` signal (A4.1). Read
        fresh each scan so a user-triggered import applies on the next cycle.
        Tolerant, same rule as `_type_pins`: a broken store must never break
        scanning."""
        if not self._ha_store:
            return {}
        try:
            return self._ha_store.signals()
        except Exception:
            _LOG.warning("ha_store.signals failed", exc_info=True)
            return {}

    async def _record_seen(self, devices) -> None:
        # Feature A: persist first-seen/last-seen for every observed MAC, not
        # just rogue ones. ONE batched call (DeviceMeta.seen_many -- one sqlite
        # commit for the whole scan cycle instead of one seen()+commit per
        # device) run via asyncio.to_thread so the synchronous sqlite write
        # never blocks THIS event loop, even on a slow/busy disk. Tolerant,
        # same rule as the on_rogue callback -- a persistence failure (raised
        # either by the write itself, or by a store that predates seen_many()
        # and doesn't have it) must never break scanning, even though nothing
        # gets recorded that cycle in the latter case.
        if not self._device_meta:
            return
        macs = [d.mac for d in devices]
        try:
            await asyncio.to_thread(self._device_meta.seen_many, macs)
        except Exception:
            _LOG.warning("device_meta.seen_many failed for %d MACs", len(macs), exc_info=True)

    def _record_rogues(self, devices) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        for d in devices:
            if d.known or d.mac in self._alerted:   # allowlisted or already seen
                continue
            self._alerted.add(d.mac)
            # Severity on wavr.alert_severity's ONE ladder: a randomized
            # (locally-administered bit set) MAC is the classic guest-phone
            # rejoin -> `info`; any other new unknown device -> `note`. The
            # locally-administered bit is a public IEEE 802 signal (license-safe,
            # inventory feature #3), never a a proprietary asset.
            severity = (SEVERITY_INFO if is_locally_administered(d.mac)
                        else SEVERITY_NOTE)
            alert = RogueAlert(
                ts=ts, mac=d.mac, vendor=d.vendor,
                ip=d.ip, device_type=d.device_type, hostname=d.hostname,
                type_confidence=d.type_confidence, severity=severity,
            )
            self._alerts.append(alert)
            if self._on_rogue:
                try:
                    self._on_rogue(alert)
                except Exception:
                    _LOG.warning("on_rogue callback failed", exc_info=True)
        if len(self._alerts) > self._max_alerts:    # bounded ring
            self._alerts = self._alerts[-self._max_alerts:]
        # Bound the edge-trigger dedup set: once it grows large (MAC randomization +
        # transient visitors accumulate forever), forget MACs no longer on the LAN.
        # A departed device re-alerts if it returns, which is fine.
        if len(self._alerted) > 4 * self._max_alerts:
            self._alerted &= {d.mac for d in devices}

    def apply_known_change(self, mac: str, known: bool) -> None:
        """Called synchronously by the POST /api/inventory/known route the
        moment an admin toggles a device's runtime-known state (after it has
        already persisted the change to wavr.known_store.KnownStore) --
        keeps the LIVE alert log and cached inventory in sync without
        waiting for the next scan cycle:

        Known ON: any existing rogue_device alert(s) for this MAC are
        dropped from the in-memory alert log immediately (so /api/alerts
        stops showing it right away) and the MAC is cleared from the
        edge-trigger dedup set (an already-known device needs no dedup
        tracking). The cached `latest_inventory()` is also patched so
        GET /api/inventory reflects known=true immediately, same courtesy
        `_device_view`'s type-pin override already gives PUT
        /api/inventory/type.

        Known OFF ("re-arm"): clears the edge-trigger dedup set for this MAC
        so a device that resurfaces as unknown on the NEXT scan alerts
        again, exactly like a brand-new unknown MAC would -- nothing is
        invented retroactively for the moment leading up to this toggle.
        The cached inventory is patched to known=false immediately too.

        The next scan_once() re-derives the authoritative `known` flag from
        `_dynamic_known()` (which reads the KnownStore fresh) regardless --
        this method only closes the gap until that happens."""
        mac = mac.strip().replace("-", ":").lower()
        self._alerted.discard(mac)
        if known:
            self._alerts = [a for a in self._alerts if a.mac != mac]
        self._inventory = [
            replace(d, known=known) if d.mac == mac else d
            for d in self._inventory
        ]

    def apply_known_change_many(self, macs, known: bool) -> None:
        """Batched `apply_known_change` for a whole set of MACs at once: ONE
        O(n) inventory rebuild + ONE alert-list filter for the entire set,
        instead of the per-MAC O(n) rebuild the single-device method does (which
        the bulk "Trust all N" route called in a loop -> O(n*U), pathological at
        airport scale). Same semantics as calling apply_known_change once per
        MAC, just without re-walking the whole inventory U times."""
        macset = {m.strip().replace("-", ":").lower() for m in macs}
        if not macset:
            return
        self._alerted -= macset
        if known:
            self._alerts = [a for a in self._alerts if a.mac not in macset]
        self._inventory = [
            replace(d, known=known) if d.mac in macset else d
            for d in self._inventory
        ]

    def latest_inventory(self) -> list[Device]:
        """The devices from the most recent scan (empty before the first)."""
        return list(self._inventory)

    def last_scan_ts(self) -> str | None:
        """ISO-8601 UTC of the most recent completed scan, or None if no scan
        has run yet since startup."""
        return self._last_scan_ts

    def recent_alerts(self, limit: int = 50) -> list[RogueAlert]:
        """The most recent rogue-device alerts, newest last."""
        return self._alerts[-limit:]

    async def _run(self) -> None:
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOG.exception("network inventory scan failed")
            if self._interval:
                await asyncio.sleep(self._interval)

    async def start(self) -> None:
        """Spawn the periodic scan task (idempotent)."""
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel the scan task, cancel-safe (mirrors SourceManager teardown)."""
        task, self._task = self._task, None
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
