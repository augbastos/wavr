"""Watch / Guard mode ("Vigia") -- the privacy-graduated thesis as a feature.

Watch is an ORTHOGONAL privacy axis layered on the Off/Presence/Precise sensing
ladder (not a fourth mutually-exclusive tier): a single server-side toggle that,
while ON, performs a deliberate PRIVACY INVERSION -- detect an UNRECOGNIZED person
WITHOUT surveilling the family. While active:

  * every per-person geometry/identity/biometric field is SUPPRESSED from state and
    from every egress (dashboard/WS, /api/state, MQTT, narrator); and
  * only COUNTS + the intrusion ROOM + entry/exit edges are allowed to leave.

Intrusion is inferred purely from counts at TWO scopes: (1) a ROOM whose honest
person_count (A1, from a counting-capable source camera/mmwave) exceeds the number
of KNOWN people present (the consent identity layer) MUST contain an unaccounted
person; and (2) the HOUSE as a whole -- when the honest SUM of per-room counts
exceeds the known-present count even though NO single room's count does (a spread-out
intrusion the per-room test alone would miss). Both are surfaced count-only: the room
signal room-level (never WHO or WHERE-in-the-room), the house signal room-AGNOSTIC
(never even which room, only THAT someone unaccounted-for is present).

SUPPRESSED_FIELDS, project_state, room_unrecognized, house_unrecognized and
known_present_persons live here ONCE so the suppression can never drift between egress
points. HONESTY GATE:
intrusion detection needs the identity layer to know who is known; with identity
disabled the KNOWN set is empty, so the caller gates on identity_enabled -- Watch
still suppresses geometry (fail-safe toward MORE privacy) but reports it cannot flag
an intruder rather than declaring the whole family unknown.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone

from wavr.alert_severity import SEVERITY_ALERT

# Debounce threshold for edge-triggered intrusion alerts (the most privacy-
# sensitive push Wavr sends -- "unrecognized person in <room>"/"in casa").
# `IntrusionAlertLog.record` is called on EVERY fusion publish -- every camera
# frame (~0.5s) or mmwave reading (~0.2s), see wavr.app._ingest -- so comparing
# the raw per-frame verdict with ZERO dwell means one noisy frame (motion blur, a
# miscount, a pet crossing the lens) fires the alert.
#
# RISING EDGE (audit fix -- was a strict consecutive-streak reset, too brittle for
# the security path): this many "present" (`unrecognized=True`) observations must
# accumulate within a LEAKY sliding window (`IntrusionAlertLog._window_size`, ~2x
# this value -- mirrors `RogueDhcpMonitor`'s leaky N-of-M window, see
# wavr.dhcp_monitor's module docstring) before the edge is allowed to fire. A
# single absent (`unrecognized=False`) observation no longer wipes ALL prior
# progress back to zero -- a real but FLICKERING intruder (partial occlusion, a
# camera riding the detection threshold) still accumulates toward this count
# instead of a strict consecutive-streak reset silently and permanently starving
# it of a fire (a security false-negative). A single one-off blip still does not
# alert: it never reaches this many "present" observations before falling out of
# the window.
#
# CLEARING (decoupled from the rising edge -- audit fix): once CONFIRMED
# (`_active`), this many CONSECUTIVE `unrecognized=False` observations are
# required before the room de-arms -- a single recognized/false-negative frame
# mid-intrusion must NOT immediately clear an ongoing, still-real intrusion and
# let it double-alert on the very next flicker back to True.
DEFAULT_INTRUSION_DEBOUNCE = 2

# Per-person fields carrying geometry (targets x/y), identity (identities who-is-home
# labels = PII) or biometrics (vitals breathing/heart): MUST NOT leave the box while
# Watch is active. Mirrors the set wavr.mcp.get_room_context strips unconditionally,
# so the two egress suppressions can never disagree. ONE definition, never re-listed.
SUPPRESSED_FIELDS = ("targets", "identities", "vitals")
_EMPTY = {"targets": [], "identities": [], "vitals": {}}


class WatchMode:
    """Server-side, in-memory Watch (Vigia) toggle. DEFAULT OFF -- privacy-first boot,
    same posture as camera sources. The ONE holder every egress projector reads, so
    the mode is atomic across dashboard, WS, MQTT and narrator."""

    def __init__(self, on: bool = False):
        self._on = bool(on)

    @property
    def on(self) -> bool:
        return self._on

    def set(self, on: bool) -> bool:
        self._on = bool(on)
        return self._on


def _idents(state) -> list:
    v = state.get("identities") if isinstance(state, dict) else getattr(state, "identities", None)
    return v or []


def known_present_persons(states) -> set:
    """DISTINCT known person labels currently present, deduped across rooms. Empty
    whenever the identity layer is off (identities never populate) -- so with identity
    disabled Watch cannot claim anyone is known, which is why the caller must gate
    intrusion detection on identity_enabled (else every counted person reads as
    unknown and Watch would flag the whole family)."""
    persons = set()
    for s in states:
        for ident in _idents(s):
            p = ident.get("person") if isinstance(ident, dict) else getattr(ident, "person", None)
            if p:
                persons.add(p)
    return persons


def room_unrecognized(state, known_count: int) -> bool:
    """True when a room DEFINITELY holds an unrecognized person: its own person_count
    exceeds the house-wide count of KNOWN-present people. Even if every known person
    were in this single room, the surplus can only be someone we cannot account for
    -- an honest, room-scoped signal that says THAT a room has an unknown, never WHO
    or WHERE-in-the-room (no geometry used or revealed). A None person_count (no
    counting source here) NEVER fires. known_count below 0 is clamped to 0."""
    pc = state.get("person_count") if isinstance(state, dict) else getattr(state, "person_count", None)
    if pc is None:
        return False
    return int(pc) > max(0, int(known_count))


def house_unrecognized(house_count, known_count: int) -> bool:
    """True when the HOUSE AS A WHOLE definitely holds an unrecognized person: the
    honest sum of per-room person_count (wavr.fusion.house_person_count -- counting-
    capable rooms only, None when NO room can count) exceeds the house-wide count of
    KNOWN-present people. The AGGREGATE backstop to room_unrecognized: it catches a
    SPREAD-OUT intrusion that no single room reveals -- several unaccounted people
    split across rooms so no one room's own count exceeds the known-count, yet the
    TOTAL surplus betrays them.

    ROOM-AGNOSTIC + count-only: asserts only THAT someone unaccounted-for is in the
    house, never WHO and never WHERE (no room, no geometry, no identity used or
    revealed -- only two integers, the honest total and the known count). A None
    house_count (a fully-UNCOUNTED house, no counting source anywhere) NEVER fires:
    an honest "unknown", never asserted as "safe". known_count below 0 clamps to 0.
    Gate on identity_enabled at the caller exactly as room_unrecognized needs -- with
    identity off the known set is empty and the whole house would read unaccounted."""
    if house_count is None:
        return False
    return int(house_count) > max(0, int(known_count))


def project_state(state: dict, watch_on: bool, unrecognized: bool = False) -> dict:
    """The privacy INVERSION, applied at every egress. When Watch is OFF this is the
    identity map -- the input dict is returned UNCHANGED, so Off/Presence/Precise
    behave byte-identically to before. When ON it returns a NEW dict (never mutates
    the callers internal truth) with every SUPPRESSED_FIELDS entry emptied -- family
    positions, identities and vitals do not leave -- while count-only derived fields
    (occupied / confidence / person_count / per-source presence+count / explanation)
    remain, plus watch=true and the room-level unrecognized flag."""
    if not watch_on:
        return state
    out = {k: v for k, v in state.items() if k not in SUPPRESSED_FIELDS}
    for f in SUPPRESSED_FIELDS:
        out[f] = list(_EMPTY[f]) if isinstance(_EMPTY[f], list) else dict(_EMPTY[f])
    out["watch"] = True
    out["unrecognized"] = bool(unrecognized)
    return out


class IntrusionAlert:
    """One edge-triggered unrecognized-person event. Count-only: carries the
    person_count that tripped it and the known-present count compared against --
    NEVER a target position, identity, or geometry. `room` is the room name for a
    per-room signal, or None for the ROOM-AGNOSTIC house-level aggregate (someone
    unaccounted-for is in the house, spread so no single room reveals them -- never
    even which room). Severity fixed at alert (serious, on the shared ladder) -- high,
    but NOT critical (the ladder reserves critical for a sustained/confirmed gateway
    change)."""

    __slots__ = ("room", "person_count", "known_present", "severity", "ts")

    def __init__(self, room, person_count, known_present, ts):
        self.room = room
        self.person_count = person_count
        self.known_present = known_present
        self.severity = SEVERITY_ALERT
        self.ts = ts

    def to_dict(self) -> dict:
        return {
            "kind": "intrusion",
            "severity": self.severity,
            "room": self.room,
            "person_count": self.person_count,
            "known_present": self.known_present,
            "ts": self.ts,
        }


class IntrusionAlertLog:
    """Edge-triggered ring of intrusion alerts, mirroring the recent_alerts()/
    to_dict() shape of the rogue-device / rogue-DHCP / gateway-identity monitors so
    /api/alerts merges it with ZERO special-casing (one severity ladder, one stream).
    Per room an alert fires ONCE on the clear-to-flagged edge and re-arms only after
    a SUSTAINED clear, so a lingering (or flickering) person never spams the stream.
    Bounded ring.

    DEBOUNCED (`debounce`, default `DEFAULT_INTRUSION_DEBOUNCE`) -- security-path
    audit fix: a strict CONSECUTIVE streak (any single `unrecognized=False` wiping
    all prior progress) was too brittle for this path, with two reproduced
    consequences -- see the module-level comment on `DEFAULT_INTRUSION_DEBOUNCE`
    for the full rationale:

      * RISING edge (not-yet-`_active` rooms): a LEAKY N-of-M window (mirrors
        `RogueDhcpMonitor`'s leaky window, see wavr.dhcp_monitor) -- `debounce`
        "present" (`unrecognized=True`) observations must accumulate within the
        last `_window_size` (~2x `debounce`) `record()` calls for the room. A
        single absent observation no longer resets the count to zero, so a real
        but FLICKERING intruder (partial occlusion, a camera riding the detection
        threshold -- alternating True/False) still confirms instead of a strict
        streak reset silently starving it of ever firing (a security
        false-negative). A lone one-off blip still never alerts: it can't reach
        `debounce` "present" observations before aging out of the window.
      * CLEARING an already-`_active` (CONFIRMED) room is DECOUPLED from the
        rising-edge window: it takes `debounce` CONSECUTIVE `unrecognized=False`
        observations to de-arm -- any `unrecognized=True` in between resets the
        clear attempt to zero (the intrusion is evidently still ongoing). A
        single false-negative frame mid-confirmed-intrusion can therefore never
        immediately de-arm and let the very next flicker back to True
        double-alert an intrusion that never actually left."""

    def __init__(self, max_alerts: int = 50, now_fn=None,
                debounce: int = DEFAULT_INTRUSION_DEBOUNCE):
        self._alerts = []
        self._active = set()   # rooms CONFIRMED (edge already fired, not yet de-armed)
        self._debounce = max(1, int(debounce))   # never 0 -- that would mean "never fires"
        # Leaky N-of-M window size for the rising edge (mirrors
        # RogueDhcpMonitor._window_size) -- big enough that a 50%-duty-cycle
        # flicker still accumulates `debounce` "present" observations within it.
        self._window_size = max(2 * self._debounce, 2)
        self._windows = {}       # room -> deque[bool] of recent unrecognized verdicts, not yet armed
        self._clear_streak = {}  # room -> consecutive unrecognized=False count, only while active
        self._max = max_alerts
        self._now_fn = now_fn

    def _now_iso(self) -> str:
        if self._now_fn is not None:
            return self._now_fn()
        return datetime.now(timezone.utc).isoformat()

    def record(self, room, unrecognized, person_count, known_present, ts=None):
        """Fold one room current unrecognized verdict into the edge state. Returns a
        NEW IntrusionAlert only on a clear-to-flagged transition, AFTER `debounce`
        "present" observations have accumulated in the room's leaky rising-edge
        window (else None -- either genuinely clear, still accumulating, or already
        confirmed so the edge already fired)."""
        if room in self._active:
            # Already CONFIRMED: de-arming is decoupled from the rising-edge window
            # (see class docstring) -- a single unrecognized=False must not clear an
            # ongoing intrusion outright. Any unrecognized=True resets the in-progress
            # clear attempt (still evidently ongoing); only `debounce` CONSECUTIVE
            # unrecognized=False de-arms the room, ready for a fresh rising edge.
            if unrecognized:
                self._clear_streak.pop(room, None)
                return None
            streak = self._clear_streak.get(room, 0) + 1
            if streak < self._debounce:
                self._clear_streak[room] = streak
                return None
            self._clear_streak.pop(room, None)
            self._active.discard(room)
            self._windows.pop(room, None)   # fresh leaky window for the next rising edge
            return None

        # Not yet confirmed -- leaky N-of-M window: a `True` credits the window, a
        # `False` does NOT wipe prior progress back to zero (unlike the old strict
        # streak), so a sustained-but-flickering intruder still accumulates toward
        # `debounce` instead of resetting on its very first missed frame.
        window = self._windows.setdefault(room, deque(maxlen=self._window_size))
        window.append(bool(unrecognized))
        if not unrecognized and sum(window) == 0:
            # Never made any progress and has now fallen fully quiet -- forget it
            # (bounds memory for a one-off blip rather than tracking it forever).
            del self._windows[room]
            return None
        if sum(window) < self._debounce:
            return None   # sustained-but-not-yet-enough progress -- no alert yet
        self._windows.pop(room, None)
        self._active.add(room)
        alert = IntrusionAlert(room, int(person_count) if person_count is not None else 0,
                               max(0, int(known_present)), ts or self._now_iso())
        self._alerts.append(alert)
        if len(self._alerts) > self._max:
            self._alerts = self._alerts[-self._max:]
        return alert

    def reset(self) -> None:
        """Clear the per-room edge state AND any in-progress rising/clearing
        evidence (NOT the alert ring), so that after Watch is turned off and on
        again a still-present intrusion is free to re-arm (and, once it rebuilds a
        fresh window, re-fire) rather than being swallowed as already-flagged or
        credited stale evidence from before Watch was off."""
        self._active.clear()
        self._windows.clear()
        self._clear_streak.clear()

    def recent_alerts(self, limit: int = 50):
        return self._alerts[-limit:]

    def active_rooms(self) -> set:
        return set(self._active)

    def active_alerts(self) -> list:
        """The latest IntrusionAlert for each CURRENTLY-flagged room (Build A10:
        `wavr.house_status`'s physical-layer input) -- unlike `recent_alerts()`'s
        full historical ring, a room whose unrecognized verdict already cleared
        (dropped out of `_active`) is NEVER included here, so a resolved
        intrusion cannot pin a downstream "is everything OK?" composite at
        alert forever. Walks the ring newest-first so a room re-flagged after a
        prior clear-and-reflag cycle surfaces its MOST RECENT alert, not a
        stale one."""
        active = self._active
        if not active:
            return []
        found: dict = {}
        for alert in reversed(self._alerts):
            if alert.room in active and alert.room not in found:
                found[alert.room] = alert
                if len(found) == len(active):
                    break
        return list(found.values())
