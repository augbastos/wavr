"""House status composer (Build A10 v0): the unified "esta tudo bem em casa?"
answer, fusing the NETWORK layer (rogue-device / rogue-DHCP / gateway-identity
alerts -- `wavr.netinventory_service` / `wavr.dhcp_monitor` / `wavr.gateway_monitor`,
already merged one-ladder by `wavr.api_inventory`'s GET /api/alerts) with the
PHYSICAL layer (Watch's A2 "unrecognized person in <room>" -- `wavr.watch` -- and
A4 "occupancy unusual for this hour" -- `wavr.occupancy_log`) into ONE honest
{status, score, reasons} signal with an evidence trail, recog.py-style.

DELIBERATELY DUMB (v0): this is pure composition/aggregation of signals that
ALREADY exist -- no new detection, no new heuristic, no ML. It reads whatever
each layer currently reports and ranks the worst thing it finds. Every reason
carries the layer that fired, a plain-English `what`, the SAME `wavr.alert_severity`
ladder every alert kind already rides, and a `ts` -- so a consumer (dashboard
panel, or B3's `get_house_status` MCP tool) can always show its work.

DERIVED ONLY: every field here is already exposed at existing egress points
(GET /api/alerts already returns kind/severity/vendor/extra_server/gateway_ip;
Watch's intrusion alerts already carry only room + counts, never geometry;
occupancy anomaly already carries only room + a probability). This module adds
NO new raw personal data -- it only re-ranks and captions data that already left
the box through those routes.

RECENCY, honestly: `wavr.netinventory_service` / `wavr.dhcp_monitor` /
`wavr.gateway_monitor` each keep a bounded RING of historical alerts, not a
still-active set (unlike Watch's `IntrusionAlertLog.active_rooms()`, which
self-clears the moment a room's unrecognized verdict clears). Treating every
ring entry as "currently wrong" would pin the banner at "alert" long after a
one-off rogue-DHCP blip resolved -- eroding exactly the banner trust this
composer exists to protect. So network reasons are windowed to the last
`window_minutes` (default 60): still-recent evidence counts, a week-old
resolved sighting quietly ages out of the LIVE house-status view (it remains
visible forever in the full GET /api/alerts history -- nothing is deleted).
Physical reasons need no such window: callers pass already-live evidence
(currently-active intrusion rooms, current-hour occupancy anomalies), so
composer that data is trusted as-is.

SCORE, honestly: NOT a fabricated composite risk number. `score` is exactly
`severity_rank(worst_reason) + 1` (1..5, 0 when there is nothing to report) --
the rank of the SINGLE worst piece of evidence found. A pile of five `note`-tier
reasons never outranks one real `alert`; noise cannot manufacture urgency.
"""
from __future__ import annotations

from datetime import datetime, timezone

from wavr.alert_severity import SEVERITY_ALERT, SEVERITY_NOTE, severity_rank

LAYER_NETWORK = "network"
LAYER_PHYSICAL = "physical"

STATUS_OK = "ok"
STATUS_NOTICE = "notice"
STATUS_ALERT = "alert"
# Not a severity between the others: a refusal to answer. Nothing is wrong AND
# nothing was watching, which is not the same as all clear. See
# `compose_house_status`'s "SILENCE, honestly".
STATUS_UNKNOWN = "unknown"

# See module docstring ("RECENCY, honestly"). An hour is generous enough that a
# genuinely still-live LAN issue (which keeps re-firing/staying edge-triggered
# in its own monitor) is never missed, short enough that a resolved blip does
# not haunt the banner for a whole day.
DEFAULT_NETWORK_WINDOW_MINUTES = 60.0

_ALERT_RANK = severity_rank(SEVERITY_ALERT)


