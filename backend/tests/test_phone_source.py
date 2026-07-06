"""PhoneSensorSource — the fusion consumer for paired-phone telemetry (blueprint step 4).

These are the GATE for the source: they prove it emits a PLAIN whole-home presence
event (never a per-person identity, never a room, never targets), that its staleness
gate IS the fusion freshness window (no bespoke timer), that fusion decay ages a silent
phone to away, and that a lone phone can corroborate who's-home but NEVER fabricate
occupancy. All clocks are injected — no real sleeps.
"""
from dataclasses import fields
from datetime import datetime, timedelta, timezone

import wavr.events as events_mod
from wavr.events import SensingEvent
from wavr.fusion import (DEFAULT_WEIGHTS, FusionEngine, _DEFAULT_FRESHNESS_S,
                         _DEFAULT_STALE_S)
from wavr.sources.phone import PhoneSensorSource
from wavr.telemetry import TelemetryPayload, TelemetryReading

_BASE = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
_FRESHNESS = _DEFAULT_FRESHNESS_S   # 30s — the SAME window fusion prunes/decays against
_STALE = _DEFAULT_STALE_S           # 90s
_THRESHOLD = 0.5                    # FusionEngine default `threshold`


class _Clock:
    """Injectable monotonic-ish clock the test advances by hand (no real time)."""

    def __init__(self, base: datetime = _BASE):
        self.t = base

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t = self.t + timedelta(seconds=seconds)


class _FakeHub:
    """Test double for TelemetryHub: `get()` returns queued readings, then simulates a
    silent tick by raising TimeoutError (exactly what `asyncio.wait_for` raises on a real
    tick timeout) — so the source's silence path runs with zero real waiting."""

    def __init__(self, readings=()):
        self._readings = list(readings)

    async def get(self):
        if self._readings:
            return self._readings.pop(0)
        raise TimeoutError   # asyncio.TimeoutError is an alias for builtin TimeoutError


def _reading(device_id: str, ts: datetime = _BASE) -> TelemetryReading:
    return TelemetryReading(device_id=device_id, ts=ts.isoformat())


# --- T1: telemetry -> arrived -------------------------------------------------------

async def test_t1_telemetry_marks_home_present():
    clock = _Clock()
    src = PhoneSensorSource(_FakeHub([_reading("devA")]), now_fn=clock)
    agen = src.events()
    ev = await agen.__anext__()
    await agen.aclose()

    assert ev.room == "casa"            # whole-home, HARDCODED — payload never sets room
    assert ev.modality == "phone"
    assert ev.presence is True
    assert ev.confidence == 0.8         # present_confidence default
    assert ev.targets == ()             # a phone cannot localize a person
    assert ev.motion == 0.0
    assert ev.breathing_bpm is None and ev.heart_bpm is None
    assert ev.ts == _BASE.isoformat()


# --- T2: silence past freshness_s -> absent (source-level gate == fusion window) -----

async def test_t2_silence_past_freshness_marks_absent():
    clock = _Clock()
    # One reading, then the hub is empty -> the next tick is a silent (TimeoutError) tick.
    src = PhoneSensorSource(_FakeHub([_reading("devA")]), now_fn=clock)
    agen = src.events()

    ev_present = await agen.__anext__()          # reading consumed at t0
    assert ev_present.presence is True

    clock.advance(_FRESHNESS + 1)                # 31s of silence — past the freshness gate
    ev_absent = await agen.__anext__()           # silent tick -> re-evaluate, prune devA
    await agen.aclose()

    assert ev_absent.presence is False
    assert ev_absent.confidence == 0.0
    # Boundary: the source gate is exactly the fusion freshness window, no bespoke timer.
    assert src._freshness_s == _FRESHNESS


# --- T3: silence -> away via FusionEngine decay past STALE_S (reuse, not a new timer) -

