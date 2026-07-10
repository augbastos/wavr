from __future__ import annotations

import json
import logging
from typing import Callable

from wavr.mqtt_topics import room_event_topic, room_state_topic

_LOG = logging.getLogger(__name__)


class RulesEngine:
    """Consumes fused RoomState from the Hub and emits MQTT for home automation.
    Publishes each room's current occupancy to a RETAINED state topic (so a broker
    subscriber always sees the latest), and an edge EVENT topic only when occupancy
    flips. Only derived state is published — never frames/CSI/vitals.

    Also raises DEFENSIVE rogue-device alerts: given a Wavr Net inventory, any host
    whose MAC is not on the `known_macs` allowlist is published once to a security
    topic. Report-only — no action beyond the alert. Known MACs never alert.

    `known_provider` is an OPTIONAL callable returning the CURRENT set of
    runtime-known MACs (wavr.known_store.KnownStore.known_macs, same seam as
    wavr.netinventory_service.NetworkInventoryService) -- read fresh on every
    `handle_devices` call and unioned with the static `known_macs` allowlist,
    so a runtime mark-known takes effect immediately, with no restart and no
    static set baked in. Tolerant: a provider failure falls back to the
    static allowlist only."""

    def __init__(self, publish: Callable[[str, str, bool], None], prefix: str = "wavr",
                 known_macs=None, known_provider=None):
        self._publish = publish
        self._prefix = prefix
        self._last: dict[str, bool] = {}   # room -> last occupied
        self._known = {
            m.strip().replace("-", ":").lower()
            for m in (known_macs or ()) if m.strip()
        }
        self._known_provider = known_provider
        self._rogue_seen: set[str] = set()  # MACs already alerted (edge-triggered)

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

    async def run(self, hub) -> None:
        q = hub.subscribe()
        try:
            while True:
                self.handle(await q.get())
        finally:
            hub.unsubscribe(q)
