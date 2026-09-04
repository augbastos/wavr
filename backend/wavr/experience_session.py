"""An experience running somewhere, and how it learns that somewhere changed.

## What a session is, and the much longer list of what it is not

A session is a NAME for "this experience is running, in this room, on these
devices". It exists so Wavr can tell an application that the room it is running
in now has a screen in it, or that the room changed.

**It is not a state transport.** Wavr does not carry an application's data from
one device to another, and building something that looked like it did would be
the worst kind of platform promise: the demo works, and then somebody ships an
app that loses a user's half-finished form on the way to the television.

**It is not a handoff.** Wavr publishes "a display became available in the room
this session is in". Whether that means anything — whether to offer *Continue on
TV?*, whether the experience even makes sense on a screen — is the application's
judgement, made with knowledge Wavr does not have. The primitive is the event.
The decision belongs to whoever wrote the thing.

**It carries no people.** A session lists the DEVICES that joined it, which the
application already knew because they joined. It does not carry participants,
because there is no spatial scope that grants identity and adding one would be a
different product with a different consent conversation. `experience.py` holds
the same line for the same reason.

## In memory, and bounded

A session belongs to a running application. It does not outlive the Core, and
that is correct rather than a limitation: after a restart the application
reconnects and opens a new one, which is exactly what its own state requires
anyway. Persisting sessions would create rows describing applications that
stopped running months ago.

## Why the events are named the way they are

Same rule as `spatial_events`: a name may only claim what Wavr observed.

    experience.target_available   ✓ a device that can show something is here
    experience.user_moved         ✗ Wavr does not know the user moved, or that
                                    the person in the new room is the same one
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

# What a device can be a target FOR. Deliberately the same words the Experience
# Context uses, so an application is not translating between two vocabularies
# that mean the same thing.
TARGET_DISPLAY = "display"
TARGET_AUDIO = "audio"

# Events a session emits. `spatial_events` owns the ROOM's events; these are
# about one running experience, which is a different subject with a different
# audience — an application that does not care about the kitchen still cares
# that its own session's room changed.
EV_ROOM_CHANGED = "experience.room_changed"
EV_TARGET_AVAILABLE = "experience.target_available"
EV_TARGET_LOST = "experience.target_lost"

SESSION_EVENTS: frozenset[str] = frozenset({
    EV_ROOM_CHANGED, EV_TARGET_AVAILABLE, EV_TARGET_LOST})

# There is deliberately no `experience.context_changed`. A room's precision,
# occupancy and count already have events, on `/ws/events`, and a session-scoped
# copy of the same facts would make an application choose between two sources of
# one truth — and eventually get a different answer from each.

# How long a session survives without being touched. Generous enough for an
# application that is idle on somebody's coffee table, short enough that a
# crashed one does not sit in the list until the Core restarts.
IDLE_TTL_S = 3600.0

# A cap, so a misbehaving application cannot grow this without limit. Reached
# only by something already broken; the oldest goes first, because a session
# nobody has touched in an hour is the least likely to be in use.
MAX_SESSIONS = 200

MAX_METADATA_KEYS = 16
MAX_METADATA_CHARS = 512
# Every other collection in this feature is bounded -- manifest lists at 32,
# anchor polygons at 64, provider batches at 200 -- and these two were the
# outliers. A caller holding the lowest credential this feature targets could
# loop `join` with fresh ids to grow one session without limit while keeping it
# alive, and `room` came straight off a request body with no cap at all.
MAX_SESSION_DEVICES = 32
MAX_ROOM_CHARS = 80


class SessionError(ValueError):
    """A session operation that would claim something Wavr cannot support."""


@dataclass
class Session:
    """One experience, running somewhere."""

    session_id: str
    experience_id: str
    room: str = ""
    devices: tuple[str, ...] = ()
    # Opaque to Wavr and bounded. An application's own bookkeeping — a chapter
    # number, a difficulty — so it can reconnect without a database of its own.
    # NEVER interpreted here: the moment Wavr reads a field it becomes a
    # contract, and this one is explicitly not.
    metadata: dict = field(default_factory=dict)
    created_ts: float = 0.0
    updated_ts: float = 0.0
    # What was available last time the session was evaluated, so a change can be
    # told from a repetition.
    targets: tuple[dict, ...] = ()

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "experience_id": self.experience_id,
            "room": self.room,
            "devices": list(self.devices),
            "targets": [dict(t) for t in self.targets],
            "metadata": dict(self.metadata),
            "note": ("Wavr names this session and tells you when its room or "
                     "its targets change. It does not move your application's "
                     "state anywhere — that is yours to carry."),
        }


def _clean_metadata(raw) -> dict:
    """An application's own bookkeeping, bounded and left uninterpreted."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SessionError("metadata must be an object")
    if len(raw) > MAX_METADATA_KEYS:
        raise SessionError(f"at most {MAX_METADATA_KEYS} metadata keys")
    out = {}
    for k, v in raw.items():
        key = str(k)[:64]
        text = v if isinstance(v, (int, float, bool)) or v is None else str(v)
        if isinstance(text, str) and len(text) > MAX_METADATA_CHARS:
            raise SessionError(
                f"metadata value for {key!r} is longer than "
                f"{MAX_METADATA_CHARS} characters — Wavr is not a state store")
        out[key] = text
    return out


def _derived_label(device, room: str) -> str:
    """What to call a device in front of a user, without naming its owner."""
    kind = ("screen" if device.get("display") is True
            else "speaker" if device.get("audio") is True else "device")
    return f"{room} {kind}".strip() if room else kind


