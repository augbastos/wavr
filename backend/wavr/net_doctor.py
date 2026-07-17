"""network-doctor: read-only LAN/source diagnosis + a narrow, explicitly-gated
auto-fix layer, built entirely from primitives that already exist elsewhere in
the codebase (SourceManager.set_enabled, NetworkInventoryService.scan_once,
mdns_peers.advertise_self). This module adds NO new low-level capability --
it only automates what an operator could already trigger by hand via the
existing routes (`/api/sources/{name}/toggle` etc).

PURE/IO SPLIT (same shape as `health_check.py`/`gateway_monitor.py`):
  * `diagnose()` -- pure, zero I/O, trivially unit-testable: takes plain
    dicts/lists (already-computed monitor snapshots) and returns a list of
    `DoctorCheck` (for display) plus a list of `FixCandidate` (things that
    COULD be auto-fixed).
  * `apply_fixes()` -- the only function that does I/O. Dispatches each
    `FixCandidate` to an injected async callable. NEVER RAISES: a failing fix
    degrades to a `DoctorSuggestion`, exactly like every monitor in this repo
    (see `dhcp_monitor.py`'s `check_once` tolerance rule) -- an auto-fix
    layer that could crash the route it lives on would be worse than none.

SAFE-AUTO ALLOWLIST (the whole point of this module -- enforced IN CODE, not
just by convention):
  * `restart_source` only ever cycles a source that is ALREADY `enabled=True`
    in `SourceManager.status()`. `diagnose()` never inspects, and therefore
    never proposes a fix for, a `enabled=False` source -- so a camera that is
    deliberately off (or in Tapo privacy mode) can NEVER be auto-restarted,
    let alone auto-enabled. There is no code path in this module that ever
    calls `set_enabled(name, True)` for a source that was not already on.
  * `gateway_identity`/`rogue_dhcp` checks are REPORT-ONLY by construction --
    no branch of `diagnose()` ever emits a `FixCandidate` for them. The
    router, other devices, and any DHCP server the operator doesn't control
    are never touched by this module.
  * Every fix is logged (`DoctorLog`, bounded in-memory ring) with kind/
    target/detail/ts only -- never a credential, frame, or rtsp_url.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

_LOG = logging.getLogger(__name__)

# Reuses health_check.py's severity vocabulary (ok/minor/degraded/major/
# critical) so a caller merging both views never has to reconcile two
# different severity ladders. `None` (not a string) means "not applicable /
# monitor disabled" -- an honest third state, never fabricated ok/bad.
SEVERITY_OK = "ok"
SEVERITY_MINOR = "minor"
SEVERITY_DEGRADED = "degraded"
SEVERITY_MAJOR = "major"
SEVERITY_CRITICAL = "critical"

_MAX_LOG = 50

# discovery_reach (net-doctor, sim cluster CL-02): the verdict is STRUCTURED data, never a
# flat string -- so the later PRs extend it (cause discrimination, remediation deep-link,
# shareable report) without rework. PR1 only ever emits MULTICAST_DEAD_UNKNOWN /
# INCONCLUSIVE_SMALL_NET / (healthy -> cause=None); PR2 refines MULTICAST_DEAD_UNKNOWN into
# AP_ISOLATION_OR_MDNS_FILTERING vs SECOND_NETWORK_VLAN via a unicast probe + DHCP/subnet cross-check.
CAUSE_AP_ISOLATION = "AP_ISOLATION_OR_MDNS_FILTERING"
CAUSE_SECOND_NETWORK = "SECOND_NETWORK_VLAN"
CAUSE_MULTICAST_DEAD = "MULTICAST_DEAD_UNKNOWN"
CAUSE_INCONCLUSIVE_SMALL = "INCONCLUSIVE_SMALL_NET"

CONF_LOW = "low"
CONF_MEDIUM = "medium"
CONF_HIGH = "high"

# ARP-visible floor below which we REFUSE to judge (a studio with 3 devices is not a
# pathology -- never false-positive it); and the multicast-responder ceiling that reads as
# "silent". The verdict is always a hypothesis ("provavelmente"), never CONFIRMED (ADR-0003).
DISCOVERY_MIN_ARP = 5
DISCOVERY_MCAST_SILENT = 1


@dataclass(frozen=True)
class DoctorVerdict:
    """Structured discovery_reach verdict (never a flat string, by design -- the later PRs
    extend it). `cause` is None when discovery reach is healthy OR unknowable; `copy_key`
    lets the frontend map to plain-language, hypothesis-framed copy without the backend
    baking a locale string; arp_count/mcast_responders are the two numbers the copy shows."""
    cause: str | None
    confidence: str
    arp_count: int
    mcast_responders: int
    copy_key: str

    def to_dict(self) -> dict:
        return {"cause": self.cause, "confidence": self.confidence,
                "arp_count": self.arp_count, "mcast_responders": self.mcast_responders,
                "copy_key": self.copy_key}


@dataclass(frozen=True)
class DoctorCheck:
    """One diagnosed item. `ok=None` is an honest "not applicable" (e.g. the
    underlying monitor is off) -- never fabricated as good or bad. `verdict` carries
    STRUCTURED discovery_reach data (None for every other check)."""
    id: str
    ok: bool | None
    severity: str | None
    detail: str
    verdict: "DoctorVerdict | None" = None

    def to_dict(self) -> dict:
        d = {"id": self.id, "ok": self.ok, "severity": self.severity, "detail": self.detail}
        if self.verdict is not None:
            d["verdict"] = self.verdict.to_dict()
        return d


@dataclass(frozen=True)
class FixCandidate:
    """An internal candidate for auto-fix -- never serialized directly to a
    caller; `apply_fixes` turns each one into either a `DoctorAction`
    (executed) or a `DoctorSuggestion` (not executed)."""
    id: str
    kind: str   # "restart_source" | "reannounce_mdns" | "reprobe_inventory"
    target: str
    explain: str


@dataclass(frozen=True)
class DoctorAction:
    """One executed, logged auto-fix. In-memory only, never a credential/
    frame/rtsp_url -- same disclosure rule as every alert dataclass in this
    repo (GatewayAlert, DhcpRogueAlert, RogueAlert)."""
    ts: str
    kind: str
    target: str
    detail: str

    def to_dict(self) -> dict:
        return {"ts": self.ts, "kind": self.kind, "target": self.target, "detail": self.detail}


@dataclass(frozen=True)
class DoctorSuggestion:
    """Anything NOT auto-fixed: auto-fix is off, the check is report-only by
    design (gateway/DHCP), or a fix attempt itself failed."""
    id: str
    message: str
    action_hint: str | None = None

    def to_dict(self) -> dict:
        return {"id": self.id, "message": self.message, "action_hint": self.action_hint}


class DoctorLog:
    """Bounded in-memory ring of executed `DoctorAction`s -- same convention
    as `RogueDhcpMonitor._alerts` / `GatewayIdentityMonitor._alerts`. Never
    persisted to disk (an auto-fix history is operational noise, not a
    signal worth surviving a restart)."""

    def __init__(self, max_len: int = _MAX_LOG):
        self._max = max(1, max_len)
        self._actions: list[DoctorAction] = []

    def record(self, action: DoctorAction) -> None:
        self._actions.append(action)
        if len(self._actions) > self._max:
            self._actions = self._actions[-self._max:]

    def recent(self, limit: int = _MAX_LOG) -> list[DoctorAction]:
        return self._actions[-limit:]


def _age_s(ts_iso: str | None) -> float | None:
    if not ts_iso:
        return None
    try:
        ts = datetime.fromisoformat(ts_iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds())
    except (ValueError, TypeError):
        return None


def diagnose(*, health: dict,
             gateway_status: dict | None, gateway_alerts: list[dict],
             dhcp_status: dict | None, dhcp_alerts: list[dict],
             source_status: dict,
             camera_down: list[str], camera_privacy: list[str],
             room_sources: dict[str, list[dict]],
             last_inventory_scan_ts: str | None, net_scan_interval: float,
             mdns_expected: bool, mdns_alive: bool,
             arp_count: int = 0, mcast_responders: int | None = None,
             ) -> tuple[list[DoctorCheck], list[FixCandidate]]:
    """Pure diagnosis pass -- no I/O, never raises (every check is wrapped so
    one bad input can't take down the others, mirroring `health_check._run_all`'s
    per-check tolerance). Returns (checks, fixable). `fixable` is de-duplicated
    by `target` so a source that is BOTH SourceManager-inactive AND
    camera_health-down doesn't get two competing restart candidates."""
    checks: list[DoctorCheck] = []
    fixable_by_target: dict[str, FixCandidate] = {}

    def _check(id_: str, fn: Callable[[], DoctorCheck]) -> None:
        try:
            checks.append(fn())
        except Exception:
            _LOG.warning("net_doctor: check %s failed", id_, exc_info=True)
            checks.append(DoctorCheck(id=id_, ok=None, severity=None,
                                       detail="check raised, see server log"))

    # 1. internet -- from the existing 5-tier ladder. No fix (external).
    def _internet() -> DoctorCheck:
        severity = health.get("severity")
        gw = health.get("gateway") or {}
        return DoctorCheck(id="internet", ok=(severity == SEVERITY_OK),
                            severity=severity,
                            detail=f"gateway={gw.get('ok')} failed={health.get('failed')}")
    _check("internet", _internet)

    # 2. dns -- from health["resolvers"]; empty dict is honest "off", not bad.
    def _dns() -> DoctorCheck:
        resolvers = health.get("resolvers") or {}
        if not resolvers:
            return DoctorCheck(id="dns", ok=None, severity=None,
                                detail="WAVR_HEALTH_RESOLVERS off")
        down = [h for h, ok in resolvers.items() if not ok]
        return DoctorCheck(id="dns", ok=not down,
                            severity=SEVERITY_OK if not down else SEVERITY_MINOR,
                            detail=f"down={down}" if down else "all resolvers up")
    _check("dns", _dns)

    # 3. gateway_identity -- REPORT-ONLY, never a fix (never touch the router).
    def _gateway_identity() -> DoctorCheck:
        if gateway_status is None:
            return DoctorCheck(id="gateway_identity", ok=None, severity=None,
                                detail="monitor disabled")
        bad = [a for a in gateway_alerts[-1:] if a.get("severity") in ("alert", "critical")]
        return DoctorCheck(id="gateway_identity", ok=not bad,
                            severity=SEVERITY_OK if not bad else SEVERITY_MAJOR,
                            detail="gateway identity stable" if not bad
                            else f"recent alert: {bad[0]}")
    _check("gateway_identity", _gateway_identity)

    # 4. rogue_dhcp -- REPORT-ONLY, never a fix (never touch other DHCP servers).
    def _rogue_dhcp() -> DoctorCheck:
        if dhcp_status is None:
            return DoctorCheck(id="rogue_dhcp", ok=None, severity=None,
                                detail="monitor disabled")
        if dhcp_status.get("available") is False:
            return DoctorCheck(id="rogue_dhcp", ok=None, severity=None,
                                detail=dhcp_status.get("unavailable_reason") or "unavailable")
        bad = dhcp_alerts[-1:]
        return DoctorCheck(id="rogue_dhcp", ok=not bad,
                            severity=SEVERITY_OK if not bad else SEVERITY_MAJOR,
                            detail="no rogue DHCP servers" if not bad
                            else f"recent alert: {bad[0]}")
    _check("rogue_dhcp", _rogue_dhcp)

    # 5. capture_stalled:<name> -- a source the operator has ENABLED but that
    # isn't actively running. Never inspects an enabled=False source, so a
    # deliberately-off camera/source can never be surfaced (let alone fixed).
    def _capture_stalled() -> None:
        for s in (source_status or {}).get("sources", []):
            if s.get("enabled") and not s.get("active"):
                name = s["name"]
                cid = f"capture_stalled:{name}"
                checks.append(DoctorCheck(id=cid, ok=False, severity=SEVERITY_DEGRADED,
                                           detail=f"source '{name}' is enabled but not active"))
                fixable_by_target.setdefault(name, FixCandidate(
                    id=cid, kind="restart_source", target=name,
                    explain=f"cycle enabled source '{name}' off/on"))
    try:
        _capture_stalled()
    except Exception:
        _LOG.warning("net_doctor: capture_stalled check failed", exc_info=True)

    # 6. camera_stalled:<name> -- F3 health-hook-latched-down cameras, EXCLUDING
    # anything in privacy mode, and only for a source the manager shows enabled
    # (satisfies "camera left OFF, never auto-enable" -- an off camera is
    # neither in source_status as enabled nor eligible here).
    def _camera_stalled() -> None:
        enabled_names = {s["name"] for s in (source_status or {}).get("sources", [])
                          if s.get("enabled")}
        for name in camera_down or []:
            if name in (camera_privacy or ()):
                continue   # deliberately covered -- never treated as faulty
            if name not in enabled_names:
                continue   # not enabled -- never surfaced/fixed here
            cid = f"camera_stalled:{name}"
            checks.append(DoctorCheck(id=cid, ok=False, severity=SEVERITY_DEGRADED,
                                       detail=f"camera '{name}' latched down (frames stalled)"))
            fixable_by_target.setdefault(name, FixCandidate(
                id=cid, kind="restart_source", target=name,
                explain=f"cycle enabled camera '{name}' off/on"))
    try:
        _camera_stalled()
    except Exception:
        _LOG.warning("net_doctor: camera_stalled check failed", exc_info=True)

    # 7. mdns_advertise -- self peer-discovery re-announce.
    def _mdns() -> DoctorCheck:
        if not mdns_expected:
            return DoctorCheck(id="mdns_advertise", ok=None, severity=None,
                                detail="peers disabled")
        if mdns_alive:
            return DoctorCheck(id="mdns_advertise", ok=True, severity=SEVERITY_OK,
                                detail="advertising")
        return DoctorCheck(id="mdns_advertise", ok=False, severity=SEVERITY_MINOR,
                            detail="peers enabled but self-advertise is not running")
    _check("mdns_advertise", _mdns)
    if mdns_expected and not mdns_alive:
        fixable_by_target.setdefault("self", FixCandidate(
            id="mdns_advertise", kind="reannounce_mdns", target="self",
            explain="re-register the _wavr._tcp mDNS self-advertisement"))

    # 8. inventory_freshness -- flush-cache-and-reprobe when stale.
    def _inventory_freshness() -> DoctorCheck:
        if last_inventory_scan_ts is None:
            return DoctorCheck(id="inventory_freshness", ok=None, severity=None,
                                detail="no scan has completed yet")
        age = _age_s(last_inventory_scan_ts)
        if age is None:
            return DoctorCheck(id="inventory_freshness", ok=None, severity=None,
                                detail="unparseable last-scan timestamp")
        stale = age > 2 * max(0.0, net_scan_interval or 0.0)
        return DoctorCheck(id="inventory_freshness", ok=not stale,
                            severity=SEVERITY_OK if not stale else SEVERITY_MINOR,
                            detail=f"last scan {round(age)}s ago")
    _check("inventory_freshness", _inventory_freshness)
    _age = _age_s(last_inventory_scan_ts)
    if _age is not None and _age > 2 * max(0.0, net_scan_interval or 0.0):
        fixable_by_target.setdefault("inventory", FixCandidate(
            id="inventory_freshness", kind="reprobe_inventory", target="inventory",
            explain="flush cache and re-scan the LAN inventory once"))

    # 9. signal_freshness -- REPORT-ONLY (any actionable case is already
    # covered by #5/#6 above); surfaces stale/dead fusion signal health.
    def _signal_freshness() -> DoctorCheck:
        stale_pairs: list[str] = []
        total = 0
        dead_or_worse = 0
        for room, sources in (room_sources or {}).items():
            for src in sources or ():
                total += 1
                h = src.get("health")
                if h in ("dead", "invalid_ts"):
                    dead_or_worse += 1
                if h in ("stale", "dead", "invalid_ts"):
                    stale_pairs.append(f"{room}/{src.get('modality')}:{h}")
        if total == 0:
            return DoctorCheck(id="signal_freshness", ok=None, severity=None,
                                detail="no fused rooms yet")
        total_loss = dead_or_worse == total
        return DoctorCheck(
            id="signal_freshness", ok=not total_loss,
            severity=(SEVERITY_CRITICAL if total_loss
                      else (SEVERITY_MINOR if stale_pairs else SEVERITY_OK)),
            detail=("total signal loss across all rooms" if total_loss
                    else (f"stale: {stale_pairs}" if stale_pairs else "all signals fresh")))
    _check("signal_freshness", _signal_freshness)

    # 10. discovery_reach -- REPORT-ONLY (never touch the router). Correlates the ARP-visible
    # device count against how many answered a multicast (mDNS/SSDP) probe: many devices
    # reachable but the mesh silent = the classic "router isolates devices / IoT-VLAN eats
    # multicast" pathology that leaves discovery cold while the rest of the doctor reads green.
    # PR1 names the GENERIC case (MULTICAST_DEAD_UNKNOWN); PR2 discriminates the cause. Always
    # a hypothesis ("provavelmente"), never CONFIRMED, never a FixCandidate (no router touch).
    def _discovery_reach() -> DoctorCheck:
        mcr = mcast_responders if mcast_responders is not None else -1
        if arp_count < DISCOVERY_MIN_ARP:
            # too few devices to distinguish "isolated" from "genuinely small home" -- and the
            # caller skips the probe entirely here (mcast_responders=None), so never claim dead
            return DoctorCheck(id="discovery_reach", ok=None, severity=None,
                               detail=f"only {arp_count} devices reachable -- too few to judge",
                               verdict=DoctorVerdict(CAUSE_INCONCLUSIVE_SMALL, CONF_LOW,
                                                     arp_count, mcr, "discovery_small_net"))
        if mcast_responders is None:
            # probe couldn't run (no socket / env) -> honest "can't tell", never a false verdict
            return DoctorCheck(id="discovery_reach", ok=None, severity=None,
                               detail="multicast probe unavailable",
                               verdict=DoctorVerdict(None, CONF_LOW, arp_count, -1,
                                                     "discovery_probe_unavailable"))
        if mcast_responders <= DISCOVERY_MCAST_SILENT:
            return DoctorCheck(id="discovery_reach", ok=False, severity=SEVERITY_DEGRADED,
                               detail=f"{arp_count} devices reachable, {mcast_responders} answered discovery",
                               verdict=DoctorVerdict(CAUSE_MULTICAST_DEAD, CONF_MEDIUM,
                                                     arp_count, mcast_responders, "discovery_multicast_dead"))
        return DoctorCheck(id="discovery_reach", ok=True, severity=SEVERITY_OK,
                           detail=f"{arp_count} reachable, {mcast_responders} answered discovery",
                           verdict=DoctorVerdict(None, CONF_HIGH, arp_count, mcast_responders,
                                                 "discovery_ok"))
    _check("discovery_reach", _discovery_reach)

    return checks, list(fixable_by_target.values())