def _parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _network_what(a: dict, kind: str) -> str:
    """Plain-English caption, reusing only fields GET /api/alerts already
    exposes (never a new raw field). Falls back to the bare kind name for a
    future alert kind this module has not been taught to caption yet -- an
    unrecognized `kind` must never crash the composer."""
    # The DATA goes in brackets, OUTSIDE the sentence literal.
    #
    # These were single f-strings — "unrecognized device on the network
    # (Microsoft)" — so the sentence a translating client looks up contained a
    # vendor name, an IP or a hostname. It therefore existed in no source file,
    # could never be in a catalogue, and stayed English on every screen. It
    # also made a household's network data into a translation key, which is the
    # same mistake the Space name made in three other places.
    #
    # Splitting the literal from the value fixes both at once: the sentence is
    # a constant a catalogue can hold, and the bracketed part passes through as
    # the data it always was.
    # Bound to a name first, not written adjacent to the f-string. Python
    # concatenates adjacent literals AT PARSE TIME, so
    # `("a sentence" f" ({x})")` folds into one f-string whose first constant is
    # `"a sentence ("` — the trailing bracket glued on, and the clean sentence
    # gone from the module's string table. Which is where a catalogue looks.
    if kind == "rogue_device":
        what = "unrecognized device on the network"
        return f"{what} ({a.get('vendor') or 'unknown vendor'})"
    if kind == "rogue_dhcp":
        what = "extra DHCP server offering on the LAN"
        return f"{what} ({a.get('extra_server', 'unknown')})"
    if kind == "gateway_identity":
        what = "router (gateway) identity changed"
        return f"{what} ({a.get('gateway_ip', 'unknown')})"
    return (kind or "network").replace("_", " ")


def _network_reasons(network_alerts, *, now: datetime, window_minutes: float) -> list[dict]:
    if not network_alerts or window_minutes <= 0:
        return []
    cutoff = now.timestamp() - window_minutes * 60.0
    reasons = []
    for a in network_alerts:
        ts = a.get("ts")
        try:
            if ts is None or _parse_ts(ts).timestamp() < cutoff:
                continue
        except (ValueError, TypeError):
            continue  # malformed ts (should never happen) never crashes the composer
        kind = a.get("kind", "network")
        reasons.append({
            "layer": LAYER_NETWORK,
            "kind": kind,
            "what": _network_what(a, kind),
            "severity": a.get("severity", SEVERITY_ALERT),
            "ts": ts,
        })
    return reasons


def _intrusion_reasons(intrusion_alerts) -> list[dict]:
    """Physical A2. Callers pass ONLY the alerts for currently-active rooms
    (e.g. the latest `wavr.watch.IntrusionAlert` per room in
    `IntrusionAlertLog.active_rooms()`) -- see module docstring. A `room` of None is
    the ROOM-AGNOSTIC house-level aggregate signal (someone unaccounted-for is in the
    house, spread across rooms so no single room's count betrays them); it is captioned
    without naming any room, geometry, or identity -- count-only, like the per-room
    case."""
    reasons = []
    for a in intrusion_alerts or []:
        d = a.to_dict() if hasattr(a, "to_dict") else dict(a)
        room = d.get("room")
        what = ("an unrecognized person is present in the house"
                if room is None else f"unrecognized person in {room}")
        reasons.append({
            "layer": LAYER_PHYSICAL,
            "kind": "intrusion",
            "what": what,
            "severity": d.get("severity", SEVERITY_ALERT),
            "ts": d.get("ts"),
        })
    return reasons


def _fall_reasons(fall_alerts) -> list[dict]:
    """Physical A9 (RESEARCH-GRADE, ADR-0003). Callers pass ONLY currently-active
    fall-suspected episodes (mirrors `_intrusion_reasons`' "active, not historical-ring"
    contract). Room + how-long only -- never a posture, position, or identity. The `what`
    caption folds in the honest disclaimer inline so a house-status consumer can never
    render this as a confirmed fall."""
    reasons = []
    for a in fall_alerts or []:
        d = a.to_dict() if hasattr(a, "to_dict") else dict(a)
        room = d.get("room")
        dur = d.get("duration_s")
        what = (f"possible fall/no-motion in {room}" + (f" (~{int(dur)}s)" if dur else "")
                + " -- research demonstration, not a medical device")
        reasons.append({
            "layer": LAYER_PHYSICAL,
            "kind": "fall_suspected",
            "what": what,
            "severity": d.get("severity", SEVERITY_ALERT),
            "ts": d.get("ts"),
        })
    return reasons


