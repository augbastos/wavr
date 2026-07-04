import logging
import sys
import types

from wavr import mqtt_publisher as mp


def test_make_publisher_calls_client_publish(monkeypatch):
    calls = []

    class FakeClient:
        def publish(self, topic, payload, retain=False):
            calls.append((topic, payload, retain))

    monkeypatch.setattr(mp, "_client", lambda host, port, prefix: FakeClient())
    publish = mp.make_publisher("localhost", 1883)
    publish("wavr/rooms/sala/state", '{"occupied": true}', True)
    assert calls == [("wavr/rooms/sala/state", '{"occupied": true}', True)]


def test_publisher_never_raises_on_client_error(monkeypatch):
    class BadClient:
        def publish(self, *a, **k):
            raise RuntimeError("broker down")

    monkeypatch.setattr(mp, "_client", lambda host, port, prefix: BadClient())
    publish = mp.make_publisher()
    publish("t", "p", False)   # must NOT raise — a dead broker can't crash the rules loop


def test_publish_failure_is_logged_not_silently_swallowed(monkeypatch, caplog):
    # Regression: the old bare `except Exception: pass` dropped e.g. paho's
    # ValueError on an illegal topic with no trace anywhere. It must be logged now.
    class BadClient:
        def publish(self, *a, **k):
            raise ValueError("Publish topic cannot contain wildcards.")

    monkeypatch.setattr(mp, "_client", lambda host, port, prefix: BadClient())
    publish = mp.make_publisher()
    with caplog.at_level(logging.WARNING):
        publish("wavr/rooms/kids_1/state", "p", True)   # still must not raise
    assert "MQTT publish" in caplog.text
    assert "wavr/rooms/kids_1/state" in caplog.text


# --- Last Will / availability wiring inside the real _client() ---------------

class _FakeClient:
    def __init__(self, rec):
        self._rec = rec
        self.on_connect = None
        rec["instances"].append(self)

    def will_set(self, topic, payload, qos=0, retain=False):
        self._rec["order"].append("will_set")
        self._rec["will"] = (topic, payload, qos, retain)

    def connect_async(self, host, port):
        self._rec["order"].append("connect_async")
        self._rec["connect"] = (host, port)

    def loop_start(self):
        self._rec["order"].append("loop_start")

    def publish(self, topic, payload, qos=0, retain=False):
        self._rec["published"].append((topic, payload, qos, retain))


def _install_fake_paho(monkeypatch, rec):
    """Inject a fake `paho.mqtt.client` so _client() runs without paho installed."""
    client_mod = types.ModuleType("paho.mqtt.client")
    client_mod.Client = lambda *a, **k: _FakeClient(rec)
    mqtt_pkg = types.ModuleType("paho.mqtt")
    mqtt_pkg.client = client_mod
    paho_pkg = types.ModuleType("paho")
    paho_pkg.mqtt = mqtt_pkg
    monkeypatch.setitem(sys.modules, "paho", paho_pkg)
    monkeypatch.setitem(sys.modules, "paho.mqtt", mqtt_pkg)
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", client_mod)


def _fresh_rec():
    return {"order": [], "published": [], "instances": []}


def test_client_registers_lwt_offline_before_connect(monkeypatch):
    rec = _fresh_rec()
    _install_fake_paho(monkeypatch, rec)
    monkeypatch.setattr(mp, "_CLIENT", None)
    mp._client("brokerhost", 1883, "wavr")
    assert rec["will"] == ("wavr/status", "offline", 1, True)
    # the will MUST be set before connecting or the broker never registers it
    assert rec["order"].index("will_set") < rec["order"].index("connect_async")
    assert rec["connect"] == ("brokerhost", 1883)


def test_client_publishes_online_on_connect(monkeypatch):
    rec = _fresh_rec()
    _install_fake_paho(monkeypatch, rec)
    monkeypatch.setattr(mp, "_CLIENT", None)
    c = mp._client("h", 1883, "casa")            # prefix flows into the status topic
    assert rec["published"] == []                # nothing until an actual connect
    c.on_connect(c, None, None, 0)               # simulate broker (re)connect
    assert ("casa/status", "online", 1, True) in rec["published"]


def test_client_is_a_singleton(monkeypatch):
    rec = _fresh_rec()
    _install_fake_paho(monkeypatch, rec)
    monkeypatch.setattr(mp, "_CLIENT", None)
    a = mp._client("h", 1, "wavr")
    b = mp._client("h", 1, "wavr")
    assert a is b
    assert len(rec["instances"]) == 1            # connected exactly once
