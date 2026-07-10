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
    'offline' (via the broker's Last Will) the moment it drops off."""
    return f"{prefix}/status"


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
