"""Daily proactive digest (C2, project_wavr_agentic_home_mission /
DESIGN-external-connectors.md section 3.3): "house empty 09:00-18:00, 1 new
device, routine normal".

Two functions, deliberately split so the pure part is trivially unit-testable
and the egress part is a thin, gated router:

  * `compose_digest(...)` -- PURE / LOCAL, NO EGRESS. Reads only already-
    derived state: an `OccupancyLog` (via its PUBLIC `rooms()`/`timeline()`/
    `is_unusual()` API only -- never its private segment-walking internals),
    an already-composed `house_status` dict
    (`wavr.house_status.compose_house_status()`'s `{status, score, reasons}`),
    and two plain counts the caller already has (`alert_count`,
    `new_device_count`, e.g. from `merge_alerts()` / `device_meta` first-seen
    for the day). Never makes a network call, never touches `connector_store`.

  * `send_digest(...)` -- routes an already-composed digest through WHICHEVER
    notify sink is enabled: Telegram (`notify.telegram.make_telegram_send`)
    first, falling back to the existing ntfy sink (`notifier.make_notifier`)
    when Telegram is off/unconfigured. Gated, default-OFF: each injected sink
    already carries its OWN gate (Telegram's `send()` checks
    `connector_store.is_enabled("telegram")` internally; `ntfy_notify` is only
    ever non-None when the caller already built it from a configured
    `WAVR_NTFY_URL`) -- if the caller passes neither (nothing configured/
    enabled), this is a pure no-op and NOTHING is attempted.

PRIVACY (reuse the Watch-mode discipline, never geometry/identity): an "empty
window" is a WHOLE-HOUSE fact (all rooms unoccupied), never a per-person
claim; `routine_status` is a single house-wide word derived from
`is_unusual()`'s own honest tri-state (`True`/`False`/`None` ->
"unusual"/"normal"/"insufficient_data"), never a per-room probability trail;
counts are bare integers. Every field this module reads is already exposed at
an existing egress point (`/api/house-status`, `/api/occupancy/routine`,
`/api/alerts`) via `OccupancyLog`'s own public read methods -- this module
adds no new raw personal data, it only re-composes counts + house-level facts
that already left the box through those routes, same rule as
`wavr.house_status`'s own module docstring.

HONESTY -- the TIMING itself is sensitive, not just identity/geometry: "no
per-person data" is not the same as "harmless". A daily line reading "house
empty 09:00-18:00" is a predictable, recurring burglary-relevant SCHEDULE --
functionally a public statement of exactly when nobody is home -- even though
it names no person and shows no room-level geometry. That is true of the
underlying `/api/house-status`/`/api/occupancy/routine` egress this module
re-composes too, but `send_digest` makes it materially worse by pushing that
schedule proactively, unprompted, to wherever Telegram/ntfy delivers it (a
phone that can be lost, a chat that can be forwarded, a bot token that can
leak) -- a passive GET the household has to go fetch is a different exposure
than an unsolicited push. This is why `send_digest` is opt-in, default-OFF,
and gated behind the SAME `connector_store` kill-switch as every other
connector in this package: a user who enables Telegram/ntfy digests must be
making that push-schedule tradeoff deliberately, and any UI surface offering
this connector must say so plainly (not just "no identity data leaves").
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

_ALLOWED_FIELD_KEYS = frozenset({
    "alert_count", "new_device_count", "empty_windows", "routine_status",
    "house_status",
})


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _fmt_hhmm(ts: str) -> str:
    """Best-effort HH:MM rendering of an ISO timestamp for the human sentence
    (falls back to the raw string if it doesn't parse -- never raises)."""
    try:
        return _parse(ts).strftime("%H:%M")
    except (ValueError, TypeError):
        return ts


def _room_state_at(occupancy_log, room: str, at: datetime) -> bool | None:
    """Last known `occupied` state for `room` at time `at`, via the PUBLIC
    `timeline()` method only (never `OccupancyLog`'s private `routine()`
    segment machinery). `timeline(end=at, limit=1)` returns the single most
    recent row strictly before `at` (see `OccupancyLog.timeline`'s
    `ORDER BY ts DESC LIMIT ?` then reverse). `None` = no history for this
    room before `at` -- an honest "don't know" rather than assuming empty."""
    prior = occupancy_log.timeline(room=room, end=at.isoformat(), limit=1)
    return bool(prior[-1]["occupied"]) if prior else None


def house_empty_windows(occupancy_log, *, start: datetime, end: datetime,
                         rooms: list[str] | None = None) -> list[dict]:
    """Whole-house empty windows within `[start, end)`: intervals where EVERY
    room's last-known state was unoccupied. Built entirely from
    `OccupancyLog`'s public `rooms()`/`timeline()` -- reconstructs house-wide
    occupancy by merging each room's edge-triggered transitions in time order
    and tracking which rooms are currently occupied, mirroring (without
    duplicating) the seed-then-walk approach `OccupancyLog.routine()` already
    uses internally for its own per-room reconstruction.

    CONSERVATIVE ON UNKNOWNS: a room with no history before `start` is treated
    as OCCUPIED (not empty) for its unknown span -- the same "never fabricate
    a verdict" discipline as `OccupancyLog.is_unusual()`'s explicit `None`
    case: we would rather under-report an empty window than falsely claim the
    house was empty when we simply have no data for one room yet.

    Returns a list of `{"start": iso, "end": iso}`, oldest first. Empty list
    when there is no room history at all, or the house was never fully empty
    in the window."""
    rooms = list(rooms) if rooms is not None else occupancy_log.rooms()
    if not rooms:
        return []

    state: dict[str, bool] = {}
    for r in rooms:
        seen = _room_state_at(occupancy_log, r, start)
        state[r] = True if seen is None else seen  # unknown => conservatively occupied

    events: list[tuple[str, str, bool]] = []
    for r in rooms:
        events += [(e["ts"], r, bool(e["occupied"]))
                   for e in occupancy_log.timeline(room=r, start=start.isoformat(),
                                                    end=end.isoformat())]
    events.sort(key=lambda e: e[0])

    windows: list[dict] = []
    cur_start: datetime | None = start if not any(state.values()) else None
    for ts, r, occ in events:
        state[r] = occ
        house_occupied = any(state.values())
        t = _parse(ts)
        if not house_occupied and cur_start is None:
            cur_start = t
        elif house_occupied and cur_start is not None:
            windows.append({"start": cur_start.isoformat(), "end": t.isoformat()})
            cur_start = None
    if cur_start is not None:
        windows.append({"start": cur_start.isoformat(), "end": end.isoformat()})
    return windows


def _house_routine_status(occupancy_log, *, at: datetime,
                           rooms: list[str] | None = None) -> str:
    """House-wide 'normal' | 'unusual' | 'insufficient_data', from
    `OccupancyLog.is_unusual()` per room at `at`. 'unusual' if ANY room's
    current state is flagged unusual for this hour; 'insufficient_data' if NO
    room has enough routine history yet; else 'normal'. Aggregated to a single
    word -- never a per-room breakdown (the per-room detail stays exactly
    where `is_unusual()`/`/api/occupancy/routine` already expose it)."""
    rooms = list(rooms) if rooms is not None else occupancy_log.rooms()
    if not rooms:
        return "insufficient_data"
    any_trusted = False
    for r in rooms:
        occ = _room_state_at(occupancy_log, r, at)
        if occ is None:
            continue
        result = occupancy_log.is_unusual(r, occ, at=at)
        if result["unusual"] is None:
            continue
        any_trusted = True
        if result["unusual"]:
            return "unusual"
    return "normal" if any_trusted else "insufficient_data"


def compose_digest(*, occupancy_log=None, rooms: list[str] | None = None,
                    house_status: dict | None = None,
                    alert_count: int = 0, new_device_count: int = 0,
                    start: datetime | None = None, end: datetime | None = None,
                    now: datetime | None = None) -> dict:
    """Pure/local composer -- see module docstring. Returns
    `{"date": "YYYY-MM-DD", "text": "<one sentence>", "lines": [...],
    "fields": {...}}`. `fields` carries exactly the allowlisted keys in
    `_ALLOWED_FIELD_KEYS` (asserted by the test suite) -- the SAME data the
    sentence renders, never anything additional, so a caller/test never has
    to re-parse the human sentence to see what went in it.

    `start`/`end` default to the trailing 24h ending at `now`
    (`datetime.now(timezone.utc)` when `now` is omitted) -- "yesterday's
    digest". `occupancy_log`/`house_status` are optional: omitted entirely
    (fields default to `insufficient_data`/absent), same additive-optional
    rule as `wavr.house_status.compose_house_status`'s own inputs."""
    now = now or datetime.now(timezone.utc)
    end = end or now
    start = start or (end - timedelta(days=1))

    empty_windows: list[dict] = []
    routine_status = "insufficient_data"
    if occupancy_log is not None:
        empty_windows = house_empty_windows(occupancy_log, start=start, end=end, rooms=rooms)
        routine_status = _house_routine_status(occupancy_log, at=now, rooms=rooms)

    fields: dict = {
        "alert_count": int(alert_count),
        "new_device_count": int(new_device_count),
        "empty_windows": empty_windows,
        "routine_status": routine_status,
    }
    hs_status = (house_status or {}).get("status")
    if hs_status:
        fields["house_status"] = hs_status

    lines: list[str] = []
    if empty_windows:
        w = empty_windows[0]
        lines.append(f"house empty {_fmt_hhmm(w['start'])}-{_fmt_hhmm(w['end'])}")
    if new_device_count:
        lines.append(f"{new_device_count} new device" + ("" if new_device_count == 1 else "s"))
    lines.append(f"routine {routine_status}".replace("_", " "))
    if alert_count:
        lines.append(f"{alert_count} alert" + ("" if alert_count == 1 else "s"))
    if hs_status and hs_status != "ok":
        lines.append(f"house status: {hs_status}")

    # `lines` always has at least the routine-status entry above, so there is
    # no reachable "empty" case here -- every digest says SOMETHING about the
    # house's routine, even when everything else is quiet.
    text = ", ".join(lines)
    return {"date": start.date().isoformat(), "text": text, "lines": lines, "fields": fields}


def send_digest(digest: dict, *,
                 telegram_send: Callable[..., dict] | None = None,
                 ntfy_notify: Callable[[str], None] | None = None) -> dict:
    """Route an already-composed `digest` (see `compose_digest`) through
    whichever sink is enabled. `telegram_send` is the closure from
    `notify.telegram.make_telegram_send(store)` (already gated on
    `store.is_enabled("telegram")` internally) or a test double; `ntfy_notify`
    is the closure from `notifier.make_notifier(url)` (only ever passed when
    `WAVR_NTFY_URL` is configured -- see `notifier.py`'s own opt-in gate) or a
    test double. Neither is called unless passed AND (for Telegram) actually
    enabled -- if both are `None` this is a pure no-op, zero egress attempted.

    Falls back to ntfy only when Telegram reports NOT ok (disabled/
    unconfigured/error) -- so a fully-configured Telegram never double-sends
    through ntfy too. Returns `{"ok": bool, "status": str, "via": "telegram"|
    "ntfy"|None}`.

    This is the function that turns the module docstring's "HONESTY" note
    into an actual push: whatever `digest["text"]` says (including any
    "house empty HH:MM-HH:MM" line) leaves the box unprompted the moment a
    caller enables Telegram or ntfy -- see that note before wiring this into
    a scheduler."""
    text = digest.get("text", "")
    if telegram_send is not None:
        result = telegram_send(kind="digest", severity="note", room=None, summary=text)
        if result.get("ok"):
            return {"ok": True, "status": result.get("status"), "via": "telegram"}
    if ntfy_notify is not None:
        ntfy_notify(f"Wavr daily digest: {text}")
        return {"ok": True, "status": "sent", "via": "ntfy"}
    return {"ok": False, "status": "no_enabled_connector", "via": None}
