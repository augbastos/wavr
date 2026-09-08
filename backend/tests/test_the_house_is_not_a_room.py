"""The whole-building pseudo-room is not a room with no sensor in it.

Evidence that cannot localise reports under `casa`: the LAN scan, because one
antenna sees a house and never a room, and Bluetooth on its default setting.
That is the DEFAULT sensing level, so on a fresh install it is the only scope
anything reports into.

`FusionEngine.rooms()` returns every scope it has ever fused, and the coverage
serializer treated that list as a list of rooms. Measured before the fix, with
two rooms drawn and a camera in one of them:

    summarize(["casa", "kitchen", "hall"], [network, camera])
    -> uncovered == ["casa", "hall"]

which the Trust screen renders as

    Rooms being watched: 1 of 3
    casa -- no sensor at all
    Wavr cannot see this room. That is not the same as it being empty.

Three things wrong with that, in front of somebody deciding what to buy: the
household has two rooms and is told three; a place they have never heard of is
named; and the sentence under it is an errand for a purchase that cannot exist,
because nothing can cover a building the way a sensor covers a room.

With Bluetooth on it moved from `uncovered` into `rooms` instead, becoming a
card titled `casa` among the real ones. Same contradiction, other column.

The Space tab has always refused to draw it, for exactly this reason. Two
screens disagreeing about what counts as a room is the shape this repository
keeps finding: one producer, or they drift.
"""
from __future__ import annotations

from wavr.events import HOUSE_ROOM
from wavr.sensor_coverage import (KIND_HOST, SensorCoverage, summarize)

FUSED = [HOUSE_ROOM, "kitchen", "hall"]


def _network_row():
    """The LAN scan: roomless by design (see `_host_rows`)."""
    return SensorCoverage(sensor_id="host-network", kind=KIND_HOST,
                          modality="network", health="ok")


def _camera(room):
    return SensorCoverage(sensor_id=f"cam-{room}", kind="camera",
                          modality="camera", room=room, health="ok")


def _bluetooth():
    """Bluetooth reports into one configured room, which defaults to the
    pseudo-room. Reporting it as roomless would misstate where its readings
    land, so the row keeps the name -- and the serializer has to know that name
    is not a place."""
    return SensorCoverage(sensor_id="host-bluetooth", kind=KIND_HOST,
                          modality="ble", room=HOUSE_ROOM, health="ok")


def test_the_pseudo_room_is_never_listed_as_a_room_with_no_sensor():
    s = summarize(FUSED, [_network_row(), _camera("kitchen")])
    assert HOUSE_ROOM not in s["uncovered"], (
        f"the Trust screen would say {HOUSE_ROOM!r} has no sensor at all and "
        f"invite somebody to fix it: {s['uncovered']}")
    assert s["uncovered"] == ["hall"], (
        f"and the room that really has nothing must survive: {s['uncovered']}")


def test_the_count_of_rooms_matches_the_rooms_a_household_has():
    """"1 of 3" over a two-room home. The denominator is the sum of the two
    lists the Trust screen adds up, so a pseudo-room in either one inflates it.
    """
    s = summarize(FUSED, [_network_row(), _camera("kitchen")])
    total = len(s["rooms"]) + len(s["uncovered"])
    assert total == 2, (
        f"the household has kitchen and hall; the screen would say {total}: "
        f"rooms={[r['room'] for r in s['rooms']]} uncovered={s['uncovered']}")


def test_the_pseudo_room_is_never_a_room_card_either():
    """The other column. With Bluetooth on, it stopped being "uncovered" and
    became a watched room instead -- still a card named after nowhere."""
    s = summarize(FUSED, [_network_row(), _camera("kitchen"), _bluetooth()])
    assert HOUSE_ROOM not in [r["room"] for r in s["rooms"]], (
        f"a card named {HOUSE_ROOM!r}: {[r['room'] for r in s['rooms']]}")


def test_the_house_wide_sensors_are_still_reported():
    """Excluded from the room lists, NOT dropped. `house_wide` already exists
    and already means "coverage that is not about a room", which is precisely
    what these two are -- so nothing a household owns disappears from the
    receipt just because it cannot name a room.
    """
    s = summarize(FUSED, [_network_row(), _camera("kitchen"), _bluetooth()])
    ids = {c["sensor_id"] for c in s["house_wide"]}
    assert ids == {"host-network", "host-bluetooth"}, ids


def test_a_real_room_that_happens_to_have_coverage_is_untouched():
    """The guard must not be a filter on everything. A room with a sensor is a
    room with a sensor."""
    s = summarize(FUSED, [_camera("kitchen"), _camera("hall")])
    assert sorted(r["room"] for r in s["rooms"]) == ["hall", "kitchen"]
    assert s["uncovered"] == []
