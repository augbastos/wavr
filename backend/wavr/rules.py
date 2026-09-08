from __future__ import annotations

import json
import logging
import time
from typing import Callable

from wavr.mqtt_topics import (
    house_coverage_topic, house_status_topic, intrusion_topic,
    room_availability_topic, room_coverage_topic, room_event_topic,
    room_state_topic, routine_anomaly_topic,
)

_LOG = logging.getLogger(__name__)

# A room with no sensor at all. Distinct from every `wavr.sensor_coverage` health
# value because it is a different KIND of answer: `offline` is a repair, `none` is
# a purchase (sensor_coverage says exactly that in its own `summarize` note).
HEALTH_NONE = "none"


def precision_pct(level: str) -> int:
    """A precision-ladder rung as the ORDINAL fill (0/25/50/75/100) the RoomState
    already carries, derived through fusion's own tables so there is no second
    ladder here to drift.

    The export deliberately carries this NUMBER and never the rung's enum LABEL.
    The top rung is spelled `position`, and the MQTT stream's hard privacy
    invariant is that the words `position`/`target`/`pose`/`vital` never appear on
    it -- an operator grepping their own broker to audit what Wavr emits must not
    find that word and have to work out that it meant "could, in principle, place
    someone" rather than a coordinate. The ordinal says exactly as much (75 = a
    count is trustworthy here, 100 = the finest rung) and says nothing else."""
    from wavr.fusion import _RANK_PCT, _SCOPE_RANK
    return _RANK_PCT[_SCOPE_RANK.get(level or "none", 0)]


def _room_health(sensors: list[dict]) -> str:
    """One room's rollup of its sensors' health, in `wavr.sensor_coverage`'s OWN
    vocabulary -- that module is the single source of truth for what a health
    string means, and a second table here would drift invisibly:

      ok       -- at least one sensor in the room is actually observing
      offline  -- sensors exist, none observing, at least one has failed
      disabled -- sensors exist, none observing, they are switched off
      unknown  -- sensors exist, none observing, and health could not be read
      none     -- no sensor at all

    The partially-degraded case (one camera alive, one dead) rolls up to `ok` on
    purpose: the room IS being watched, and calling it `offline` would be the
    overclaim in the other direction. `sensors` and `observing` travel in the
    same payload, so the degradation is visible without a rollup word having to
    lie either way."""
    if not sensors:
        return HEALTH_NONE
    if any(s.get("observing") for s in sensors):
        return "ok"
    healths = {str(s.get("health") or "unknown") for s in sensors}
    for worst in ("offline", "disabled"):
        if worst in healths:
            return worst
    return "unknown"


