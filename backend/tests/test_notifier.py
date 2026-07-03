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
