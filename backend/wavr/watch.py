"""Watch / Guard mode ("Vigia") -- the privacy-graduated thesis as a feature.

Watch is an ORTHOGONAL privacy axis layered on the Off/Presence/Precise sensing
ladder (not a fourth mutually-exclusive tier): a single server-side toggle that,
while ON, performs a deliberate PRIVACY INVERSION -- detect an UNRECOGNIZED person
WITHOUT surveilling the family. While active:

  * every per-person geometry/identity/biometric field is SUPPRESSED from state and
    from every egress (dashboard/WS, /api/state, MQTT, narrator); and
  * only COUNTS + the intrusion ROOM + entry/exit edges are allowed to leave.

Intrusion is inferred purely from counts: a room whose honest person_count (A1,
from a counting-capable source camera/mmwave) exceeds the number of KNOWN people
present (the consent identity layer) MUST contain at least one unaccounted person
-- surfaced room-level only, never WHO or WHERE-in-the-room.

SUPPRESSED_FIELDS, project_state, room_unrecognized and known_present_persons live
here ONCE so the suppression can never drift between egress points. HONESTY GATE:
intrusion detection needs the identity layer to know who is known; with identity
disabled the KNOWN set is empty, so the caller gates on identity_enabled -- Watch
still suppresses geometry (fail-safe toward MORE privacy) but reports it cannot flag
an intruder rather than declaring the whole family unknown.
"""
from __future__ import annotations

from datetime import datetime, timezone

from wavr.alert_severity import SEVERITY_ALERT

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
    """One edge-triggered unrecognized-person-in-room event. Count-only and room-
    level: carries the room, the person_count that tripped it and the known-present
    count compared against -- NEVER a target position, identity, or geometry.
    Severity fixed at alert (serious, on the shared ladder) -- high, but NOT critical
    (the ladder reserves critical for a sustained/confirmed gateway change)."""

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
    it clears, so a lingering person never spams the stream. Bounded ring."""

    def __init__(self, max_alerts: int = 50, now_fn=None):
        self._alerts = []
        self._active = set()   # rooms currently flagged (edge state)
        self._max = max_alerts
        self._now_fn = now_fn

    def _now_iso(self) -> str:
        if self._now_fn is not None:
            return self._now_fn()
        return datetime.now(timezone.utc).isoformat()

    def record(self, room, unrecognized, person_count, known_present, ts=None):
        """Fold one room current unrecognized verdict into the edge state. Returns a
        NEW IntrusionAlert only on a clear-to-flagged transition (else None)."""
        if not unrecognized:
            self._active.discard(room)
            return None
        if room in self._active:
            return None   # already flagged -- edge already fired, no re-spam
        self._active.add(room)
        alert = IntrusionAlert(room, int(person_count) if person_count is not None else 0,
                               max(0, int(known_present)), ts or self._now_iso())
        self._alerts.append(alert)
        if len(self._alerts) > self._max:
            self._alerts = self._alerts[-self._max:]
        return alert

    def reset(self) -> None:
        """Clear the per-room edge state (NOT the alert ring), so that after Watch is
        turned off and on again a still-present intrusion re-fires rather than being
        swallowed as already-flagged."""
        self._active.clear()

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
