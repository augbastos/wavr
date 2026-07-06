"""Live IP<->paired-device correlation for the "IDENTIFIED on its network host"
overlay (blueprint item 4, privacy centerpiece).

A paired phone that (a) is GREEN-consented and (b) is actively POSTing telemetry
reveals its OWN LAN IP to the server as the TCP peer of `POST /api/telemetry`
(`request.client.host` -- the real peer, NEVER an X-Forwarded-For header). This
module correlates that live IP back to the device's friendly name so Wavr Net can
show the operator "this host on my network is <name>'s phone" -- but ONLY while the
correlation is simultaneously fresh, still-consented, unambiguous, and MAC-consistent.

HARD PRIVACY BOUNDARIES (do not weaken -- these gate the whole feature):

  * IN-MEMORY ONLY. `PairedHostBinder` holds a plain dict keyed by device_id and is
    NEVER persisted -- no device<->MAC/name table, no sqlite, nothing on disk. A
    process restart forgets every binding. The source_ip that feeds it is used ONLY
    here; it never lands on a TelemetryReading, a SensingEvent, or a log line.

  * SUBTRACTIVE / FAIL-CLOSED. `resolve()` emits a name for an IP ONLY when ALL of:
      (a) FRESH        -- the device POSTed within `freshness_s` (fusion's window).
      (b) still GREEN  -- consent re-checked AT READ TIME (never trusting a stale
                          record); unknown / None / non-green -> excluded. This is the
                          GDPR-red backstop for a device that SILENTLY withdrew and
                          simply stopped POSTing -- withdrawal must not wait for a
                          later POST to take effect.
      (c) UNAMBIGUOUS  -- exactly ONE green device claims that IP; a collision (two
                          green devices, one IP after a DHCP churn) emits NO binding.
      (d) MAC-CONSISTENT -- the IP's CURRENT MAC (from the live LAN inventory) equals
                          the MAC captured when the binding was recorded. This defeats
                          a DHCP-reassign: if a DIFFERENT device now answers at that IP,
                          the name is withheld rather than pinned onto the new host.

  * EVAPORATES. `record()` overwrites a device's own entry (so a DHCP move re-homes it
    and frees the old IP on the next POST), `drop()` evicts on yellow/red/withdraw, and
    `_prune()` (reusing fusion's freshness window -- no second timer) forgets anything
    stale. yellow and red both DROP the binding: a yellow (anonymous) device votes
    presence but is never NAMED, and red is gone entirely.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

# Reuse the SAME freshness window fusion decays against -- never a second timer.
from wavr.fusion import _DEFAULT_FRESHNESS_S


def _norm_mac(mac: str | None) -> str | None:
    """Lowercase colon-form MAC for comparison (accepts '-' or ':' separators, the
    same convention as netinventory/device_meta). None/empty -> None so a missing
    MAC can never accidentally compare-equal to another missing MAC."""
    if not mac:
        return None
    cleaned = mac.strip().replace("-", ":").lower()
    return cleaned or None


class PairedHostBinder:
    """In-memory device_id -> (ip, mac, last_seen) map. NEVER persisted.

    `get_label(device_id) -> str | None` resolves the friendly name (DeviceStore.get_label);
    `get_consent(device_id) -> str | None` resolves the live consent tier
    (DeviceStore.get_consent). Both are injected so the binder itself opens no DB and,
    when they are None (single-device / no-consent build), the binder is inert:
    `_is_green` is fail-closed, so `resolve()` yields nothing.
    """

    def __init__(self, get_label: Callable[[str], str | None] | None = None,
                 get_consent: Callable[[str], str | None] | None = None,
                 freshness_s: float | None = None):
        self._get_label = get_label
        self._get_consent = get_consent
        self._freshness_s = _DEFAULT_FRESHNESS_S if freshness_s is None else freshness_s
        # device_id -> (ip, mac, last_seen). The ONLY state; RAM only, never on disk.
        self._entries: dict[str, tuple[str | None, str | None, datetime]] = {}

    # -- producer side (called from the consent-gated telemetry chokepoint) -----------

    def record(self, device_id: str, ip: str | None, mac: str | None,
               now: datetime) -> None:
        """Overwrite this device's binding with its current (ip, mac, now). Overwrite --
        not append -- makes a DHCP move safe: the device's own entry MOVES to the new IP
        and the old IP is freed on this very call, so a device is never bound to two IPs.
        Prunes on write so stale entries evaporate even without a read."""
        self._entries[device_id] = (ip, mac, now)
        self._prune(now)

    def drop(self, device_id: str) -> None:
        """Evict a device's binding (yellow/red/withdrawal). Idempotent."""
        self._entries.pop(device_id, None)

    # -- housekeeping -----------------------------------------------------------------

    def _fresh(self, last_seen: datetime, now: datetime) -> bool:
        return (now - last_seen).total_seconds() <= self._freshness_s

    def _prune(self, now: datetime) -> None:
        """Forget bindings older than the freshness window (fusion's window -- no new
        timer). This is how a stopped device un-names itself in RAM."""
        self._entries = {
            device_id: entry for device_id, entry in self._entries.items()
            if self._fresh(entry[2], now)
        }

    def _is_green(self, device_id: str) -> bool:
        """Fail-closed live consent check: True ONLY when the injected resolver reports
        exactly 'green'. No resolver, an unknown/None tier, or a raising resolver -> False
        (never named). This is the read-time GDPR-red backstop."""
        if self._get_consent is None:
            return False
        try:
            return self._get_consent(device_id) == "green"
        except Exception:
            return False

    # -- consumer side (view-time overlay + rogue-suppression predicate) --------------

    def resolve(self, now: datetime,
                current_mac_of_ip: Callable[[str], str | None],
                is_green: Callable[[str], bool] | None = None) -> dict[str, str]:
        """Return {ip: label} for every host that CURRENTLY satisfies all four gates
        (fresh, green, unambiguous, MAC-consistent). `current_mac_of_ip(ip)` supplies the
        live MAC observed at that IP by the LAN inventory this instant. `is_green` defaults
        to the binder's own get_consent-derived predicate; a caller (a unit test) may inject
        an explicit predicate. Fail-closed throughout: any gate not met -> the IP is absent
        from the result (no name)."""
        green = is_green if is_green is not None else self._is_green
        self._prune(now)

        # Group the fresh + still-green bindings by their recorded IP so an IP claimed by
        # more than one green device can be detected and dropped (constraint c).
        by_ip: dict[str, list[tuple[str, str | None]]] = {}
        for device_id, (ip, mac, last_seen) in self._entries.items():
            if ip is None or not self._fresh(last_seen, now):
                continue
            if not self._safe_green(green, device_id):
                continue
            by_ip.setdefault(ip, []).append((device_id, mac))

        result: dict[str, str] = {}
        for ip, owners in by_ip.items():
            if len(owners) != 1:
                continue                     # ambiguous IP -> emit NO binding
            device_id, recorded_mac = owners[0]
            if not self._mac_consistent(recorded_mac, current_mac_of_ip, ip):
                continue                     # MAC-consistency guard (constraint d)
            label = self._get_label(device_id) if self._get_label else None
            if not label:
                continue                     # no name -> cannot "IDENTIFY" -> fail-closed
            result[ip] = label
        return result

    @staticmethod
    def _safe_green(green: Callable[[str], bool], device_id: str) -> bool:
        try:
            return bool(green(device_id))
        except Exception:
            return False

    @staticmethod
    def _mac_consistent(recorded_mac: str | None,
                        current_mac_of_ip: Callable[[str], str | None],
                        ip: str) -> bool:
        """The IP's CURRENT MAC must match the MAC captured at record time. Both must be
        present and equal -- a missing recorded MAC or a missing/None current MAC withholds
        the name (fail-closed), so a freshly-reassigned IP with an unknown occupant is never
        misnamed."""
        want = _norm_mac(recorded_mac)
        if want is None:
            return False
        try:
            have = _norm_mac(current_mac_of_ip(ip))
        except Exception:
            return False
        return have is not None and have == want
