import asyncio
import threading
import time

from wavr import notifier as nt


def test_make_notifier_posts_message_bytes_to_url():
    calls = []

    def fake_post(url, body):
        calls.append((url, body))

    notify = nt.make_notifier("http://nas.local:8080/wavr", post=fake_post)
    notify("Wavr: alguém chegou em casa")
    assert calls == [("http://nas.local:8080/wavr", "Wavr: alguém chegou em casa".encode("utf-8"))]


def test_notify_payload_is_plain_message_only_no_leaks():
    # Derived-only: the wire payload must be exactly the human message -- no
    # coordinates, vitals, or MAC addresses ever ride along.
    calls = []
    notify = nt.make_notifier("http://nas.local:8080/wavr", post=lambda url, body: calls.append(body))
    notify("Wavr: dispositivo desconhecido na rede (Espressif)")
    assert len(calls) == 1
    text = calls[0].decode("utf-8")
    assert text == "Wavr: dispositivo desconhecido na rede (Espressif)"
    lowered = text.lower()
    for leak in ("mac", "lat", "lon", "x=", "y=", "rssi", "bpm"):
        assert leak not in lowered


def test_notify_never_raises_on_dead_server(monkeypatch):
    monkeypatch.setattr(nt, "_WARNED", False)

    def raising_post(url, body):
        raise OSError("connection refused")

    notify = nt.make_notifier("http://nas.local:8080/wavr", post=raising_post)
    notify("Wavr: casa vazia")   # must not raise
    notify("Wavr: casa vazia")   # second failure -- still must not raise (warn-once)


def test_default_transport_not_used_when_post_injected(monkeypatch):
    # No real urllib call should happen when a fake transport is injected --
    # proves the transport is genuinely swappable (opt-in / no real network in tests).
    def boom(*a, **k):
        raise AssertionError("real urllib transport must not be used in tests")

    monkeypatch.setattr(nt, "_urllib_post", boom)
    calls = []
    notify = nt.make_notifier("http://nas.local:8080/wavr", post=lambda url, body: calls.append(body))
    notify("Wavr: alguém chegou em casa")
    assert calls


# --- MEDIUM: notify() must not block the event loop when called from async code ----
# (AwayMonitor / NetworkInventoryService.on_rogue both call notify() synchronously
# from inside a running loop -- a slow/unreachable ntfy server must not freeze it.)

async def test_notify_returns_immediately_when_called_from_a_running_loop():
    release = threading.Event()
    posted = []

    def slow_post(url, body):
        release.wait(2)     # blocks only the WORKER thread, never the event loop
        posted.append(body)

    notify = nt.make_notifier("http://nas.local:8080/wavr", post=slow_post)

    t0 = time.monotonic()
    notify("Wavr: casa vazia")           # called synchronously, as away.py/app.py do
    assert (time.monotonic() - t0) < 0.1  # must return promptly, not after the "post"

    # The loop stays free to run other coroutines while the post is still in flight.
    await asyncio.sleep(0)
    assert posted == []

    release.set()
    for _ in range(100):
        if posted:
            break
        await asyncio.sleep(0.01)
    assert posted == [b"Wavr: casa vazia"]


async def test_notify_offloaded_failure_still_warns_once_never_raises(monkeypatch):
    monkeypatch.setattr(nt, "_WARNED", False)
    done = threading.Event()

    def raising_post(url, body):
        try:
            raise OSError("connection refused")
        finally:
            done.set()

    notify = nt.make_notifier("http://nas.local:8080/wavr", post=raising_post)
    notify("Wavr: casa vazia")    # must not raise, even though the POST is offloaded
    # Poll via asyncio.sleep (NOT a blocking Event.wait here) -- the task was only
    # scheduled, not started, so the event loop must get turns to actually run it.
    for _ in range(200):
        if done.is_set():
            break
        await asyncio.sleep(0.01)
    assert done.is_set()          # the offloaded post did run...
    await asyncio.sleep(0)        # ...and its failure was swallowed, not propagated


def test_notify_without_a_running_loop_still_runs_inline():
    # Outside a running event loop (e.g. a plain sync caller) there's nothing to
    # offload from, so notify() must behave exactly as before: synchronous, immediate.
    calls = []
    notify = nt.make_notifier("http://nas.local:8080/wavr", post=lambda url, body: calls.append(body))
    notify("Wavr: alguém chegou em casa")
    assert calls == ["Wavr: alguém chegou em casa".encode("utf-8")]
