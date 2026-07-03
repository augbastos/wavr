from fastapi.testclient import TestClient
from wavr.app import create_app

def _client(narrator=None):
    app = create_app(sources=[], narrator=narrator)
    return TestClient(app, headers={"X-Wavr-Local": "1"})

class _FakeNarrator:
    def narrate(self, state, history):
        return "Casa vazia no momento."

def test_narrate_returns_text_when_configured():
    with _client(narrator=_FakeNarrator()) as c:
        r = c.post("/api/narrate")
        assert r.status_code == 200
        assert r.json()["narration"] == "Casa vazia no momento."

def test_narrate_503_when_not_configured(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with _client(narrator=None) as c:                     # no narrator, no key
        assert c.post("/api/narrate").status_code == 503

def test_narrate_requires_local_header():
    from wavr.app import create_app
    with TestClient(create_app(sources=[], narrator=_FakeNarrator())) as c:  # no header
        assert c.post("/api/narrate").status_code == 403

def test_narrate_502_on_generator_error():
    class _Boom:
        def narrate(self, state, history):
            raise RuntimeError("gemini down")
    with _client(narrator=_Boom()) as c:
        assert c.post("/api/narrate").status_code == 502

def test_narrate_503_when_key_present_but_flag_unset(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-present")
    monkeypatch.delenv("WAVR_NARRATE_ENABLED", raising=False)
    with _client(narrator=None) as c:                     # key set, but no opt-in flag
        assert c.post("/api/narrate").status_code == 503


def test_narrate_offloads_storage_recent_to_a_thread(monkeypatch):
    # LOW: /api/narrate must offload the SQLite `_storage.recent(50)` read via
    # asyncio.to_thread, same as /api/history -- not call it inline on the event loop.
    import asyncio

    from wavr.camera_store import CameraStore
    from wavr.storage import Storage

    calls = []
    orig_to_thread = asyncio.to_thread

    async def spy_to_thread(fn, *a, **k):
        calls.append(getattr(fn, "__name__", fn))
        return await orig_to_thread(fn, *a, **k)

    monkeypatch.setattr(asyncio, "to_thread", spy_to_thread)

    app = create_app(sources=[], storage=Storage(":memory:"), camera_store=CameraStore(":memory:"),
                     narrator=_FakeNarrator())
    with TestClient(app, headers={"X-Wavr-Local": "1"}) as c:
        r = c.post("/api/narrate")
    assert r.status_code == 200
    assert "recent" in calls      # the storage read is offloaded ...
    assert "narrate" in calls     # ... same as the narrate call already was
