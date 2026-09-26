"""In-process measurements of the hot path and the storage it touches.

Prints one JSON object. Everything is driven through the REAL composition --
`create_app` with its own stores, supervisor and hub -- and the only thing
injected is a source that yields events from a queue, so the timer starts where a
sensor would hand over an event and stops where a dashboard would receive it:

    queue -> SourceManager (supervised) -> _ingest -> FusionEngine -> _publish
          -> SQLite (on change) -> occupancy log -> routines -> Hub subscriber

Run with the interpreter of the environment being measured:

    python benchmarks/pipeline.py [--events 2000]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone

ROOMS = ["kitchen", "living", "office", "bedroom", "bath", "hall", "garage", "porch"]
MODALITIES = ["camera", "mmwave", "ble", "pir"]


def _pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    k = min(len(xs) - 1, max(0, round(p / 100 * (len(xs) - 1))))
    return xs[k]


def _summ(xs_s):
    """Seconds in, microseconds out."""
    us = [x * 1e6 for x in xs_s]
    return {"n": len(us), "p50_us": round(_pct(us, 50), 1), "p95_us": round(_pct(us, 95), 1),
            "p99_us": round(_pct(us, 99), 1) if len(us) >= 200 else None,
            "mean_us": round(statistics.fmean(us), 1)}


def _event(i: int, *, changing: bool):
    from wavr.events import SensingEvent
    room = ROOMS[i % len(ROOMS)]
    present = (i // len(ROOMS)) % 2 == 0 if changing else True
    return SensingEvent(
        room=room, modality=MODALITIES[i % len(MODALITIES)], presence=present,
        motion=0.3 if present else 0.0, breathing_bpm=None, heart_bpm=None,
        confidence=0.9 if present else 0.0,
        ts=datetime.now(timezone.utc).isoformat(), sensor_id=f"bench-{room}")


async def _pipeline(n: int, db: str) -> dict:
    from wavr.app import create_app
    from wavr.hub import Hub

    q: asyncio.Queue = asyncio.Queue()

    class QueueSource:
        async def events(self):
            while True:
                yield await q.get()

    hub = Hub(maxsize=4096)
    t0 = time.perf_counter()
    app = create_app(sources=[("bench", QueueSource, True)], hub=hub)
    create_app_s = time.perf_counter() - t0
    sub = hub.subscribe()
    out: dict = {"create_app_existing_db_s": round(create_app_s, 4)}
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.2)                  # let the source task start
        while not sub.empty():
            sub.get_nowait()
        for mode in ("changing", "steady"):
            lat = []
            for i in range(n):
                ev = _event(i, changing=(mode == "changing"))
                t = time.perf_counter()
                await q.put(ev)
                while True:
                    msg = await asyncio.wait_for(sub.get(), timeout=5)
                    if isinstance(msg, dict) and msg.get("room") == ev.room:
                        break
                lat.append(time.perf_counter() - t)
            out[f"ingest_to_hub_{mode}"] = _summ(lat[50:])  # drop warm-up

        # Read-side payloads the dashboard, tray and MCP poll, with 8 rooms fused.
        import httpx
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://127.0.0.1:8000") as c:
            payloads = {}
            for path in ("/api/state", "/api/runtime", "/api/house-status",
                         "/api/status", "/api/history", "/api/attention",
                         "/api/transparency"):
                times, size, code = [], 0, None
                for _ in range(30):
                    t = time.perf_counter()
                    r = await c.get(path)
                    times.append(time.perf_counter() - t)
                    size, code = len(r.content), r.status_code
                payloads[path] = {"status": code, "bytes": size,
                                  **_summ(times[5:])}
            out["payloads"] = payloads
    return out


def _storage(db: str, n: int) -> dict:
    from wavr.fusion import FusionEngine
    from wavr.storage import Storage
    fe = FusionEngine()
    rs = fe.update(_event(0, changing=True))
    st = Storage(db)
    t = time.perf_counter()
    for _ in range(n):
        st.insert_state(rs)
    ins = (time.perf_counter() - t) / n
    times = []
    for _ in range(50):
        t = time.perf_counter()
        st.recent(200)
        times.append(time.perf_counter() - t)
    st.close()
    return {"insert_state_ms": round(ins * 1e3, 3),
            "recent_200": _summ(times)}


def _fusion(n: int) -> dict:
    from wavr.fusion import FusionEngine
    fe = FusionEngine(now_fn=lambda: datetime.now(timezone.utc))
    evs = [_event(i, changing=True) for i in range(512)]
    t = time.perf_counter()
    for i in range(n):
        fe.update(evs[i % 512])
    upd = (time.perf_counter() - t) / n
    t = time.perf_counter()
    for i in range(n // 10):
        for r in ROOMS:
            fe.state(r)
    st = (time.perf_counter() - t) / (n // 10 * len(ROOMS))
    return {"fusion_update_us": round(upd * 1e6, 2),
            "fusion_state_us": round(st * 1e6, 2)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=int, default=1000)
    args = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="wavr-bench-")
    os.chdir(tmp)
    os.environ["WAVR_DB"] = os.path.join(tmp, "wavr.db")
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    t = time.perf_counter()
    import wavr.app  # noqa: F401 -- module import builds one app (fresh DB)
    res = {"import_wavr_app_fresh_db_s": round(time.perf_counter() - t, 4),
           "python": sys.version.split()[0]}
    res.update(_fusion(20000))
    res.update(_storage(os.path.join(tmp, "storage.db"), 2000))
    res.update(asyncio.run(_pipeline(args.events, os.environ["WAVR_DB"])))
    print(json.dumps(res))


if __name__ == "__main__":
    main()
