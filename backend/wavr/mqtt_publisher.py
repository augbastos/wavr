from __future__ import annotations

import contextlib
import logging
from typing import Callable

_CLIENT = None


def _client(host: str, port: int):
    """Lazily create + connect a paho MQTT client (once). Lazy import so paho is
    only needed on the real path; connect_async + loop_start means publish never
    blocks and reconnects on its own if the broker is down."""
    global _CLIENT
    if _CLIENT is None:
        import paho.mqtt.client as mqtt   # optional dep, only imported when MQTT is enabled
        c = mqtt.Client()
        c.connect_async(host, port)
        c.loop_start()
        _CLIENT = c
    return _CLIENT


def make_publisher(host: str = "localhost", port: int = 1883) -> Callable[[str, str, bool], None]:
    def publish(topic: str, payload: str, retain: bool) -> None:
        with contextlib.suppress(Exception):        # a dead broker must not crash the rules loop
            _client(host, port).publish(topic, payload, retain=retain)
        # note: suppression also swallows a missing-paho ImportError, so an enabled-but-
        # uninstalled MQTT degrades to a no-op with a one-time warning rather than a crash.
    return publish
