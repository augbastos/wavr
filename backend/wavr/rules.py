from __future__ import annotations

import json
import logging
from typing import Callable

from wavr.mqtt_topics import (
    house_status_topic, intrusion_topic, room_event_topic, room_state_topic,
    routine_anomaly_topic,
)

_LOG = logging.getLogger(__name__)


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

    `known_provider` is an OPTIONAL callable returning the CURRENT set of
    runtime-known MACs (wavr.known_store.KnownStore.known_macs, same seam as
    wavr.netinventory_service.NetworkInventoryService) -- read fresh on every
    `handle_devices` call and unioned with the static `known_macs` allowlist,
    so a runtime mark-known takes effect immediately, with no restart and no
    static set baked in. Tolerant: a provider failure falls back to the
    static allowlist only."""

    def __init__(self, publish: Callable[[str, str, bool], None], prefix: str = "wavr",
                 known_macs=None, known_provider=None, on_edge=None):
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
        # Topics are built via mqtt_topics so the room segment is slugged the SAME
        # way ha_discovery slugs it -- a room named e.g. "Sala + Cozinha" or
        # "Kids #1" would otherwise produce an illegal MQTT wildcard topic that
        # paho rejects, silently dropping that room from Home Assistant.
        self._publish(
            room_state_topic(self._prefix, room),
            json.dumps({"occupied": occupied, "confidence": rs["confidence"], "ts": rs["ts"]}),
            True,   # retained: latest state persists on the broker
        )
        prev = self._last.get(room)
        if prev is not None and prev != occupied:
            self._publish(room_event_topic(self._prefix, room),
                          "occupied" if occupied else "vacant", False)
            if self._on_edge:
                self._on_edge(room, occupied)   # same flip guard as the event above
        self._last[room] = occupied

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
