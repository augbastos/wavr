"""Presence report -- pure LOCAL aggregation of ``wavr.device_meta`` (Feature
A's first_seen/last_seen store) into a human-useful "who's around" summary.
NO new scanning, NO cloud, NO network I/O: this module only reads whatever
``DeviceMeta`` already persisted from existing scanners.

Honest limitation (read the data model before trusting this shape): each MAC
in ``device_meta`` keeps exactly ONE first_seen and ONE last_seen timestamp,
overwritten on every sighting -- there is no per-scan history log. That means
this module CANNOT reconstruct a true per-day occupancy timeline (a proprietary scanner's
"monthly report"); what it CAN honestly derive, right now, from that snapshot:

  * which devices look currently present (last seen within ``active_window_s``
    of "now" -- a proxy for "on the LAN right now"),
  * which look recently away / gone stale (last seen longer ago),
  * house-wide first/last activity ever recorded,
  * a house-wide "quiet period" = time since ANY device was last seen at all
    (a cheap away/occupied proxy -- long quiet_period_seconds ~= empty house),
  * a "most present" ranking by tenure (last_seen - first_seen) among devices
    currently present -- the longer a device has continuously been seen AND
    still looks present, the more it reads as "a resident", not a visitor.

A future stage that wants real per-day windows needs a periodic snapshot log
(e.g. append-only samples), which is a bigger, separate change -- not layered
onto this pure-aggregation module.
"""
from __future__ import annotations

from datetime import datetime, timezone

from wavr.device_meta import DeviceMeta

# Defaults chosen for a home-LAN context, not wired to any WAVR_ env flag here
# -- the integrate step decides whether/how these become configurable.
DEFAULT_ACTIVE_WINDOW_S = 15 * 60        # "seen in the last 15min" = present
DEFAULT_STALE_AFTER_S = 7 * 24 * 3600    # "not seen in 7 days" = stale/gone
DEFAULT_TOP_N = 5


def _parse(ts: str | None) -> datetime | None:
    """Best-effort ISO-8601 parse of a device_meta timestamp. Returns None on
    anything missing/malformed rather than raising -- one bad row must never
    take down the whole report."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _age_seconds(now: datetime, ts: datetime) -> float:
    """Seconds elapsed since ``ts``, clamped to >=0 -- defensive against clock
    skew (a last_seen slightly "in the future" reads as 0, not negative)."""
    return max(0.0, (now - ts).total_seconds())


def build_report(
    meta: DeviceMeta,
    *,
    now: datetime | None = None,
    active_window_s: float = DEFAULT_ACTIVE_WINDOW_S,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
    top_n: int = DEFAULT_TOP_N,
) -> dict:
    """Build the presence summary. Pure/offline: ``meta`` is read once via
    ``DeviceMeta.all()`` and everything else is arithmetic on the timestamps
    already stored -- no scan, no I/O, safe to call on every GET.

    Devices with no parseable last_seen (e.g. named/pinned via the API but
    never actually sighted by a scanner) are counted in ``device_count`` but
    excluded from every time-bucketed list -- there's nothing honest to say
    about their presence yet.
    """
    now = now or datetime.now(timezone.utc)
    entries = meta.all()

    currently_present: list[dict] = []
    recently_away: list[dict] = []
    stale: list[dict] = []
    first_seen_all: list[datetime] = []
    last_seen_all: list[datetime] = []

    for mac, row in entries.items():
        first_dt = _parse(row.get("first_seen"))
        last_dt = _parse(row.get("last_seen"))
        if first_dt is not None:
            first_seen_all.append(first_dt)
        if last_dt is None:
            continue
        last_seen_all.append(last_dt)

        age = _age_seconds(now, last_dt)
        item = {
            "mac": mac,
            "name": row.get("name"),
            "device_type": row.get("device_type"),
            "last_seen": row.get("last_seen"),
            "quiet_for_seconds": round(age, 1),
        }
        if age <= active_window_s:
            item.pop("quiet_for_seconds")
            tenure = None
            if first_dt is not None:
                tenure = round(_age_seconds(last_dt, first_dt), 1)
            currently_present.append({**item, "tenure_seconds": tenure,
                                       "first_seen": row.get("first_seen")})
        elif age >= stale_after_s:
            stale.append(item)
        else:
            recently_away.append(item)

    currently_present.sort(key=lambda d: d["mac"])
    recently_away.sort(key=lambda d: (-d["quiet_for_seconds"], d["mac"]))
    stale.sort(key=lambda d: (-d["quiet_for_seconds"], d["mac"]))

    most_present = sorted(
        (d for d in currently_present if d["tenure_seconds"] is not None),
        key=lambda d: (-d["tenure_seconds"], d["mac"]),
    )[:max(0, top_n)]

    first_activity_at = min(first_seen_all).isoformat() if first_seen_all else None
    last_activity_at = max(last_seen_all).isoformat() if last_seen_all else None
    quiet_period_seconds = (
        round(_age_seconds(now, max(last_seen_all)), 1) if last_seen_all else None
    )

    return {
        "generated_at": now.isoformat(),
        "device_count": len(entries),
        "first_activity_at": first_activity_at,
        "last_activity_at": last_activity_at,
        "quiet_period_seconds": quiet_period_seconds,
        "currently_present": currently_present,
        "recently_away": recently_away,
        "stale": stale,
        "most_present": most_present,
    }
