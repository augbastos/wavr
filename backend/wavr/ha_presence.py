"""Home Assistant as a source of spatial EVIDENCE, not just a device registry.

## What was already here, and what was missing

`ha_import` reads HA's device registry so Wavr can name things; `ha_discovery`
publishes Wavr's own state back to HA. Both treat HA as a catalogue. Neither
lets HA's sensors say anything about who is where — and a household that already
owns six motion sensors, three door contacts and a couple of mmWave presence
sensors has already bought most of the hardware Wavr wants, and wired it in.

This is the inbound half: an HA entity, mapped to a Wavr room by a person,
becomes a `SensingEvent` and goes through the same fusion, the same precision
ladder and the same reliability measurement as a camera.

## Five rules, each of which exists because breaking it is a lie

**The room comes from the MAPPING, never from the payload.** Same rule as
`nodes.node_event`, for the same reason: an HA area renamed at three in the
morning must not silently relocate presence, and a compromised HA must not be
able to place a person in a room it was never given.

**`unavailable` and `unknown` are not `off`.** HA uses those for "this sensor is
not reporting", and translating them into `presence: False` would turn a dead
sensor into positive evidence of an empty room. They produce NO event at all —
which is exactly right, because fusion's freshness decay then does the honest
thing on its own: the source quietly loses its vote.

**A motion sensor's `off` is not `nobody there`.** It maps to `pir`, which is
outside `fusion.TRUSTED_ABSENCE_MODALITIES`, so its negative correctly cannot
clear a room. A person reading a book stops triggering a PIR long before they
stop being in the room.

**An unrecognised device class is `node`, not `pir`.** The conservative modality,
because guessing upward means claiming a capability the sensor may not have.

**Nothing is polled that nobody mapped.** An install with no mappings makes zero
requests to HA. That keeps the "Wavr works fully with every external provider
switched off" property true by construction rather than by intention.

## Why HA's clock is treated as foreign

An HA box keeps its own time, and its `last_changed` is stamped by that clock.
Feeding it straight into fusion's freshness arithmetic means a slow HA looks
permanently stale (its evidence silently discarded) and a fast one looks
permanently fresh (last night's reading still voting). `wavr.timebase` measures
the skew and either corrects it or, when it is big enough to be a timezone
misconfiguration rather than drift, says so and falls back to receipt time.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator

from wavr.events import SensingEvent
from wavr.providers import (
    CONF_NONE, KIND_SENSOR, REACH_LAN, describe,
)
from wavr.timebase import TimeBase

_LOG = logging.getLogger(__name__)

# HA device classes that are honest presence evidence, and what each becomes on
# Wavr's own ladder. Deliberately short: a device class Wavr does not understand
# is not silently promoted, and a class that does not observe PEOPLE (door,
# window, moisture) is not here at all -- a door opening is an event about a
# door, and treating it as presence would put a person in the hallway every time
# the wind moved.
DEVICE_CLASS_MODALITY: dict[str, str] = {
    "motion": "pir",          # passive IR: sees movement, loses a still person
    "occupancy": "pir",       # HA's own name for the same physics, usually
    "presence": "node",       # vague by definition -- could be anything
}

# What a mapped entity's `on` is worth before reliability has measured it.
#
# A number Wavr DECLARES, not one it measured, and low on purpose: HA hands over
# a boolean with no confidence attached, so any value here is an assumption. It
# sits below a camera (1.0) and a radar (0.9) because Wavr cannot see this
# sensor, cannot tell whether it is aimed at a sofa or a corridor, and did not
# install it. `reliability.py` measures the real figure per sensor and per room
# and scales this down -- never up.
DEFAULT_CONFIDENCE = 0.65

# HA states that mean "not reporting". See the module docstring: these produce no
# event, never a negative one.
NOT_REPORTING = frozenset({"unavailable", "unknown", "none", "", None})

# The prefix every sensor id from this provider carries, so a reliability profile
# or a coverage row can never be confused with a camera called `motion`.
SENSOR_PREFIX = "ha:"

# Wavr publishes its OWN conclusions into Home Assistant over MQTT Discovery —
# `binary_sensor.wavr_kitchen_occupancy` and friends, all carrying the unique_id
# prefix `wavr_` and the device name "Wavr" (see `ha_discovery.py`).
#
# Reading one of those back in as evidence would close a loop: Wavr decides the
# kitchen is occupied, publishes it, reads its own publication as an independent
# sensor, and reinforces itself. The room would then hold occupancy on the
# strength of nothing at all, and every surface — confidence, coverage, the
# explanation — would agree with it, because from the inside the evidence looks
# real.
#
# Provenance is the fix, and it is checked BY NAME because `/api/states` does not
# expose the device registry. That is a heuristic, and it errs the safe way: a
# third-party sensor with "wavr" in its name is refused, which costs an operator
# a rename, while the alternative costs them a house that agrees with itself.
_WAVR_OWN = "wavr"


def is_wavr_entity(entity) -> bool:
    """Whether this HA entity is one Wavr published itself.

    See `_WAVR_OWN`. Checks the entity id AND the friendly name, because HA
    derives the first from the device and entity names at creation time and an
    operator may have renamed either one afterwards.
    """
    if isinstance(entity, str):
        return _WAVR_OWN in entity.lower()
    if not isinstance(entity, dict):
        return False
    return any(_WAVR_OWN in str(entity.get(k) or "").lower()
               for k in ("entity_id", "friendly_name"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ha_presence_map (
    entity_id  TEXT PRIMARY KEY,
    room       TEXT    NOT NULL,
    modality   TEXT    NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    label      TEXT    NOT NULL DEFAULT '',
    created_ts TEXT    NOT NULL
);
"""