async def test_t3_fusion_decay_ages_silent_phone_to_away():
    clock = _Clock()
    engine = FusionEngine(now_fn=clock)           # default weights + threshold
    ev = SensingEvent(room="casa", modality="phone", presence=True, motion=0.0,
                      breathing_bpm=None, heart_bpm=None, confidence=0.8,
                      ts=_BASE.isoformat(), targets=())

    fresh = engine.update(ev)                      # age 0 -> full weight
    assert fresh.confidence == 0.4                 # 0.5 weight x 0.8 conf x 1.0 decay
    assert fresh.occupied is False

    clock.advance(_STALE + 1)                      # 91s -> past STALE_S -> decay to 0
    stale = engine.state("casa")                   # re-fuse against the advanced clock
    assert stale.confidence == 0.0                 # the phone's vote has fully decayed
    assert stale.occupied is False                 # away — proved by fusion decay, not a
    #                                                bespoke phone timer


# --- T4: whole-home only + a lone phone can never dominate/fabricate occupancy -------

async def test_t4_whole_home_only_and_no_dominate():
    # (a) every event the source emits is whole-home: room == "casa", targets == ().
    clock = _Clock()
    src = PhoneSensorSource(_FakeHub([_reading("devA")]), now_fn=clock)
    agen = src.events()
    present = await agen.__anext__()               # a present event
    clock.advance(_FRESHNESS + 1)
    absent = await agen.__anext__()                # a silent/absent event
    await agen.aclose()
    for ev in (present, absent):
        assert ev.room == "casa"
        assert ev.targets == ()

    # (b) a LONE phone (its only casa source) present at conf 0.8 -> NOT occupied.
    engine = FusionEngine()                        # DEFAULT_WEIGHTS, threshold 0.5
    rs = engine.update(present)                    # present.presence is True, conf 0.8
    assert rs.occupied is False

    # (c) the numeric invariant behind (b): phone_weight x present_confidence < threshold.
    phone_weight = DEFAULT_WEIGHTS["phone"]
    present_confidence = 0.8
    assert phone_weight == 0.5
    assert phone_weight * present_confidence == 0.4
    assert phone_weight * present_confidence < _THRESHOLD


# --- T5: no SensingEvent contract fork; label resolved by device_id, self-name ignored -

async def test_t5_no_contract_fork_label_by_device_id():
    # (a) SensingEvent was NOT forked: no identity/person/rssi field, and there is no
    #     Identity class in wavr.events (the blueprint sketch's error, rejected).
    field_names = {f.name for f in fields(SensingEvent)}
    assert "identity" not in field_names
    assert "person" not in field_names
    assert "rssi" not in field_names
    assert not hasattr(events_mod, "Identity")

    # (b) a phone POSTs a body self-naming as another device ("LIAR"); identity is the
    #     token-derived device_id, and the payload's self-name is dropped at the boundary.
    payload = TelemetryPayload(device="LIAR-I-AM-BOB", rssi=-40)
    reading = TelemetryReading.from_payload(payload, device_id="devA")
    assert reading.device_id == "devA"                     # token identity, authoritative
    reading_field_names = {f.name for f in fields(TelemetryReading)}
    assert "device" not in reading_field_names             # payload self-name never stored
    assert "LIAR-I-AM-BOB" not in reading.to_dict().values()

    # (c) the label is resolved from the DeviceStore (get_label) BY device_id — never from
    #     the payload — and it never rides the emitted SensingEvent.
    labels = {"devA": "Augusto's Phone"}
    src = PhoneSensorSource(_FakeHub([reading]), get_label=labels.get, now_fn=_Clock())
    agen = src.events()
    ev = await agen.__anext__()
    await agen.aclose()

    assert src.whos_home() == ["Augusto's Phone"]          # resolved by device_id
    ev_dict = ev.to_dict()
    assert "rssi" not in ev_dict                           # rssi stays in the telemetry
    assert "identity" not in ev_dict and "person" not in ev_dict
    assert "Augusto's Phone" not in str(ev_dict)           # label never on the event