async def apply_fixes(fixable: list[FixCandidate], *, enabled: bool,
                       restart_source: Callable[[str], Awaitable[None]],
                       reprobe_inventory: Callable[[], Awaitable[None]],
                       reannounce_mdns: Callable[[], object],
                       log: DoctorLog,
                       ) -> tuple[list[DoctorAction], list[DoctorSuggestion]]:
    """The only I/O in this module. When `enabled=False` (the default --
    two-factor-gated by the caller: env flag AND per-call opt-in) every
    candidate becomes a `DoctorSuggestion` and nothing executes. Never
    raises: a failing fix degrades to a suggestion, same tolerance rule as
    every monitor's `check_once`/`collect` in this repo."""
    actions: list[DoctorAction] = []
    suggestions: list[DoctorSuggestion] = []

    if not enabled:
        for c in fixable:
            suggestions.append(DoctorSuggestion(
                id=c.id, message=f"suggested: {c.explain}", action_hint=c.kind))
        return actions, suggestions

    for c in fixable:
        ts = datetime.now(timezone.utc).isoformat()
        try:
            if c.kind == "restart_source":
                await restart_source(c.target)
            elif c.kind == "reprobe_inventory":
                await reprobe_inventory()
            elif c.kind == "reannounce_mdns":
                result = reannounce_mdns()
                if hasattr(result, "__await__"):
                    await result
            else:
                raise ValueError(f"unknown fix kind: {c.kind}")
        except Exception as exc:
            _LOG.warning("net_doctor: auto-fix %s (%s/%s) failed", c.id, c.kind, c.target,
                         exc_info=True)
            suggestions.append(DoctorSuggestion(
                id=c.id, message=f"auto-fix failed: {c.explain} ({exc})", action_hint=c.kind))
            continue
        action = DoctorAction(ts=ts, kind=c.kind, target=c.target, detail=c.explain)
        log.record(action)
        actions.append(action)

    return actions, suggestions
