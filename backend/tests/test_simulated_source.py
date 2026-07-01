from wavr.events import SensingEvent
from wavr.sources.base import SensorSource
from wavr.sources.simulated import SimulatedSource, SENSORS


async def take(agen, n):
    out = []
    async for x in agen:
        out.append(x)
        if len(out) >= n:
            break
    return out


async def test_simulated_emits_one_event_per_sensor_with_modalities():
    src = SimulatedSource(interval=0.0)
    events = await take(src.events(), len(SENSORS))
    assert [(e.room, e.modality) for e in events] == list(SENSORS)
    assert all(isinstance(e, SensingEvent) for e in events)
    # at least two distinct modalities so the FusionEngine has something to fuse
    assert len({e.modality for e in events}) >= 2


async def test_simulated_is_deterministic_on_non_time_fields():
    a = await take(SimulatedSource(interval=0.0).events(), len(SENSORS))
    b = await take(SimulatedSource(interval=0.0).events(), len(SENSORS))
    key = lambda e: (e.room, e.modality, e.presence, e.motion, e.confidence)
    assert [key(e) for e in a] == [key(e) for e in b]


def test_simulated_source_satisfies_protocol():
    assert isinstance(SimulatedSource(), SensorSource)