def descriptor():
    """This provider, declaring itself the way any other one has to.

    `REACH_LAN`: it contacts the operator's own Home Assistant over the local
    network and nothing else. Nothing about the home leaves the premises, which
    is the distinction `providers.REACH_INTERNET` and `REACH_CLOUD` exist to
    make and the one a household actually reads down to.

    Deliberately carries NO count of mapped entities. A descriptor describes the
    provider, not this deployment of it, and a registry built once at start would
    publish a number that goes stale the first time somebody maps a sensor — the
    same bug already found and fixed on the privacy screen's connector count.
    """
    return describe(
        "home_assistant", "Home Assistant sensors", KIND_SENSOR, REACH_LAN,
        observes=("presence", "motion"),
        # `room`, not `count`: HA hands over a boolean per entity. Six motion
        # sensors in one room are six opinions about presence, never a headcount,
        # and adding them up would be the fabricated number this codebase refuses.
        precision_ceiling="room",
        confidence_semantics=CONF_NONE,
        requires=("Home Assistant URL", "long-lived access token"),
        notes=("Presence evidence from binary sensors an operator has mapped to "
               "a Wavr room. Unmapped entities are never polled."))


@dataclass(frozen=True)
class Mapping:
    """One HA entity, bound by a person to one Wavr room."""

    entity_id: str
    room: str
    modality: str
    enabled: bool = True
    label: str = ""

    @property
    def sensor_id(self) -> str:
        return f"{SENSOR_PREFIX}{self.entity_id}"

    def to_dict(self) -> dict:
        return {"entity_id": self.entity_id, "room": self.room,
                "modality": self.modality, "enabled": self.enabled,
                "label": self.label, "sensor_id": self.sensor_id}


class HAPresenceError(ValueError):
    """A mapping that would put presence somewhere it does not belong."""