def _routine_reasons(routine_flags) -> list[dict]:
    """Physical A4. Callers pass ONLY rooms already confirmed
    `is_unusual()["unusual"] is True` (never the `None`/insufficient-data or
    `False` cases -- an honest "don't know" or "normal" is not a reason).
    Each entry is `{"room": ..., "ts": ...}`. Fixed at `note` severity: an
    occupancy anomaly is ambiguous and non-security by nature (unlike the LAN
    events above) -- worth a glance on the panel, never rendered as urgent as
    a confirmed rogue-DHCP/gateway event."""
    reasons = []
    for f in routine_flags or []:
        reasons.append({
            "layer": LAYER_PHYSICAL,
            "kind": "routine_anomaly",
            "what": f"{f.get('room')} occupancy is unusual for this hour",
            "severity": SEVERITY_NOTE,
            "ts": f.get("ts"),
        })
    return reasons


def compose_house_status(*, network_alerts=None, intrusion_alerts=None, fall_alerts=None,
                          routine_flags=None, now: datetime | None = None,
                          window_minutes: float = DEFAULT_NETWORK_WINDOW_MINUTES,
                          sources_checked=None) -> dict:
    """Fuse the already-existing signal sources into one
    `{status, score, reasons, ts}` verdict. Pure function -- no I/O, no
    background state; app.py gathers the inputs from the live monitors/logs and
    calls this on every GET /api/house-status. `fall_alerts` (A9,
    RESEARCH-GRADE) is additive/optional -- omitted entirely (unchanged shape)
    when None, same rule as every other optional input here. See the module
    docstring for the recency-window and score semantics.

    ## SILENCE, honestly (`sources_checked`)

    An empty `reasons` list has two causes that mean opposite things: every
    layer looked and found nothing, or no layer looked at all. Reported as
    `ok`, the dashboard says "Everything looks normal." and the
    `get_house_status` MCP tool lets an agent say "everything's fine" -- about
    a house with the network monitor off, no cameras, no Watch and no
    occupancy log. Nothing was wrong because nothing was watching. That is the
    single most damaging sentence this product can produce, and it is the same
    mistake the map's screen-reader summary once made by announcing "Empty
    home" while every camera was offline.

    So a caller may declare WHICH layers were actually able to report --
    `{"network", "physical"}` -- and with no reasons and no live layer the
    verdict is `unknown`, not `ok`. The returned `checked` list carries it, in
    keeping with this module's rule that a verdict always shows its work.

    Omitting the argument keeps the old behaviour exactly. That is deliberate
    rather than lazy: a caller that never says what it checked cannot be
    second-guessed here, and every existing caller and test predates the idea.
    app.py, which does know, passes it.
    """
    now = now or datetime.now(timezone.utc)
    reasons = (
        _network_reasons(network_alerts, now=now, window_minutes=window_minutes)
        + _intrusion_reasons(intrusion_alerts)
        + _fall_reasons(fall_alerts)
        + _routine_reasons(routine_flags)
    )
    reasons.sort(key=lambda r: r["ts"] or "")

    if not reasons:
        blind = sources_checked is not None and not sources_checked
        status, score = (STATUS_UNKNOWN if blind else STATUS_OK), 0
    else:
        worst_rank = max(severity_rank(r["severity"]) for r in reasons)
        score = worst_rank + 1  # 1..5 -- the rank of the single worst reason, see docstring
        status = STATUS_ALERT if worst_rank >= _ALERT_RANK else STATUS_NOTICE

    out = {
        "status": status,
        "score": score,
        "reasons": reasons,
        "ts": now.isoformat(),
    }
    if sources_checked is not None:
        out["checked"] = sorted(sources_checked)
    return out
