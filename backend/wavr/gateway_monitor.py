"""Gateway-MAC-identity tracker (inventory feature #2) -- LOCAL-ONLY, on-box.

The default gateway is the natural anchor for the scariest LAN event: whether
the router identity changed (ARP-spoofing / rogue-router-adjacent), materially
worse than an unknown phone joining the Wi-Fi. This tracker folds the per-scan
gateway_ip -> gateway_mac binding (built from the is_gateway flag
wavr.netinventory already sets from THIS host own routing table -- zero egress)
and fires a debounced alert when that binding changes to a MAC it does not trust
for that gateway IP.

TWO-FACTOR DEBOUNCE (the philosophy, NOT proprietary-tool numbers)
proprietary cloud DHCP-anomaly logic (confirmed on-box this session: agent_settings
dhcp_rate_*) trips only when BOTH a relative factor (2x baseline rate) AND an
absolute floor (30 requests) hold within a rolling window, throttled to one
report per hour, with the baseline cached to disk so a restart does not
false-alarm. We reimplement that BOTH-must-hold + debounce + throttle +
persist-across-restart SHAPE, but derive our OWN constants for a single home
~30s scan loop (proprietary-tool values are tuned for an MSP fleet -- never ported
verbatim):

  Factor 1 (identity -- the qualitative new MAC, our analogue of the 2x-rate
            trip): the gateway IP now answers with a MAC that is NOT trusted for
            it -- not in the operator allowlist AND not this IP established
            (first-seen, persisted) baseline.
  Factor 2 (persistence floor -- the quantitative debounce, our analogue of the
            30-request floor): that anomaly must hold across DEBOUNCE_CYCLES
            CONSECUTIVE scan cycles before it counts.

NEITHER factor alone fires. A router reboot / firmware update keeps the SAME NIC
MAC (fails Factor 1 -> silent). A single stale-ARP blip that clears next cycle
fails Factor 2 -> silent. Only a NEW MAC that PERSISTS crosses both.

PERSISTED ACROSS RESTARTS (inventory feature #7): the trusted per-IP baseline is written
to disk (an injectable store; production is a small sqlite table sharing
wavr.db). This is the whole point: if an attacker is spoofing the gateway MAC at
the moment Wavr restarts, an in-memory-only baseline would silently re-adopt the
SPOOFED MAC as trusted and never alert. Loading the last legitimate baseline
from disk means we still catch it. The transient debounce COUNTER is
deliberately NOT persisted -- resetting it on restart only costs a ~60s
re-confirmation and can never cause a false positive.

THROTTLED REPORTING + HONEST ESCALATION: once fired for a given (gateway IP,
rogue MAC), we re-alert at most once per THROTTLE_S (anti-spam, same idea as
a proprietary tool dhcp_rate_min_report_period_s). The FIRST debounced detection is severity
alert; a change still present when the throttle window expires re-fires as
critical (SUSTAINED). A change that self-heals before the window never reaches
critical: a source may never overstate confidence it lacks.

FULLY LOCAL, ZERO CLOUD: unlike a proprietary tool -- whose alerting brain is cloud-side over
a persistent AMQPS broker connection (confirmed live: outbound :5671 to
messaging a cloud service) -- this runs entirely on-box: no egress, no external
dependency. That is Wavr actual pitch advantage, and the reason it can ship on
by default.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from wavr.alert_severity import SEVERITY_ALERT, SEVERITY_CRITICAL

_LOG = logging.getLogger(__name__)

# Wavr OWN constants, DERIVED from its ~30s home-scan cadence -- NOT ported from
# a proprietary tool MSP-fleet numbers (see module docstring / the inventory roadmap).
#   DEBOUNCE_CYCLES: a changed gateway MAC must persist across this many
#     CONSECUTIVE scan cycles. At the default 30s scan interval it is ~60s --
#     long enough to ride out one stale-ARP blip or a router reboot momentary
#     MAC flap, short enough to catch a real ARP-spoof inside a minute.
#   THROTTLE_S: min seconds between re-alerts for the SAME (gateway IP, rogue
#     MAC). 30 min -- a persisting spoof should nag promptly (a home has exactly
#     ONE gateway to watch, unlike an MSP fleet), but never spam the banner.
#     a proprietary tool throttles to 1h for many-site triage; we pick a tighter home value.
DEFAULT_DEBOUNCE_CYCLES = 2
DEFAULT_THROTTLE_S = 1800.0


def _norm_mac(mac: str) -> str:
    """Lowercase colon-form MAC, accepting dash or colon separators, same
    convention as netinventory/device_meta. Defensive: never raises."""
    return (mac or "").strip().replace("-", ":").lower()


@dataclass(frozen=True)
class GatewayAlert:
    """One gateway-identity-change sighting -- the default gateway IP started
    answering with a MAC we do not trust for it. In-memory only (bounded ring),
    same convention as netinventory_service.RogueAlert / dhcp_monitor alert.

    trusted_mac is the established/allowlisted MAC we expected; observed_mac is
    the new one now answering. severity rides wavr.alert_severity ONE ladder:
    alert on the first debounced detection, critical once the change is SUSTAINED
    across the re-alert throttle window (never critical on a first sighting)."""
    ts: str
    gateway_ip: str
    trusted_mac: str
    observed_mac: str
    severity: str = SEVERITY_ALERT

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "kind": "gateway_identity",
            "severity": self.severity,
            "gateway_ip": self.gateway_ip,
            "trusted_mac": self.trusted_mac,
            "observed_mac": self.observed_mac,
        }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS gateway_binding (
    gateway_ip  TEXT PRIMARY KEY,
    gateway_mac TEXT NOT NULL,
    first_seen  TEXT,
    last_seen   TEXT
);
"""