class HAPresenceStore:
    """Which HA entities feed which Wavr rooms. Shares `wavr.db`, owns one table.

    Configuration only -- an entity id, a room name and a switch. No states, no
    history, no HA token (that stays in config, exactly as `ha_import` requires).
    """

    def __init__(self, path: str = "wavr.db", now_fn=None):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._now = now_fn or (lambda: datetime.now(timezone.utc).isoformat())
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def map(self, entity_id: str, room: str, modality: str = "node",
            label: str = "") -> Mapping:
        entity_id = str(entity_id or "").strip()
        room = str(room or "").strip()
        if not entity_id or "." not in entity_id:
            raise HAPresenceError("entity_id must look like 'binary_sensor.hall'")
        if is_wavr_entity(entity_id):
            # Refused at the STORE, not only filtered out of the suggestions: an
            # operator can map an entity id by hand, and the suggestion list is
            # a convenience rather than a gate.
            raise HAPresenceError(
                f"{entity_id} is one of Wavr's own sensors, published to Home "
                f"Assistant over MQTT. Mapping it back in would make Wavr its "
                f"own evidence: the room would hold occupancy on the strength "
                f"of nothing, and every screen would agree with it.")
        if not room:
            # No default room, for the same reason `providers.describe` refuses a
            # default reach: the comfortable value would be chosen at exactly the
            # moment nobody is checking, and presence in the wrong room is worse
            # than no presence at all.
            raise HAPresenceError("a mapping must name the room it observes")
        if modality not in ("pir", "node", "mmwave", "ble"):
            raise HAPresenceError(f"unsupported modality {modality!r}")
        with self._lock:
            self._conn.execute(
                "INSERT INTO ha_presence_map"
                " (entity_id, room, modality, enabled, label, created_ts)"
                " VALUES (?, ?, ?, 1, ?, ?)"
                " ON CONFLICT(entity_id) DO UPDATE SET"
                " room = excluded.room, modality = excluded.modality,"
                " label = excluded.label",
                (entity_id, room, modality, label, self._now()))
            self._conn.commit()
        return Mapping(entity_id, room, modality, True, label)

    def unmap(self, entity_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM ha_presence_map WHERE entity_id = ?", (entity_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def set_enabled(self, entity_id: str, enabled: bool) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE ha_presence_map SET enabled = ? WHERE entity_id = ?",
                (1 if enabled else 0, entity_id))
            self._conn.commit()
        return cur.rowcount > 0

    def list(self, *, enabled_only: bool = False) -> list[Mapping]:
        sql = ("SELECT entity_id, room, modality, enabled, label"
               " FROM ha_presence_map")
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY entity_id"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [Mapping(r["entity_id"], r["room"], r["modality"],
                        bool(r["enabled"]), r["label"]) for r in rows]

    def get(self, entity_id: str) -> Mapping | None:
        for m in self.list():
            if m.entity_id == entity_id:
                return m
        return None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def suggest(entities, rooms) -> list[dict]:
    """HA entities that look like presence sensors, with a room GUESS.

    A suggestion, never an action. The `room` here is matched from HA's own
    friendly name against Wavr's room names, and it is offered to a human with
    the match shown -- because "Hall motion" landing in a Wavr room called `hall`
    is usually right and occasionally puts somebody in the wrong half of a house.
    A `room` of "" means no confident match and the operator has to choose.

    Entities Wavr does not recognise are returned with `modality: node` rather
    than dropped, so an operator with an unusual sensor can still map it. What is
    dropped is everything that is not about people at all -- a door contact is
    evidence about a door.
    """
    known = {str(r).strip().lower(): str(r) for r in (rooms or []) if str(r).strip()}
    out: list[dict] = []
    for e in entities or []:
        if not isinstance(e, dict):
            continue
        eid = str(e.get("entity_id") or "")
        if not eid.startswith(("binary_sensor.", "device_tracker.", "sensor.")):
            continue
        dev_class = str(e.get("device_class") or "").lower()
        if eid.startswith("binary_sensor.") and dev_class not in DEVICE_CLASS_MODALITY:
            continue
        modality = DEVICE_CLASS_MODALITY.get(dev_class, "node")
        name = str(e.get("friendly_name") or eid)

        if is_wavr_entity(e):
            # Listed with the reason rather than hidden. An operator scanning
            # for their hall sensor and finding Wavr's own "Hall occupancy"
            # missing would reasonably assume something is broken; being told
            # why it cannot be used teaches the thing worth knowing.
            out.append({"entity_id": eid, "friendly_name": name,
                        "device_class": dev_class, "modality": modality,
                        "suggested_room": "",
                        "refused": ("This is Wavr's own conclusion, published "
                                    "to Home Assistant. Reading it back would "
                                    "make Wavr its own evidence.")})
            continue

        haystack = f"{name} {eid}".lower()
        room = ""
        for key, original in known.items():
            # Longest match wins, so a house with `bath` and `bathroom` does not
            # put the bathroom sensor in the bath.
            if key in haystack and len(key) > len(room):
                room = original
        out.append({"entity_id": eid, "friendly_name": name,
                    "device_class": dev_class, "modality": modality,
                    "suggested_room": room})
    return out