def targets_in(room: str, devices) -> tuple[dict, ...]:
    """Devices in a room that could be handed something.

    Only devices that SAID they can. A device that never sent a capability
    manifest reports `display: None`, and offering it as a screen would put an
    application's "Continue on TV?" in front of somebody whose device has no
    screen — the tristate rule, at the one place where getting it wrong is
    visible to a user.
    """
    out = []
    for d in devices or ():
        if str(d.get("room") or "") != room:
            continue
        kinds = tuple(k for k, field_name in
                      ((TARGET_DISPLAY, "display"), (TARGET_AUDIO, "audio"))
                      if d.get(field_name) is True)
        if not kinds:
            continue
        # ALWAYS derived — never a label the caller handed in, and never the
        # pairing name. That name is typed by whoever paired the device and is
        # very often a person's, and this is the list an application renders
        # into "Continue on which?".
        #
        # This read `d.get("label") or _derived_label(...)`, which quietly made
        # the guarantee conditional on the caller not supplying one. No live
        # caller did, so it never leaked — but "safe because of what today's
        # only caller happens to pass" is not a guarantee, and the identical
        # bug in `experience._visible_devices` was live.
        out.append({"device_id": d.get("device_id"),
                    "label": _derived_label(d, room),
                    "can": list(kinds)})
    return tuple(sorted(out, key=lambda t: str(t["device_id"])))


class SessionStore:
    """Live experience sessions. In memory, bounded, and not persisted.

    See the module docstring: a session belongs to a running application, and
    rows describing applications that stopped months ago would be worse than
    nothing.
    """

    def __init__(self, *, clock=time.monotonic, on_event=None):
        self._sessions: dict[str, Session] = {}
        self._clock = clock
        self._on_event = on_event

    # -- lifecycle -----------------------------------------------------------

    def open(self, experience_id: str, *, room: str = "", devices=(),
             metadata=None) -> Session:
        experience_id = " ".join(str(experience_id or "").split())[:64]
        if not experience_id:
            raise SessionError("a session must name the experience it is for")
        self._expire()
        if len(self._sessions) >= MAX_SESSIONS:
            # Oldest first: a session nobody has touched is the least likely to
            # be in use. Reached only by something already misbehaving.
            oldest = min(self._sessions.values(), key=lambda s: s.updated_ts)
            self._sessions.pop(oldest.session_id, None)
        now = self._clock()
        session = Session(
            session_id=f"ses_{secrets.token_hex(8)}",
            experience_id=experience_id,
            room=str(room or "")[:MAX_ROOM_CHARS],
            devices=tuple(str(d) for d in devices or ()),
            metadata=_clean_metadata(metadata),
            created_ts=now, updated_ts=now)
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        self._expire()
        return self._sessions.get(session_id)

    def close(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None

    def list(self) -> list[Session]:
        self._expire()
        return [self._sessions[k] for k in sorted(self._sessions)]

    def touch(self, session_id: str) -> Session | None:
        session = self.get(session_id)
        if session is not None:
            session.updated_ts = self._clock()
        return session

    def _expire(self) -> None:
        now = self._clock()
        for sid in [s.session_id for s in self._sessions.values()
                    if now - s.updated_ts > IDLE_TTL_S]:
            self._sessions.pop(sid, None)

    # -- the part that produces events ---------------------------------------

    def observe(self, session_id: str, *, room: str, devices) -> list[dict]:
        """Re-evaluate a session against the world, returning what changed.

        The heart of handoff, and note what it does NOT do: it reports that a
        display became available. It does not decide that the experience should
        move there, because that depends on what the experience IS — a recipe
        follows you to a screen, a private message does not.
        """
        session = self.get(session_id)
        if session is None:
            raise SessionError("no such session")
        now = self._clock()
        events: list[dict] = []

        def emit(kind: str, **payload):
            ev = {"event": kind, "session_id": session_id,
                  "experience_id": session.experience_id, **payload}
            events.append(ev)
            if self._on_event is not None:
                self._on_event(ev)

        room = str(room or "")[:MAX_ROOM_CHARS]
        if room != session.room:
            emit(EV_ROOM_CHANGED, room=room, previous=session.room)
            session.room = room

        now_targets = targets_in(room, devices)
        before = {t["device_id"]: t for t in session.targets}
        after = {t["device_id"]: t for t in now_targets}
        for device_id in sorted(set(after) - set(before)):
            emit(EV_TARGET_AVAILABLE, target=after[device_id])
        for device_id in sorted(set(before) - set(after)):
            emit(EV_TARGET_LOST, target=before[device_id])
        session.targets = now_targets
        session.updated_ts = now
        return events

    def set_metadata(self, session_id: str, metadata) -> Session:
        session = self.get(session_id)
        if session is None:
            raise SessionError("no such session")
        session.metadata = _clean_metadata(metadata)
        session.updated_ts = self._clock()
        return session

    def join(self, session_id: str, device_id: str) -> Session:
        """Add a device to a session.

        The application's own record of who is in it. Wavr stores the id it was
        given and attaches no meaning — it does not check that the device is
        paired, because a session is not an authorization and pretending it were
        would put a second, weaker gate beside the real one.
        """
        session = self.get(session_id)
        if session is None:
            raise SessionError("no such session")
        device_id = str(device_id or "").strip()[:64]
        if not device_id:
            raise SessionError("a device id is required")
        if device_id not in session.devices:
            if len(session.devices) >= MAX_SESSION_DEVICES:
                raise SessionError(
                    f"a session may hold at most {MAX_SESSION_DEVICES} devices. "
                    f"More than that is not a session, it is a leak.")
            session.devices = session.devices + (device_id,)
        session.updated_ts = self._clock()
        return session
