from __future__ import annotations

# Single source of truth for the room MQTT topic layout. The RulesEngine
# (publisher) and ha_discovery (config it points at) MUST build the state/event
# topics through these helpers so the two stay byte-for-byte identical -- if they
# diverge, Home Assistant subscribes to a topic Wavr never publishes and the room
# silently never appears.


def slug_room(name: str) -> str:
    """Reduce a room name to a fragment that is BOTH a legal MQTT topic level and
    a legal HA object_id/unique_id: keep [a-z0-9_-], collapse everything else to
    '_'. Crucially this strips the MQTT wildcards '+' and '#' (illegal in a
    PUBLISH topic -- paho raises ValueError) and the level separator '/'. Empty /
    all-punctuation names fall back to 'room'."""
    s = "".join(c if (c.isalnum() or c in "-_") else "_" for c in name)
    return s.strip("_").lower() or "room"


def room_state_topic(prefix: str, room: str) -> str:
    """Retained per-room occupancy/confidence state topic."""
    return f"{prefix}/rooms/{slug_room(room)}/state"


def room_event_topic(prefix: str, room: str) -> str:
    """Edge occupied/vacant event topic (not retained)."""
    return f"{prefix}/rooms/{slug_room(room)}/event"


def status_topic(prefix: str) -> str:
    """Retained availability / Last-Will topic: 'online' while Wavr is connected,
    'offline' (via the broker's Last Will) the moment it drops off.

    HOUSE-level only: it answers "is Wavr running?", never "can Wavr see the
    kitchen?". A Core that is perfectly online with a dead kitchen camera keeps
    publishing 'online' here -- which is why `room_availability_topic` exists."""
    return f"{prefix}/status"


def room_availability_topic(prefix: str, room: str) -> str:
    """Retained PER-ROOM availability: 'online' while Wavr can currently say
    something about this room, 'offline' while it cannot.

    This is the room-scoped counterpart to `status_topic`, and it exists because
    a broker subscriber (Home Assistant above all) otherwise cannot tell two very
    different rooms apart: one a healthy sensor confirmed empty, and one whose
    only camera died an hour ago. Both used to arrive as a bare
    `{"occupied": false}` on the state topic and render as "Clear". Silence must
    not render as "nobody here" -- the same rule the dashboard's map painter
    already follows internally.

    Payloads are 'online'/'offline' so they match the discovery configs'
    `payload_available`/`payload_not_available` (and HA's own defaults) with no
    template."""
    return f"{prefix}/rooms/{slug_room(room)}/availability"


def room_coverage_topic(prefix: str, room: str) -> str:
    """Retained per-room sensor health + coverage rollup:
    `{sensors, observing, health, precision_level}` -- how many sensors the room
    has, how many are actually contributing, the room's health in
    `wavr.sensor_coverage`'s own vocabulary, and the finest answer the observing
    ones can honestly support. Counts and capability labels only: never a
    position, a target or a vital."""
    return f"{prefix}/rooms/{slug_room(room)}/coverage"


def house_coverage_topic(prefix: str) -> str:
    """Retained house-level coverage rollup: `{rooms, covered, uncovered,
    observing}`. COUNTS ONLY -- which rooms are blind is already implicit in the
    per-room coverage topics, so the house view does not need to re-list the
    household's own room names on a shared bus."""
    return f"{prefix}/house/coverage"


# ---- Build C4: derived-signal topics (RulesEngine publishes, ha_discovery points at) ----
# Same lockstep contract as the room topics above: both modules build these
# ONLY through these helpers so a discovery config can never subscribe to a
# topic the publisher doesn't actually write to.


def intrusion_topic(prefix: str, room: str | None) -> str:
    """Watch's A2 unrecognized-person binary signal. `room=None` is the
    ROOM-AGNOSTIC house-level aggregate (mirrors `wavr.watch.IntrusionAlert`'s
    own `room=None` convention for the spread-out-intrusion backstop) -- its
    topic never carries a room name, so a subscriber can't infer WHICH room
    from the topic alone when the signal is intentionally room-agnostic."""
    if room is None:
        return f"{prefix}/watch/house/intrusion"
    return f"{prefix}/watch/rooms/{slug_room(room)}/intrusion"


def routine_anomaly_topic(prefix: str, room: str) -> str:
    """A4 'occupancy unusual for this hour' per-room binary signal."""
    return f"{prefix}/rooms/{slug_room(room)}/routine_anomaly"


def house_status_topic(prefix: str) -> str:
    """A10's composed {status, score, reasons} house-status verdict."""
    return f"{prefix}/house/status"
