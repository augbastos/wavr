import asyncio
import struct

import pytest

from wavr.sources.mmwave import MmWaveSource, parse_ld2450_frame


def _slot(x_mm, y_mm, speed_cms):
    def enc(v):                      # sign-magnitude int16
        return (0x8000 | v) if v >= 0 else (-v)
    return struct.pack("<HHHH", enc(x_mm), enc(y_mm), enc(speed_cms), 320)


def _frame(*slots):
    body = b"".join(slots) + b"\x00" * 8 * (3 - len(slots))
    return b"\xaa\xff\x03\x00" + body + b"\x55\xcc"


def test_parse_one_target_mm_to_meters():
    ts = parse_ld2450_frame(_frame(_slot(1500, 2000, 0)))
    assert len(ts) == 1
    assert ts[0].x == pytest.approx(1.5) and ts[0].y == pytest.approx(2.0)
    assert ts[0].posture is None                     # not moving


def test_parse_negative_x_and_walking():
    ts = parse_ld2450_frame(_frame(_slot(-800, 1000, 60)))   # 0.6 m/s
    assert ts[0].x == pytest.approx(-0.8)
    assert ts[0].velocity == pytest.approx(0.6)
    assert ts[0].posture == "walking"


def test_parse_empty_and_garbage():
    assert parse_ld2450_frame(_frame()) == []
    assert parse_ld2450_frame(b"\x00" * 30) == []    # bad header
    assert parse_ld2450_frame(b"\xaa\xff\x03\x00" + b"\x01" * 10) == []  # short


@pytest.mark.asyncio
async def test_source_emits_presence_from_injected_frames():
    async def fake_frames():
        yield _frame(_slot(1000, 1000, 0))
        yield _frame()                               # everyone left

    src = MmWaveSource(room="sala", port="", frames=fake_frames(), interval=0)
    gen = src.events()
    e1 = await asyncio.wait_for(anext(gen), 1)
    assert e1.presence is True and e1.modality == "mmwave" and len(e1.targets) == 1
    e2 = await asyncio.wait_for(anext(gen), 1)
    assert e2.presence is False and e2.targets == ()
    await gen.aclose()


@pytest.mark.asyncio
async def test_mmwave_closes_inner_frames_generator_deterministically():
    closed = {"v": False}

    async def fake_frames():
        try:
            yield _frame(_slot(1000, 1000, 0))
            yield _frame(_slot(1000, 1000, 0))
        finally:
            closed["v"] = True

    src = MmWaveSource(room="sala", port="", frames=fake_frames(), interval=0)
    gen = src.events()
    ev = await asyncio.wait_for(anext(gen), 1)
    assert ev.presence is True
    await gen.aclose()
    assert closed["v"] is True
