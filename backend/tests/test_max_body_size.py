"""wavr.serve.MaxBodySizeMiddleware -- global request-body-size cap (audit HIGH:
pre-auth resource exhaustion). serve.py is the ONE place a listening uvicorn socket
actually opens (app.py itself never binds -- every test's create_app() proves that),
so the guard is wired by wrapping the ASGI app THERE, outside FastAPI's own
add_middleware()/ServerErrorMiddleware stack (see the class docstring for why).
"""
import anyio
import pytest
from fastapi.testclient import TestClient

from wavr import serve
from wavr.app import create_app
from wavr.camera_store import CameraStore
from wavr.sources.simulated import SimulatedSource
from wavr.storage import Storage

CSRF = {"X-Wavr-Local": "1"}


def _wrapped_client(tmp_path, monkeypatch, max_bytes):
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(tmp_path / "house.json"))
    app = create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))
    bound = serve.MaxBodySizeMiddleware(app, max_bytes=max_bytes)
    return TestClient(bound)


def _valid_house():
    return {"version": 2, "units": "m", "floors": [
        {"id": "f0", "name": "T", "level": 0,
         "rooms": [{"id": "r1", "name": "sala", "polygon": [[0, 0], [4, 0], [4, 3], [0, 3]]}],
         "walls": [], "features": [], "backdrop": None}]}


# --------------------------------------------------------------------------- #
# Real-route integration: an oversized body never reaches the app; a legitimate
# house-map PUT (the exact "large-but-bounded request" the task calls out) is
# unaffected.
# --------------------------------------------------------------------------- #

def test_oversized_declared_content_length_rejected_413_never_500(tmp_path, monkeypatch):
    c = _wrapped_client(tmp_path, monkeypatch, max_bytes=1024)
    # A body an honest client accurately declares via Content-Length as over budget.
    r = c.put("/api/house", content=b"x" * 4096, headers={**CSRF, "Content-Type": "application/json"})
    assert r.status_code == 413
    assert not (tmp_path / "house.json").exists()   # never reached the handler


def test_legitimate_house_map_put_within_cap_still_works(tmp_path, monkeypatch):
    # Default-sized cap (8 MiB) comfortably clears a real house-map doc -- the guard
    # must never break the "large-but-bounded" legitimate case.
    c = _wrapped_client(tmp_path, monkeypatch, max_bytes=serve.DEFAULT_MAX_BODY_BYTES)
    r = c.put("/api/house", json=_valid_house(), headers=CSRF)
    assert r.status_code == 200
    assert (tmp_path / "house.json").exists()


def test_get_request_unaffected(tmp_path, monkeypatch):
    c = _wrapped_client(tmp_path, monkeypatch, max_bytes=1024)
    assert c.get("/api/house").status_code == 200


def test_max_bytes_zero_disables_the_guard(tmp_path, monkeypatch):
    # Documented escape hatch: <= 0 -> byte-identical to no guard at all.
    c = _wrapped_client(tmp_path, monkeypatch, max_bytes=0)
    r = c.put("/api/house", content=b"x" * 4096, headers={**CSRF, "Content-Type": "application/json"})
    assert r.status_code != 413   # not rejected by the guard (fails validation instead)


# --------------------------------------------------------------------------- #
# Low-level ASGI behaviour: the drain-and-replay path (no/lying Content-Length),
# and that non-HTTP scopes (websocket, lifespan) are never touched.
# --------------------------------------------------------------------------- #

def _http_scope(headers=()):
    return {"type": "http", "method": "PUT", "path": "/x", "headers": list(headers)}


async def _run_middleware(mw, scope, receive):
    sent = []

    async def send(message):
        sent.append(message)

    await mw(scope, receive, send)
    return sent


def test_drain_path_rejects_over_cap_body_with_no_content_length():
    # A chunked-style stream (no Content-Length header at all) that exceeds the cap
    # must still be rejected cleanly -- the header check alone cannot catch this.
    chunks = [b"a" * 50, b"b" * 50, b"c" * 50]  # 150 bytes total

    async def app(scope, receive, send):
        pytest.fail("wrapped app must never be invoked for an over-cap body")

    async def fake_receive():
        if chunks:
            body = chunks.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(chunks)}
        return {"type": "http.request", "body": b"", "more_body": False}

    mw = serve.MaxBodySizeMiddleware(app, max_bytes=100)
    sent = anyio.run(_run_middleware, mw, _http_scope(), fake_receive)
    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert starts and starts[0]["status"] == 413


def test_drain_path_replays_within_cap_body_byte_identical():
    chunks = [b"ab", b"cd"]
    received_bodies = []

    async def app(scope, receive, send):
        while True:
            msg = await receive()
            received_bodies.append(msg.get("body", b""))
            if not msg.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def fake_receive():
        if chunks:
            body = chunks.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(chunks)}
        return {"type": "http.request", "body": b"", "more_body": False}

    mw = serve.MaxBodySizeMiddleware(app, max_bytes=1000)
    sent = anyio.run(_run_middleware, mw, _http_scope(), fake_receive)
    assert received_bodies == [b"ab", b"cd"]        # replayed verbatim, in order
    assert sent[0]["status"] == 200


def test_non_http_scope_passes_through_untouched():
    # A websocket (or lifespan) scope must never be drained -- /ws/live streaming is
    # unaffected by this HTTP-body-only guard.
    calls = []

    async def app(scope, receive, send):
        calls.append(scope["type"])

    async def fake_receive():
        pytest.fail("must never be called for a non-http scope")

    mw = serve.MaxBodySizeMiddleware(app, max_bytes=10)
    anyio.run(_run_middleware, mw, {"type": "websocket"}, fake_receive)
    assert calls == ["websocket"]


# --------------------------------------------------------------------------- #
# Production wiring: `python -m wavr.serve` actually installs the guard around
# the real app before handing it to uvicorn.
# --------------------------------------------------------------------------- #

def _no_torch_warmup(monkeypatch):
    # main()'s torch/ultralytics main-thread warm-up (serve.py's own docstring:
    # Windows c10.dll WinError 1114 trap) is orthogonal to what these tests check
    # (which app object reaches uvicorn.run) and is slow/heavy to actually import in
    # a unit test -- force the `except ImportError` no-op branch instead of a real
    # torch import, same as a base install without the [camera] extra.
    import sys
    monkeypatch.setitem(sys.modules, "ultralytics", None)


def test_main_wraps_app_with_body_size_middleware(monkeypatch, tmp_path):
    captured = {}

    def fake_run(bound_app, **kwargs):
        captured["app"] = bound_app
        captured["kwargs"] = kwargs

    _no_torch_warmup(monkeypatch)
    monkeypatch.setattr(serve.uvicorn, "run", fake_run)
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(tmp_path / "house.json"))
    serve.main()
    assert isinstance(captured["app"], serve.MaxBodySizeMiddleware)
    assert captured["app"]._max_bytes == serve.DEFAULT_MAX_BODY_BYTES
    assert captured["kwargs"]["host"] == "127.0.0.1"


def test_main_honours_max_body_bytes_env_override(monkeypatch, tmp_path):
    captured = {}

    def fake_run(bound_app, **kwargs):
        captured["app"] = bound_app

    _no_torch_warmup(monkeypatch)
    monkeypatch.setattr(serve.uvicorn, "run", fake_run)
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(tmp_path / "house.json"))
    monkeypatch.setenv("WAVR_MAX_BODY_BYTES", "12345")
    serve.main()
    assert captured["app"]._max_bytes == 12345
