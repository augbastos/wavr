from __future__ import annotations

import logging
from typing import Callable

_CLIENT = None
_WARNED = False


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
        global _WARNED
        try:
            _client(host, port).publish(topic, payload, retain=retain)
        except ImportError:
            # MQTT enabled but the optional dep isn't installed: warn once, then no-op.
            if not _WARNED:
                logging.warning("MQTT enabled but paho-mqtt not installed; "
                                "publishing is a no-op. Run: pip install -e backend[mqtt]")
                _WARNED = True
        except Exception:
            pass  # a dead/unreachable broker must not crash the rules loop
    return publish