def to_event(state: dict, mapping: Mapping, *, at: str,
             confidence: float = DEFAULT_CONFIDENCE) -> SensingEvent | None:
    """One HA state row as a canonical event, or None when it says nothing.

    None covers two different situations that must both stay silent:
    a sensor that is not reporting (`unavailable`), and a mapping the operator
    has switched off. Neither may produce a negative reading -- see the module
    docstring on why "not reporting" is not "empty".
    """
    if not mapping.enabled:
        return None
    raw = state.get("state") if isinstance(state, dict) else None
    value = str(raw).strip().lower() if raw is not None else None
    if value in NOT_REPORTING:
        return None
    present = value in ("on", "home", "detected", "true")
    return SensingEvent(
        sensor_id=mapping.sensor_id,
        # From the MAPPING. A payload that could set its own room would let a
        # renamed HA area silently relocate presence.
        room=mapping.room,
        modality=mapping.modality,
        presence=present,
        motion=0.0,
        breathing_bpm=None, heart_bpm=None,
        # Zero on absence, matching every first-party source: an "I do not see
        # anybody" carries no mass into the merge, because a PIR failing to see a
        # still person is much weaker evidence than a camera seeing an empty room.
        confidence=float(confidence) if present else 0.0,
        ts=at,
        # No count, ever. `pir`/`node` are outside COUNTING_MODALITIES and a
        # boolean per entity is not a headcount however many entities agree.
        count=None,
    )


class HomeAssistantSource:
    """Polls the MAPPED HA entities and emits canonical events.

    Per-entity rather than one `/api/states` sweep: a big HA install answers that
    with thousands of rows several times a minute, and this way an install with
    two mapped sensors makes two small requests -- the cost tracks what the
    operator asked for.

    The fetch is injectable for the same reason `ha_client`'s is: a test drives
    canned rows with no network and no new dependency.
    """

    def __init__(self, store: HAPresenceStore, client, *, interval: float = 3.0,
                 timebase: TimeBase | None = None, on_health=None):
        self._store = store
        self._client = client
        self._interval = max(0.5, float(interval))
        self._time = timebase or TimeBase()
        self._on_health = on_health

    async def events(self) -> AsyncIterator[SensingEvent]:
        while True:
            mappings = []
            try:
                mappings = self._store.list(enabled_only=True)
            except sqlite3.Error:
                # A store read that fails is a Wavr problem, not an HA one.
                # Deliberately narrow: a wrong method name here should surface as
                # a crash the supervisor reports, not as "HA has no sensors".
                _LOG.warning("ha_presence: mapping read failed", exc_info=True)
            for m in mappings:
                ev = await asyncio.to_thread(self._poll_one, m)
                if ev is not None:
                    yield ev
            await asyncio.sleep(self._interval)

    def _poll_one(self, mapping: Mapping) -> SensingEvent | None:
        try:
            state = self._client.get_state(mapping.entity_id)
        except Exception:      # noqa: BLE001 -- reaching off-box, any failure
            # An unreachable HA is ordinary (it reboots, it updates). It produces
            # no event, so fusion's freshness decay retires the evidence on its
            # own, which is the honest outcome: Wavr stops claiming to know.
            _LOG.debug("ha_presence: %s unreadable", mapping.entity_id,
                       exc_info=True)
            self._report(mapping, False)
            return None
        if not isinstance(state, dict):
            self._report(mapping, False)
            return None
        stamp = self._time.normalize(
            state.get("last_changed") or state.get("last_updated")
            or datetime.now(timezone.utc),
            source_id="home_assistant")
        self._report(mapping, True)
        return to_event(state, mapping, at=stamp.at.isoformat())

    def _report(self, mapping: Mapping, ok: bool) -> None:
        if self._on_health is None:
            return
        try:
            self._on_health(mapping.entity_id, ok)
        except Exception:      # noqa: BLE001
            _LOG.warning("ha_presence: health callback failed", exc_info=True)

    def clocks(self) -> dict:
        """What Wavr has measured about HA's clock, for the provider screen.

        Exposed rather than kept private because "your Home Assistant is an hour
        behind" is the kind of thing that otherwise shows up as "the motion
        sensors do nothing" and takes an evening to find.
        """
        return self._time.to_dict()
