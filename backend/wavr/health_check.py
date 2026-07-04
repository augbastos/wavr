"""5-tier LAN/internet health-check ladder (defensive-inventory #12) -- generalizes
`internet_monitor.py`'s single gateway ping into gateway + DNS-resolver +
optional operator-configured extra targets, rolled into one severity verdict.

ON-DEMAND, LOCAL-BY-DEFAULT: this module runs nothing on its own -- the
caller (the existing `GET /api/health` route) decides when a check happens,
same as today. No new background task, no new opt-in flag needed just to
call it. The gateway check still targets the LAN router by default (never a
fixed cloud host, mirroring `internet_monitor.guess_gateway`); the three DNS
resolvers below ARE public internet hosts and DO make real egress WHEN this
route is actually called -- that's the point of a resolver check (it answers
"is the internet up", not just "is the LAN up").

Severity ladder -- 5 tiers total (ok is the non-problem baseline; mirrors the
CONCEPT of a proprietary scanner's health-check ladder only, original tier names/thresholds,
no proprietary data/wording copied):
  ok        gateway reachable, every resolver reachable, no extra target down.
  minor     gateway reachable, AT MOST ONE resolver unreachable (isolated DNS
            flakiness, not an outage) OR exactly one extra target down.
  degraded  gateway reachable, MORE THAN HALF the configured resolvers
            unreachable (2 of the default 3) -- DNS is broadly failing.
  major     gateway reachable but EVERY resolver unreachable -- the LAN
            itself is fine but the internet (or DNS entirely) looks down.
  critical  the gateway itself is unreachable -- no LAN routing at all, the
            worst case (nothing past the LAN can be meaningfully tested, so
            resolver/extra results are moot).
Extra targets are operator-configured, non-core hosts: their failure alone
never escalates past "minor"/"degraded" -- a broken extra target must never
read as bad as the internet actually being down.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from wavr.internet_monitor import make_checker

CheckFn = Callable[[], Awaitable[bool]]

# Public, independently-operated DNS resolvers (Cloudflare / Google / Quad9) --
# not a a proprietary catalog, just three commonly-cited independent
# operators so one operator's outage alone doesn't read as "internet is down".
DEFAULT_RESOLVERS: tuple[str, ...] = ("1.1.1.1", "8.8.8.8", "9.9.9.9")

SEVERITY_OK = "ok"
SEVERITY_MINOR = "minor"
SEVERITY_DEGRADED = "degraded"
SEVERITY_MAJOR = "major"
SEVERITY_CRITICAL = "critical"


def compute_severity(gateway_ok: bool, resolver_results: dict[str, bool],
                      extra_results: dict[str, bool] | None = None) -> str:
    """Pure severity calc, no I/O -- trivially unit-testable. See module
    docstring for the ladder's rationale."""
    if not gateway_ok:
        return SEVERITY_CRITICAL
    extra_results = extra_results or {}
    total = len(resolver_results)
    up = sum(1 for ok in resolver_results.values() if ok)
    down = total - up
    if total and up == 0:
        return SEVERITY_MAJOR
    if down > total / 2:
        return SEVERITY_DEGRADED
    if down >= 1:
        return SEVERITY_MINOR
    extras_down = sum(1 for ok in extra_results.values() if not ok)
    if extras_down > 1:
        return SEVERITY_DEGRADED
    if extras_down == 1:
        return SEVERITY_MINOR
    return SEVERITY_OK


def default_resolver_checkers(hosts: tuple[str, ...] = DEFAULT_RESOLVERS) -> dict[str, CheckFn]:
    """Build the default real resolver checkers -- one ICMP ping each (same
    transport `internet_monitor.make_checker` already uses for the gateway),
    lazily so no socket/subprocess exists until the check actually runs."""
    return {host: make_checker(host) for host in hosts}


def default_extra_checkers(hosts) -> dict[str, CheckFn]:
    return {host: make_checker(host) for host in (hosts or ())}


async def _run_all(checks: dict[str, CheckFn]) -> dict[str, bool]:
    """Run every named check concurrently; a raising check counts as False
    (tolerant, same rule as InternetMonitor.check_once) and never
    propagates."""
    if not checks:
        return {}

    async def _one(fn: CheckFn) -> bool:
        try:
            return await fn()
        except Exception:
            return False

    names = list(checks.keys())
    results = await asyncio.gather(*(_one(checks[n]) for n in names))
    return dict(zip(names, results))


async def check_health(gateway_check: CheckFn, gateway_host: str | None = None,
                        resolver_checks: dict[str, CheckFn] | None = None,
                        extra_checks: dict[str, CheckFn] | None = None) -> dict:
    """Run the full ladder once. Returns the `GET /api/health` JSON shape:
    {
      "severity": "ok"|"minor"|"degraded"|"major"|"critical",
      "gateway": {"ok": bool, "host": str|None},
      "resolvers": {"1.1.1.1": bool, "8.8.8.8": bool, "9.9.9.9": bool},
      "extra": {host: bool, ...},         # only configured targets; {} if none
      "failed": [names of every failed check, gateway first if it failed]
    }
    Every check runs concurrently; each is individually exception-tolerant
    (see `_run_all`/the inline gateway wrapper) -- one bad target can never
    crash the route. `resolver_checks`/`extra_checks` default to the real
    ping-based checkers when omitted; tests inject canned CheckFns instead
    (zero real network)."""
    resolver_checks = resolver_checks if resolver_checks is not None else default_resolver_checkers()
    extra_checks = extra_checks or {}

    async def _gateway() -> bool:
        try:
            return await gateway_check()
        except Exception:
            return False

    gateway_ok, resolvers, extra = await asyncio.gather(
        _gateway(), _run_all(resolver_checks), _run_all(extra_checks),
    )
    severity = compute_severity(gateway_ok, resolvers, extra)
    failed = ([] if gateway_ok else ["gateway"])
    failed += [name for name, ok in resolvers.items() if not ok]
    failed += [name for name, ok in extra.items() if not ok]
    return {
        "severity": severity,
        "gateway": {"ok": gateway_ok, "host": gateway_host},
        "resolvers": resolvers,
        "extra": extra,
        "failed": failed,
    }
