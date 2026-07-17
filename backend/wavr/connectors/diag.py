"""Diagnostics-report egress ("send diagnosis") — the ONLY module that may ship a
net_doctor report off the LAN.

Privacy contract (Augusto, 2026-07-17 — "privacy first mas sempre com opt-in não
forçado"): Wavr never sends a diagnostic anywhere on its own. Two consent shapes,
both defaulting to NOTHING leaving:

  * MANUAL — the loopback admin taps "Send report". The tap itself is the per-action
    consent, so this path does NOT require the `diagnostics` connector row to be on
    (`manual=True` below). It is only reachable from the require_local admin route.
  * AUTOMATIC — the `diagnostics` connector toggle (registry, default OFF) is the
    STANDING consent: while on, a diagnosis that finds a problem reports home by
    itself. Flipping the toggle off stops it on the very next call (guarded_call's
    revocability contract).

Both paths additionally sit behind the system egress master (checked at the route /
call site) and both re-redact the report (net_doctor.redact_macs) immediately before
transmission — defense in depth: even a caller handing us a raw-MAC string cannot
leak one. The payload is the redacted report text + schema version, nothing else —
no device identity, no house map, no credentials.
"""
from __future__ import annotations

from wavr.connector_store import ConnectorStore
from wavr.connectors.http import post_json
from wavr.net_doctor import redact_macs

# Hard cap on what we will ever transmit — a report is ~1-3 KB; anything huge is
# not a report and gets truncated rather than shipped.
MAX_REPORT_BYTES = 64 * 1024


def send_report(store: ConnectorStore, endpoint: str, report: str,
                *, manual: bool = False, transport=post_json) -> dict:
    """Ship one MAC-redacted report to `endpoint`. Returns {"ok": bool, "status": ...};
    never raises (an unreachable diagnostics endpoint must never break the doctor).

    Gate: `manual=True` (explicit admin tap — per-action consent) OR the
    `diagnostics` registry toggle (standing consent). Read fresh on every call, so
    turning the toggle off stops the next automatic send immediately.
    """
    if not endpoint:
        return {"ok": False, "status": None, "reason": "no endpoint configured"}
    if not manual and not store.is_enabled("diagnostics"):
        return {"ok": False, "status": None, "reason": "diagnostics connector off"}
    clean = redact_macs(str(report))[:MAX_REPORT_BYTES]
    try:
        # post_json returns the endpoint's parsed JSON body and RAISES on transport
        # failure / HTTP error / non-JSON — so reaching the next line means delivered.
        # Custom UA: Cloudflare's default Browser Integrity Check 403s the stdlib
        # "Python-urllib/x.y" agent (proven live 2026-07-17), so we identify honestly.
        transport(endpoint, {"schema": 1, "report": clean},
                  headers={"User-Agent": "wavr-diag/1"})
        return {"ok": True, "status": 200}
    except Exception as exc:  # noqa: BLE001 — bare transport raises; we degrade clean
        return {"ok": False, "status": None, "reason": str(exc)[:200]}
