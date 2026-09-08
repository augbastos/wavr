"""A sensor whose clock is an hour fast must not be the freshest in the house.

Freshness was `max(0.0, now - observed)`. The clamp is a reasonable instinct
about arithmetic — an age should not be negative — and the wrong answer about
the world, because it turns EVERY future timestamp into age zero, and age zero
is full weight.

Measured before the fix, on a Core whose fresh window is under ten minutes:

    honest camera,        ten minutes later  ->  confidence 0.000, "dead"
    camera one hour fast, fifty-nine minutes ->  confidence 0.900, "fresh"

So a board whose first NTP sync failed held its room at full confidence for
exactly as long as its skew, off an observation that was by then an hour old —
and because it kept sending, the skew never expired. It was permanently the
freshest source in the room while the honest camera beside it had already gone
dead.

A timestamp ahead of now is not fresher than now. It is a broken clock, and a
broken clock is as untrustworthy forward as it is backward, so age is now the
DISTANCE from now in either direction. `_SKEW_TOLERANCE_S` keeps the ordinary
couple of seconds between two machines free; past that, a sensor a minute fast
pays exactly what a sensor a minute late pays.

Deliberately not done: rejecting the event, or recording a separate
`received_at`. Rejecting throws away a reading that may be perfectly good and
only badly dated, and a second timestamp would be a field with no consumer —
the decay already expresses everything the fusion needs to know about when
something was seen.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from wavr.events import SensingEvent
from wavr.fusion import _SKEW_TOLERANCE_S, FusionEngine

T0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def _cam(desvio_s: float, *, sensor="cam", present=True, conf=0.9):
    """A camera reading stamped `desvio_s` from T0 — negative is late."""
    return SensingEvent(
        room="sala", modality="camera", presence=present, motion=0.0,
        breathing_bpm=None, heart_bpm=None,
        confidence=conf if present else 0.0,
        ts=(T0 + timedelta(seconds=desvio_s)).isoformat(),
        count=None, sensor_id=sensor)


def _em(agora_s: float):
    return FusionEngine(now_fn=lambda: T0 + timedelta(seconds=agora_s))


def test_a_clock_an_hour_fast_does_not_read_as_fresh():
    """The measurement that prompted this, as an assertion."""
    rs = _em(0).update(_cam(3600))
    linha = rs.sources[0]
    assert linha["health"] != "fresh", (
        f"a reading stamped an hour ahead reports {linha['health']!r} at age "
        f"{linha['age_s']}s; a fast clock is not freshness")
    assert rs.confidence == 0.0, (
        f"a reading an hour out of step still carries {rs.confidence} "
        f"confidence")


def test_it_decays_the_same_amount_in_either_direction():
    """Symmetry is the whole rule, so it is asserted rather than described.

    A sensor `n` seconds fast and a sensor `n` seconds late are equally out of
    step with this Core, and neither is entitled to more weight than the other.
    """
    n = 600.0
    adiantado = _em(0).update(_cam(n + _SKEW_TOLERANCE_S))
    atrasado = _em(0).update(_cam(-n))
    assert adiantado.confidence == pytest.approx(atrasado.confidence, abs=0.02), (
        f"a sensor {n:.0f}s fast reads {adiantado.confidence} and one "
        f"{n:.0f}s late reads {atrasado.confidence}")


def test_ordinary_clock_jitter_between_two_machines_costs_nothing():
    """The control on the fix, and the reason it is not simply "clamp to dead".

    Two machines are never perfectly in step. A node a second ahead of the Core
    is not a broken clock, and if this cost it weight the fix would be worse
    than the defect it replaces — every remote source in the house would
    flicker on ordinary NTP drift.
    """
    perfeito = _em(0).update(_cam(0))
    quase = _em(0).update(_cam(_SKEW_TOLERANCE_S - 1))
    assert quase.confidence == perfeito.confidence, (
        f"a source {_SKEW_TOLERANCE_S - 1:.0f}s ahead lost weight "
        f"({quase.confidence} vs {perfeito.confidence}); that is ordinary "
        f"jitter, not a broken clock")
    assert quase.sources[0]["health"] == "fresh"


def test_the_skewed_sensor_stops_dominating_the_honest_one():
    """The product consequence, end to end.

    Before: the skewed camera was fresh and the honest one was dead, so the
    room's answer came entirely from an hour-old observation. After: the
    honest one is the only one with any weight left.
    """
    f = _em(0)
    f.update(_cam(3600, sensor="cam-relogio-quebrado"))
    rs = f.update(_cam(0, sensor="cam-honesta", conf=0.7))

    por_id = {s["sensor_id"]: s for s in rs.sources}
    assert por_id["cam-honesta"]["health"] == "fresh"
    assert por_id["cam-relogio-quebrado"]["health"] == "dead"
    # The room's confidence is exactly what the honest camera alone would
    # produce — compared against that rather than against a number I worked
    # out from the weights, which is how the first version of this assertion
    # got it wrong. The engine's own answer is the reference.
    sozinha = _em(0).update(_cam(0, sensor="cam-honesta", conf=0.7))
    assert rs.confidence == sozinha.confidence, (
        f"the room reads {rs.confidence}; the only source with a working clock "
        f"would give {sozinha.confidence} on its own, so the skewed camera is "
        f"still contributing")


def test_a_reading_stamped_in_the_future_becomes_usable_when_now_catches_up():
    """Not rejected, just not trusted yet — and that distinction is deliberate.

    The reading may be perfectly good and only badly dated. Throwing it away
    would lose evidence; ageing it means it counts when its own timestamp stops
    being in the future, which is the same rule every other event follows.
    """
    adiantado = 600.0
    cedo = _em(0).update(_cam(adiantado))
    depois = _em(adiantado).update(_cam(adiantado))
    assert cedo.confidence < depois.confidence
    assert depois.sources[0]["health"] == "fresh"
