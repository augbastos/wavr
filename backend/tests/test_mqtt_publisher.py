import pytest
from wavr import mqtt_publisher as mp


def test_make_publisher_calls_client_publish(monkeypatch):
    calls = []

    class FakeClient:
        def publish(self, topic, payload, retain=False):
            calls.append((topic, payload, retain))

    monkeypatch.setattr(mp, "_client", lambda host, port: FakeClient())
    publish = mp.make_publisher("localhost", 1883)
    publish("wavr/rooms/sala/state", '{"occupied": true}', True)
    assert calls == [("wavr/rooms/sala/state", '{"occupied": true}', True)]


def test_publisher_never_raises_on_client_error(monkeypatch):
    class BadClient:
        def publish(self, *a, **k):
            raise RuntimeError("broker down")

    monkeypatch.setattr(mp, "_client", lambda host, port: BadClient())
    publish = mp.make_publisher()
    publish("t", "p", False)   # must NOT raise — a dead broker can't crash the rules loop