class GatewayBindingStore:
    """Persists the trusted gateway_ip -> gateway_mac baseline across restarts
    (inventory feature #7: an in-memory-only baseline would re-adopt a spoofed MAC on
    every restart). Small sqlite store, injectable path, in-memory for tests --
    same shape as wavr.device_meta / wavr.camera_store, shares the wavr.db file
    but owns its own table.

    Stores ONLY coarse network topology: a gateway IP and its NIC MAC, both
    already visible in /api/inventory. NEVER occupancy / presence / PII -- this
    table must never become a place a fusion-debug path writes real occupancy
    data (the wavr.db-wal PII-leak lesson)."""

    def __init__(self, path: str = "wavr.db"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def load(self) -> dict[str, str]:
        """Every persisted baseline as {gateway_ip: gateway_mac}."""
        rows = self._conn.execute(
            "SELECT gateway_ip, gateway_mac FROM gateway_binding"
        ).fetchall()
        return {r["gateway_ip"]: r["gateway_mac"] for r in rows}

    def set(self, gateway_ip: str, gateway_mac: str) -> None:
        """Upsert the trusted baseline for a gateway IP (sets first_seen once,
        bumps last_seen thereafter -- mirrors device_meta.seen)."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """INSERT INTO gateway_binding (gateway_ip, gateway_mac, first_seen, last_seen)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(gateway_ip) DO UPDATE SET
                   gateway_mac = excluded.gateway_mac,
                   first_seen  = COALESCE(gateway_binding.first_seen, excluded.first_seen),
                   last_seen   = excluded.last_seen""",
            (gateway_ip, gateway_mac, now, now),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


class GatewayIdentityMonitor:
    """Tracks the default gateway MAC identity across scan cycles and fires a
    two-factor-debounced, throttled GatewayAlert when it changes to an untrusted
    MAC. DRIVEN by the inventory scan (not its own loop): the caller --
    wavr.netinventory_service.NetworkInventoryService -- calls observe(ip, mac)
    once per cycle with this cycle is_gateway device, so there is no second
    scanner and no extra network footprint. See the module docstring for the
    full debounce / persistence / throttle rationale.

    store (optional): persistence with .load() -> {ip: mac} and .set(ip, mac);
    None -> in-memory only (still works, just no restart persistence).
    known_macs (optional): operator-allowlisted gateway MACs (config), ALWAYS
    trusted regardless of IP -- the escape hatch for a router swap or a failover
    pair, same allowlist idea as NetworkInventoryService.known_macs. throttle
    timing uses now (injectable monotonic clock; tests advance it). on_alert:
    optional callback fired on the SAME edge the alert log records -- a raising
    callback is caught, never propagated (a broken notifier must not break
    scanning)."""

    def __init__(self, store=None, known_macs=None,
                 debounce_cycles: int = DEFAULT_DEBOUNCE_CYCLES,
                 throttle_s: float = DEFAULT_THROTTLE_S,
                 max_alerts: int = 50,
                 now: Callable[[], float] | None = None,
                 on_alert: Callable[[GatewayAlert], None] | None = None):
        self._store = store
        self._known_macs = {_norm_mac(m) for m in (known_macs or ()) if _norm_mac(m)}
        self._debounce_cycles = max(1, debounce_cycles)
        self._throttle_s = max(0.0, throttle_s)
        self._max_alerts = max_alerts
        self._now = now or time.monotonic
        self._on_alert = on_alert
        # Trusted per-IP baseline, seeded from the persisted store (survives
        # restarts). known_macs is applied on top at observe() time and wins
        # over any stale persisted baseline.
        self._trusted: dict[str, str] = {}
        if store is not None:
            try:
                self._trusted = dict(store.load())
            except Exception:
                _LOG.warning("gateway binding store load failed", exc_info=True)
                self._trusted = {}
        # Transient debounce state (in-memory only -- NOT persisted). _pending
        # counts CONSECUTIVE anomalous cycles per IP; _pending_mac is the
        # specific anomalous MAC being accumulated (a DIFFERENT new MAC restarts
        # the count). _last_alert throttles per (ip, observed_mac) and drives
        # the alert -> critical escalation.
        self._pending: dict[str, int] = {}
        self._pending_mac: dict[str, str] = {}
        self._last_alert: dict[tuple[str, str], float] = {}
        self._alerts: list[GatewayAlert] = []

    def status(self) -> dict:
        """Read-only view of the trusted baselines; never the debounce counters."""
        return {"trusted_bindings": dict(self._trusted)}

    def recent_alerts(self, limit: int = 50) -> list[GatewayAlert]:
        return self._alerts[-limit:]

    def observe(self, gateway_ip, gateway_mac) -> None:
        """Fold one scan cycle gateway binding into the debounced state. A cycle
        with no resolved gateway (either arg falsy) is NEUTRAL -- it neither
        advances nor resets the debounce (a momentary ARP miss must not wipe a
        genuine accumulating anomaly, nor invent one). Never raises."""
        if not gateway_ip or not gateway_mac:
            return
        ip = gateway_ip.strip()
        mac = _norm_mac(gateway_mac)
        if not ip or not mac:
            return

        if mac in self._known_macs:
            # Operator-allowlisted gateway MAC -- always trusted (router swap /
            # failover pair). Adopt it as this IP baseline and clear any pending
            # anomaly (Factor 1 is not satisfied by an allowlisted MAC).
            self._settle(ip, mac)
            return

        baseline = self._trusted.get(ip)
        if baseline is None:
            # First-ever determination for this gateway IP settles the baseline
            # WITHOUT alerting (mirrors RogueDhcpMonitor / InternetMonitor
            # first-guard -- there is no prior identity to have changed from).
            self._settle(ip, mac)
            return

        if mac == baseline:
            # Still (or back to) the trusted identity -- clear any pending
            # anomaly and forget this IP throttle state so a genuinely NEW future
            # change starts fresh at alert, never a stale critical.
            self._clear_pending(ip)
            return

        # Factor 1 satisfied: gateway IP answers with an untrusted MAC.
        if self._pending_mac.get(ip) != mac:
            # A different anomalous MAC than the one we were accumulating --
            # restart the consecutive-cycle count for this fresh identity.
            self._pending_mac[ip] = mac
            self._pending[ip] = 0
        self._pending[ip] += 1

        if self._pending[ip] < self._debounce_cycles:
            return  # Factor 2 not yet met -- honest silence, no premature alert.

        # Both factors met. Throttled, escalating fire.
        self._maybe_fire(ip, baseline, mac)

    def _settle(self, ip: str, mac: str) -> None:
        """Trust mac as this IP baseline (persist only on an actual change, so
        steady state does zero disk writes) and clear pending anomaly."""
        if self._trusted.get(ip) != mac:
            self._trusted[ip] = mac
            self._persist(ip, mac)
        self._clear_pending(ip)

    def _clear_pending(self, ip: str) -> None:
        self._pending.pop(ip, None)
        self._pending_mac.pop(ip, None)
        # Recovery forgets this IP throttle/escalation state.
        for key in [k for k in self._last_alert if k[0] == ip]:
            del self._last_alert[key]

    def _persist(self, ip: str, mac: str) -> None:
        if self._store is None:
            return
        try:
            self._store.set(ip, mac)
        except Exception:
            _LOG.warning("gateway binding store write failed", exc_info=True)

    def _maybe_fire(self, ip: str, trusted: str, observed: str) -> None:
        now = self._now()
        key = (ip, observed)
        last = self._last_alert.get(key)
        if last is not None and (now - last) < self._throttle_s:
            return  # same (ip, rogue MAC) already alerted within the window.
        # First detection of THIS rogue identity -> alert; a re-fire after the
        # throttle window means it is STILL here -> critical (sustained).
        severity = SEVERITY_CRITICAL if last is not None else SEVERITY_ALERT
        self._last_alert[key] = now
        alert = GatewayAlert(
            ts=datetime.now(timezone.utc).isoformat(),
            gateway_ip=ip, trusted_mac=trusted, observed_mac=observed,
            severity=severity,
        )
        self._alerts.append(alert)
        if len(self._alerts) > self._max_alerts:   # bounded ring
            self._alerts = self._alerts[-self._max_alerts:]
        if self._on_alert:
            try:
                self._on_alert(alert)
            except Exception:
                _LOG.warning("gateway monitor on_alert callback failed", exc_info=True)
