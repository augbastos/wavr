import pytest
from wavr.sources.ruview import RuViewSource

FRAME = {
    "type": "sensing_update",
    "classification": {"presence": True, "confidence": 0.43},
    "features": {"motion_band_power": 9.7758},
    "vital_signs": {"breathing_rate_bpm": 9.707, "heart_rate_bpm": 46.22},
    "timestamp": 1782924055.636,
}

async def _first_n(source, n):
    out = []
    agen = source.events()
    try:
        async for ev in agen:
            out.append(ev)
            if len(out) == n:
                break
    finally:
        await agen.aclose()
    return out

async def test_ruview_yields_wifi_csi_events_from_frames():
    async def connect(url):
        for f in (FRAME, FRAME):
            yield f
    src = RuViewSource("ws://x", room="quarto", connect=connect)
    evs = await _first_n(src, 2)
    assert all(e.modality == "wifi_csi" and e.room == "quarto" for e in evs)
    assert evs[0].breathing_bpm == 9.707 and evs[0].confidence == 0.43

async def test_ruview_skips_non_sensing_frames():
    async def connect(url):
        yield {"type": "hello"}          # ignored
        yield FRAME                       # yielded
    src = RuViewSource("ws://x", room="sala", connect=connect)
    [ev] = await _first_n(src, 1)
    assert ev.modality == "wifi_csi"

async def test_ruview_reconnects_after_a_dropped_connection():
    calls = {"n": 0}
    async def connect(url):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("boom")   # first connection fails
        yield FRAME                          # second connection succeeds
    src = RuViewSource("ws://x", room="sala", connect=connect, reconnect_delay=0)
    [ev] = await _first_n(src, 1)
    assert calls["n"] == 2                   # it retried
    assert ev.presence is True

async def test_ruview_skips_non_dict_frame_without_reconnect():
    calls = {"n": 0}
    async def connect(url):
        calls["n"] += 1
        yield [1, 2, 3]   # validly-decoded but non-dict frame
        yield FRAME
    src = RuViewSource("ws://x", room="sala", connect=connect)
    [ev] = await _first_n(src, 1)
    assert ev.modality == "wifi_csi"
    assert calls["n"] == 1   # single connection, no reconnect

async def test_ruview_skips_bad_frame_without_reconnecting():
    calls = {"n": 0}
    bad = {
        "type": "sensing_update",
        "classification": {"presence": True, "confidence": 0.5},
        "features": {},
        "vital_signs": {},
        "timestamp": float("nan"),   # normalize_ruview -> _iso_from_unix raises ValueError
    }
    async def connect(url):
        calls["n"] += 1
        yield bad          # bad frame: skipped, not a reconnect
        yield FRAME         # good frame on the SAME connection
    src = RuViewSource("ws://x", room="sala", connect=connect)
    [ev] = await _first_n(src, 1)
    assert ev.modality == "wifi_csi"
    assert calls["n"] == 1   # single connection — only the bad frame was skipped

async def test_ruview_closes_inner_connect_generator_deterministically():
    closed = {"v": False}
    async def connect(url):
        try:
            yield FRAME
            yield FRAME
        finally:
            closed["v"] = True
    src = RuViewSource("ws://x", room="sala", connect=connect)
    agen = src.events()
    ev = await agen.__anext__()
    assert ev.modality == "wifi_csi"
    await agen.aclose()
    assert closed["v"] is True

async def test_an_unreachable_service_is_retried_with_backoff_and_logged_once(
        monkeypatch, caplog):
    # It used to retry every 3 s forever with a traceback each time, on every
    # install. The delay now doubles to a cap and resets when frames flow again.
    import asyncio
    import logging
    from wavr.sources import ruview as mod

    waits = []

    async def fake_sleep(s):
        waits.append(s)

    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
    calls = {"n": 0}

    async def connect(url):
        calls["n"] += 1
        if calls["n"] <= 7:
            raise ConnectionRefusedError("nobody home")
        yield FRAME

    caplog.set_level(logging.WARNING)
    src = RuViewSource("ws://x", room="sala", connect=connect, reconnect_delay=3.0)
    [ev] = await _first_n(src, 1)
    assert ev.modality == "wifi_csi"
    assert waits == [3.0, 6.0, 12.0, 24.0, 48.0, 60.0, 60.0]
    outage_lines = [r for r in caplog.records if "cannot reach" in r.getMessage()]
    assert len(outage_lines) == 1, "one warning per outage, not one per attempt"
    assert outage_lines[0].exc_info is None, "the traceback belongs at DEBUG"