class RulesEngine:
    """Consumes fused RoomState from the Hub and emits MQTT for home automation.
    Publishes each room's current occupancy to a RETAINED state topic (so a broker
    subscriber always sees the latest), and an edge EVENT topic only when occupancy
    flips. Only derived state is published — never frames/CSI/vitals.

    Also raises DEFENSIVE rogue-device alerts: given a Wavr Net inventory, any host
    whose MAC is not on the `known_macs` allowlist is published once to a security
    topic. Report-only — no action beyond the alert. Known MACs never alert.

    Build C4: also forwards three already-existing DERIVED signals onto MQTT (via
    ha_discovery's matching entities) so a user builds HA automations off them --
    ADR-0005 (Wavr stays a signal SOURCE, never an automation engine):
    `handle_intrusion` (Watch's A2), `handle_routine_anomaly` (A4 "unusual for this
    hour") and `handle_house_status` (A10's composite verdict). Every one of these
    is edge/change-triggered (never a re-publish of an unchanged value), same
    discipline as `handle`'s own state/event split.

    WORLD-READY EXPORT: the retained room-state payload carries the facts the
    fused RoomState already holds -- `occupied`, `confidence`, `person_count` and
    the precision rung as `precision_pct` (the ordinal, never the enum label; see
    `precision_pct` for why) -- plus, once coverage is known, that room's
    `sensors`/`observing`/`health` rollup and an `available` flag. The RoomState
    fields are copied ONLY when the incoming state actually carries them, so a
    caller handing this a minimal `{room, occupied, confidence, ts}` dict still
    gets byte-identical output to before these fields existed.

    AVAILABILITY (the sharp half): `occupied: false` is an ASSERTION -- "a healthy
    sensor looked and nobody is here". A room whose only camera has died cannot
    make that assertion, and publishing it anyway is how a broker subscriber ends
    up rendering a blind room as "Clear". So when coverage says nothing in the
    room is observing and nothing is currently detected, this publishes
    `occupied: null` on the state topic and `offline` on
    `room_availability_topic` -- unknown, not empty -- and fires NO occupancy edge
    (a dead camera must never look like somebody leaving). A room that IS being
    watched, or that is currently reporting a person, stays `online` and behaves
    exactly as before.

    Coverage reaches this engine two ways, both optional: pushed via
    `handle_coverage(summary)`, or pulled from a `coverage_provider` callable
    returning `wavr.sensor_coverage.summarize(...)`'s shape (throttled to
    `coverage_interval_s`, because `handle` runs per room per fuse tick and the
    provider reads stores). With neither wired, coverage is simply unknown and
    every room stays available -- the pre-availability behaviour, unchanged.

    `known_provider` is an OPTIONAL callable returning the CURRENT set of
    runtime-known MACs (wavr.known_store.KnownStore.known_macs, same seam as
    wavr.netinventory_service.NetworkInventoryService) -- read fresh on every
    `handle_devices` call and unioned with the static `known_macs` allowlist,
    so a runtime mark-known takes effect immediately, with no restart and no
    static set baked in. Tolerant: a provider failure falls back to the
    static allowlist only."""

    def __init__(self, publish: Callable[[str, str, bool], None], prefix: str = "wavr",
                 known_macs=None, known_provider=None, on_edge=None,
                 coverage_provider=None, coverage_interval_s: float = 10.0,
                 monotonic=None):
        self._publish = publish
        self._prefix = prefix
        # `on_edge(room, occupied)` -- the in-process seam the routines engine taps for
        # room_occupied/room_empty triggers, fired on the SAME per-room flip as the
        # MQTT event below (so it inherits the flip debounce). None -> unchanged.
        self._on_edge = on_edge
        self._last: dict[str, bool] = {}   # room -> last occupied
        self._known = {
            m.strip().replace("-", ":").lower()
            for m in (known_macs or ()) if m.strip()
        }
        self._known_provider = known_provider
        self._rogue_seen: set[str] = set()  # MACs already alerted (edge-triggered)
        # Build C4 change-detection: last published ON/OFF per (kind, room) binary
        # signal, and the last house-status payload string -- so an unchanged tick
        # (the common case: nothing wrong) never re-publishes/spams the broker.
        self._last_binary: dict[tuple[str, str | None], bool] = {}
        self._last_house_status: str | None = None
        # Coverage/availability state. `_room_coverage` missing a room means
        # coverage is UNKNOWN for it (not "uncovered") -- the engine then leaves
        # that room exactly as it behaved before availability existed.
        self._coverage_provider = coverage_provider
        self._coverage_interval = float(coverage_interval_s)
        self._monotonic = monotonic or time.monotonic
        self._coverage_at: float | None = None
        self._room_coverage: dict[str, dict] = {}
        self._last_coverage: dict[str, str] = {}      # room -> last coverage payload
        self._last_house_coverage: str | None = None
        self._last_availability: dict[str, bool] = {}
        # The most recent RAW `occupied` seen per room, refreshed on EVERY tick.
        # Distinct from `_last`, which is the edge tracker and deliberately
        # freezes while a room is blind -- reading that one here would latch a
        # blind-but-once-occupied room to 'online' forever.
        self._last_seen_occupied: dict[str, bool] = {}

    def _dynamic_known(self) -> set[str]:
        """The static `known_macs` allowlist UNIONED with the runtime
        KnownStore (if wired) -- see `known_provider` in the class
        docstring."""
        if not self._known_provider:
            return self._known
        try:
            provided = {
                m.strip().replace("-", ":").lower()
                for m in (self._known_provider() or ()) if m.strip()
            }
        except Exception:
            _LOG.warning("known_provider failed", exc_info=True)
            return self._known
        return self._known | provided

    def handle(self, rs: dict) -> None:
        room = rs["room"]
        occupied = bool(rs["occupied"])
        self._last_seen_occupied[room] = occupied
        self._refresh_coverage()
        cov = self._room_coverage.get(room)
        # Everything the fused RoomState already knows, carried straight through.
        # Copied only when PRESENT so a minimal state dict still produces the
        # original three-key payload (see the class docstring).
        body = {"occupied": occupied, "confidence": rs["confidence"], "ts": rs["ts"]}
        for key in ("person_count", "precision_pct"):
            if key in rs:
                body[key] = rs[key]
        available = True
        if cov is not None:
            available = self._available(room)
            body["available"] = available
            body["sensors"] = cov["sensors"]
            body["observing"] = cov["observing"]
            body["health"] = cov["health"]
            if not available:
                # Nothing is watching and nothing is detected: Wavr does not know
                # whether this room is empty, and `false` would claim it does.
                body["occupied"] = None
        # Topics are built via mqtt_topics so the room segment is slugged the SAME
        # way ha_discovery slugs it -- a room named e.g. "Sala + Cozinha" or
        # "Kids #1" would otherwise produce an illegal MQTT wildcard topic that
        # paho rejects, silently dropping that room from Home Assistant.
        self._publish(
            room_state_topic(self._prefix, room),
            json.dumps(body),
            True,   # retained: latest state persists on the broker
        )
        if cov is not None:
            self._publish_availability(room, available)
        if not available:
            # Blind room: no edge event, no routine trigger, and `_last` keeps its
            # pre-blackout value, so a dead camera never looks like somebody
            # leaving -- and a genuine departure still fires once sight returns.
            return
        prev = self._last.get(room)
        if prev is not None and prev != occupied:
            self._publish(room_event_topic(self._prefix, room),
                          "occupied" if occupied else "vacant", False)
            if self._on_edge:
                self._on_edge(room, occupied)   # same flip guard as the event above
        self._last[room] = occupied

    # ---- sensor health / coverage / per-room availability ----

    def handle_coverage(self, summary: dict) -> None:
        """Forward `wavr.sensor_coverage.summarize(...)`'s shape onto MQTT.

        Per room: a retained `room_coverage_topic` rollup
        (`{sensors, observing, health, precision_level}`) and a retained
        `room_availability_topic` online/offline. House-level: a retained
        `house_coverage_topic` count rollup. All three are change-detected, so a
        steady house (the common case) never re-publishes.

        A room in the summary's `uncovered` list gets a row too -- `health:
        "none"`, zero sensors -- because "nothing is installed here" is an answer
        a subscriber must be able to read, not an absence it has to infer."""
        self._apply_coverage(summary)

    def _refresh_coverage(self) -> None:
        """Pull coverage from `coverage_provider`, at most once per
        `coverage_interval_s`. `handle` runs once per room per fuse tick and the
        provider reads the camera/node stores, so an unthrottled pull would turn
        every tick into N store reads.

        A provider failure keeps the LAST known coverage rather than blanking the
        house: a transient store hiccup is not evidence that every sensor died,
        and flipping every room to unavailable on one bad read would be its own
        false alarm."""
        if not self._coverage_provider:
            return
        now = self._monotonic()
        if self._coverage_at is not None and (now - self._coverage_at) < self._coverage_interval:
            return
        self._coverage_at = now
        try:
            summary = self._coverage_provider()
        except Exception:
            _LOG.warning("coverage_provider failed", exc_info=True)
            return
        if summary:
            self._apply_coverage(summary)

    def _apply_coverage(self, summary: dict) -> None:
        rooms = [e for e in (summary.get("rooms") or []) if isinstance(e, dict)]
        uncovered = [r for r in (summary.get("uncovered") or []) if r]
        covered = 0
        observing_rooms = 0
        for entry in rooms:
            room = entry.get("room")
            if not room:
                continue
            sensors = [s for s in (entry.get("sensors") or []) if isinstance(s, dict)]
            observing = sum(1 for s in sensors if s.get("observing"))
            covered += 1
            observing_rooms += 1 if observing else 0
            self._record_room_coverage(room, {
                "sensors": len(sensors),
                "observing": observing,
                "health": _room_health(sensors),
                # The finest answer the OBSERVING sensors support (from
                # sensor_coverage.best_precision), as the word-free ordinal --
                # see `precision_pct`. A capability, never a certainty and never
                # a coordinate.
                "precision_pct": precision_pct(entry.get("precision_level")),
            })
        for room in uncovered:
            self._record_room_coverage(room, {
                "sensors": 0, "observing": 0, "health": HEALTH_NONE,
                "precision_pct": 0,
            })
        house = json.dumps({"rooms": covered + len(uncovered), "covered": covered,
                            "uncovered": len(uncovered), "observing": observing_rooms},
                           sort_keys=True)
        if house != self._last_house_coverage:
            self._last_house_coverage = house
            self._publish(house_coverage_topic(self._prefix), house, True)

    def _record_room_coverage(self, room: str, row: dict) -> None:
        self._room_coverage[room] = row
        payload = json.dumps(row, sort_keys=True)
        if self._last_coverage.get(room) != payload:
            self._last_coverage[room] = payload
            self._publish(room_coverage_topic(self._prefix, room), payload, True)
        self._publish_availability(room, self._available(room))

    def _available(self, room: str) -> bool:
        """Whether Wavr can currently say ANYTHING about this room.

        True while a sensor in the room is observing -- and also while the room
        is currently reporting a person, because a detection is itself proof
        something saw it. That second clause matters: a source the coverage table
        cannot match to a row (a renamed source reads `unknown`, never green)
        would otherwise hide a live detection behind an `unavailable` entity,
        which is the same failure as the one this whole seam exists to fix, only
        pointed the other way."""
        row = self._room_coverage.get(room)
        if row is None:
            return True          # coverage unknown -> unchanged legacy behaviour
        return bool(row["observing"]) or bool(self._last_seen_occupied.get(room))

    def _publish_availability(self, room: str, available: bool) -> None:
        if self._last_availability.get(room) == available:
            return
        self._last_availability[room] = available
        self._publish(room_availability_topic(self._prefix, room),
                      "online" if available else "offline", True)

    def handle_devices(self, devices) -> None:
        """Raise a rogue-device alert for each host whose MAC is not on the
        allowlist. Edge-triggered: a given rogue MAC alerts once (re-scans don't
        spam). Known/allowlisted devices — either on `known_macs` or already
        flagged `known` by the inventory — never alert. Report-only."""
        known = self._dynamic_known()
        for d in devices:
            if hasattr(d, "to_dict"):
                d = d.to_dict()
            mac = str(d.get("mac", "")).replace("-", ":").lower()
            if not mac or d.get("known") is True or mac in known:
                continue
            if mac in self._rogue_seen:
                continue
            self._rogue_seen.add(mac)
            self._publish(
                f"{self._prefix}/security/rogue",
                json.dumps({
                    "mac": mac,
                    "ip": d.get("ip"),
                    "vendor": d.get("vendor", "unknown"),
                    "device_type": d.get("device_type", "unknown"),
                    "hostname": d.get("hostname"),
                    "ts": d.get("ts"),
                }),
                False,   # edge security event — not retained
            )

    def handle_intrusion(self, room: str | None, active: bool) -> None:
        """Build C4: Watch's A2 unrecognized-person signal, retained ON/OFF, at
        BOTH scopes -- per room (`room=<name>`) and the ROOM-AGNOSTIC house-level
        aggregate (`room=None`, mirrors `wavr.watch.IntrusionAlert`'s own
        convention). Published ONLY on a change from the last-known value for
        that scope, so a lingering intrusion (or a lingering all-clear) never
        spams MQTT -- callers are expected to call this on every re-evaluation
        (not just on a NEW alert edge) so a clear-to-flagged AND a
        flagged-to-clear transition both reach the broker. Count-only upstream
        (wavr.watch never hands this a position/identity) -- this only forwards
        a bool."""
        key = ("intrusion", room)
        if self._last_binary.get(key) == active:
            return
        self._last_binary[key] = active
        self._publish(intrusion_topic(self._prefix, room),
                      "ON" if active else "OFF", True)

    def handle_routine_anomaly(self, room: str, unusual: bool) -> None:
        """Build C4: A4's "occupancy unusual for this hour" per-room signal,
        retained ON/OFF, change-triggered like `handle_intrusion`. Callers pass
        the CURRENT verdict every re-evaluation (`unusual` is never None here --
        an insufficient-data "don't know" from `OccupancyLog.is_unusual` should
        be folded to False by the caller, never asserted as an anomaly)."""
        key = ("routine", room)
        if self._last_binary.get(key) == unusual:
            return
        self._last_binary[key] = unusual
        self._publish(routine_anomaly_topic(self._prefix, room),
                      "ON" if unusual else "OFF", True)

    def handle_house_status(self, status: dict) -> None:
        """Build C4: A10's composed `{status, score, reasons, ts}` house-status
        verdict (`wavr.house_status.compose_house_status`'s return shape),
        forwarded RETAINED, byte-for-byte, to one JSON topic -- no new field,
        no re-ranking. Deduped on the full (status, score, reasons) payload (the
        `ts` alone changing every tick would otherwise defeat the dedup and
        re-publish "nothing changed" every cycle), so an unchanged "ok, nothing
        to report" verdict never re-publishes."""
        payload = json.dumps({"status": status["status"], "score": status["score"],
                              "reasons": status["reasons"]}, sort_keys=True)
        if payload == self._last_house_status:
            return
        self._last_house_status = payload
        self._publish(house_status_topic(self._prefix), json.dumps(status), True)

    async def run(self, hub) -> None:
        q = hub.subscribe()
        try:
            while True:
                self.handle(await q.get())
        finally:
            hub.unsubscribe(q)
