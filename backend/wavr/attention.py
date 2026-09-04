"""The things that need a person, in one place, ranked.

## Why this exists

Wavr can already tell you a camera is offline, that a phone wants to pair, that
a device appeared on the network and that a room lost its count. It tells you
each of those in a different screen, and a household is expected to visit six
places to find out whether anything is waiting for them.

Nobody does that. So the honest state of the product today is that actionable
conditions accumulate silently until somebody happens to open the right tab —
which is the same class of failure as a Core that died and never said so.

One list. Ranked. Every item names what happened, what it costs, and where to
go. If nothing is here, nothing needs a person, and that sentence has to be
trustworthy or the whole surface is noise.

## What belongs here, and what does not

**Actionable.** Something a person can DO changes it. "Kitchen radar is
offline" belongs; "the kitchen has no position-capable sensor" is a fact about
the Space, not a task, and it belongs on the coverage screen.

**Not a log.** An event that has already been handled leaves. An alert that
fires every thirty seconds appears once, as one item, with its count.

**Not a duplicate of severity.** A DEGRADED runtime state is a state, and it is
shown as a state. It appears here only when there is a specific thing to do
about it.

The rule that keeps this list short: **an item nobody can act on is a bug in
this module, not an item.** The moment this becomes a feed, people stop reading
it, and the one time it matters they will not look.

## Ranking

Blocking first, then things degrading what Wavr can do, then things that are
merely worth knowing. Within a band, the oldest first: a request that has been
waiting three days is more urgent than one from a minute ago, and sorting by
newest — the default everywhere — would bury it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

# Bands, worst first. The number is the sort key and the API contract; the name
# is what a person is shown.
BLOCKING = "blocking"       # Wavr cannot do its job until somebody acts
DEGRADED = "degraded"       # it works, less well, and a person can fix it
INFO = "info"               # worth knowing, nothing is broken

BANDS = (BLOCKING, DEGRADED, INFO)


@dataclass(frozen=True)
class Item:
    """One thing waiting for a person.

    `where` is a UI target rather than a URL: the same item is rendered by the
    dashboard, and one day by a phone and a panel, and a hard-coded path would
    be wrong in two of the three.
    """
    key: str
    band: str
    title: str
    detail: str = ""
    where: str = ""
    action: str = ""
    since: str = ""
    count: int = 1

    def to_dict(self) -> dict:
        return {"key": self.key, "band": self.band, "title": self.title,
                "detail": self.detail, "where": self.where,
                "action": self.action, "since": self.since,
                "count": self.count}


def _oldest_first(items):
    # Missing timestamps sort last rather than first: an item with no `since` is
    # not "from 1970", and putting it at the top would be an invented urgency.
    return sorted(items, key=lambda i: (BANDS.index(i.band), i.since or "9999"))


def collect(*, discoveries=(), pending_pairings=(), node_requests=(),
            coverage_rows=(), cameras_needing_url=(), alerts=(),
            update=None, now=None) -> list[Item]:
    """Everything waiting, from facts other modules already established.

    Every argument is optional and an absent one contributes nothing — a caller
    that cannot read pending pairings must not cause "nothing needs you" to be
    printed over three waiting requests, so a failure upstream should leave the
    argument out and the caller should say it could not check.
    """
    items: list[Item] = []

    # -- Somebody is waiting on a decision -----------------------------------
    for p in pending_pairings or ():
        # `requester_name` and `created_at` are what `PairApprovalManager
        # .list_pending()` actually emits. An earlier draft of this guessed
        # `device_name`/`created_ts` and would have rendered every request as
        # "A device wants to join" with no timestamp, which sorts wrong AND
        # reads wrong — a consumer expecting a shape the producer never
        # produces is the commonest bug in this codebase, so the field names
        # here are copied from the producer rather than assumed.
        name = str(p.get("requester_name") or "A device")
        items.append(Item(
            key=f"pairing:{p.get('request_id') or name}",
            band=BLOCKING,
            title=f"{name} wants to join",
            detail="It cannot do anything until you approve or deny it.",
            where="devices", action="Review",
            since=str(p.get("created_at") or "")))

    for n in node_requests or ():
        label = str(n.get("label") or n.get("node_id") or "A sensor node")
        items.append(Item(
            key=f"node:{n.get('request_id') or label}",
            band=BLOCKING,
            title=f"{label} is asking to be enrolled",
            detail="A sensor board is waiting to be let in.",
            where="devices", action="Review",
            since=str(n.get("created_ts") or "")))

    # -- Something Wavr can see is not working --------------------------------
    offline = [r for r in (coverage_rows or ())
               if str(r.get("health")) in ("offline", "silent", "failed")]
    for r in offline:
        room = str(r.get("room") or "")
        where_txt = f" in {room}" if room else ""
        items.append(Item(
            key=f"sensor:{r.get('sensor_id')}",
            band=DEGRADED,
            title=f"{_sensor_name(r)}{where_txt} is not reporting",
            detail=_what_it_costs(r),
            where="coverage", action="Fix",
            since=str(r.get("last_seen") or "")))

    for c in cameras_needing_url or ():
        name = str(c.get("name") or "A camera")
        items.append(Item(
            key=f"camera-url:{name}",
            band=DEGRADED,
            title=f"{name} needs its stream address",
            detail=("A camera's address carries its password, so it is never "
                    "part of a backup. Enter it again and this camera starts "
                    "watching."),
            where="devices", action="Add address"))

    # -- Something appeared -----------------------------------------------------
    for d in discoveries or ():
        if str(d.get("status") or "pending") != "pending":
            continue
        items.append(Item(
            key=f"discovery:{d.get('discovery_id')}",
            band=INFO,
            title=str(d.get("title") or "Wavr noticed something"),
            detail=_discovery_detail(d),
            where="discoveries", action="Review",
            since=str(d.get("first_seen") or "")))

    # -- Alerts, collapsed --------------------------------------------------
    #
    # An alert that fires every thirty seconds is ONE thing wrong, not eighty
    # things to read. Grouped by kind, with the count, and only the severities a
    # person is expected to do something about.
    grouped: dict[str, list] = {}
    for a in alerts or ():
        if str(a.get("severity") or "").lower() not in ("high", "critical"):
            continue
        grouped.setdefault(str(a.get("kind") or "alert"), []).append(a)
    for kind, group in grouped.items():
        first = group[0]
        items.append(Item(
            key=f"alert:{kind}",
            band=DEGRADED,
            title=str(first.get("title") or kind.replace("_", " ").capitalize()),
            detail=str(first.get("detail") or first.get("message") or ""),
            where="alerts", action="View",
            since=str(min((str(a.get("ts") or "") for a in group), default="")),
            count=len(group)))

    if update and update.get("behind") is True:
        items.append(Item(
            key="update",
            band=INFO,
            title="An update is available",
            detail=str(update.get("instructions") or
                       "Wavr never updates itself; this is how this install "
                       "updates."),
            where="system", action="How"))

    return _oldest_first(items)


def _sensor_name(row) -> str:
    """What a person calls it, never the id.

    `sensor_id` is very often the name an operator typed — which is exactly what
    the experience layer had to stop exposing. Here the audience IS the
    operator, so their own words are the right thing to show; this exists so
    that decision is deliberate rather than accidental.
    """
    modality = str(row.get("modality") or "sensor")
    name = str(row.get("sensor_id") or "").strip()
    return name or modality


def _what_it_costs(row) -> str:
    """What the household loses while this stays broken.

    "Offline" is a status. "You can no longer count people in the kitchen" is
    the reason to get up and fix it, and it is the sentence Wavr is uniquely
    able to write because it knows what each sensor was contributing.
    """
    level = str(row.get("precision_level") or "")
    room = str(row.get("room") or "this room")
    if level == "position":
        return (f"While it is down, Wavr can no longer place people within "
                f"{room} — presence and count may still work from other "
                f"sensors.")
    if level == "count":
        return (f"While it is down, Wavr may not be able to count people in "
                f"{room}.")
    return f"While it is down, Wavr has less evidence about {room}."


def _discovery_detail(d) -> str:
    detail = d.get("detail")
    if isinstance(detail, dict):
        room = detail.get("room") or detail.get("suggested_room")
        if room:
            return f"Seen in {room}."
    return "Wavr noticed this and would like you to decide."


def summarise(items) -> dict:
    """The shape a chip, a tray line or a badge renders.

    One place, so a count on the tray and a count in the header cannot disagree
    — and so "nothing needs you" is produced by the same code that produces
    "three things need you", rather than by a separate emptiness check that can
    drift from it.
    """
    items = list(items or ())
    by_band = {b: sum(1 for i in items if i.band == b) for b in BANDS}
    total = len(items)
    if not total:
        headline = "Nothing needs your attention"
    elif total == 1:
        headline = "1 thing needs your attention"
    else:
        headline = f"{total} things need your attention"
    return {
        "total": total,
        "blocking": by_band[BLOCKING],
        "degraded": by_band[DEGRADED],
        "info": by_band[INFO],
        "headline": headline,
        "items": [i.to_dict() for i in items],
    }
