from __future__ import annotations

import logging
from typing import Callable

from wavr.mqtt_topics import status_topic

_CLIENT = None
_WARNED = False


def _client(host: str, port: int, prefix: str = "wavr"):
    """Lazily create + connect a paho MQTT client (once). Lazy import so paho is
    only needed on the real path; connect_async + loop_start means publish never
    blocks and reconnects on its own if the broker is down.

    Registers a retained Last Will on `{prefix}/status` = "offline" BEFORE
    connecting, and (re)publishes "online" on every successful connect. The broker
    fires the will whenever the connection drops without a clean DISCONNECT --
    crash, sleep, or a plain process exit (we never call disconnect()) -- so Home
    Assistant flips Wavr's entities to *unavailable* instead of showing retained
    presence forever."""
    global _CLIENT
    if _CLIENT is None:
        import paho.mqtt.client as mqtt   # optional dep, only imported when MQTT is enabled
        status = status_topic(prefix)
        c = mqtt.Client()
        c.will_set(status, "offline", qos=1, retain=True)   # MUST precede connect

        def _on_connect(client, *_args, **_kwargs):
            # Republish on every (re)connect so a broker restart re-announces us.
            client.publish(status, "online", qos=1, retain=True)

        c.on_connect = _on_connect
        c.connect_async(host, port)
        c.loop_start()
        _CLIENT = c
    return _CLIENT


def make_publisher(host: str = "localhost", port: int = 1883,
                   prefix: str = "wavr") -> Callable[[str, str, bool], None]:
    def publish(topic: str, payload: str, retain: bool) -> None:
        global _WARNED
        try:
            _client(host, port, prefix).publish(topic, payload, retain=retain)
        except ImportError:
            # MQTT enabled but the optional dep isn't installed: warn once, then no-op.
            if not _WARNED:
                logging.warning("MQTT enabled but paho-mqtt not installed; "
                                "publishing is a no-op. Run: pip install -e backend[mqtt]")
                _WARNED = True
        except Exception as exc:
            # A dead/unreachable broker -- or an illegal topic paho rejects -- must
            # not crash the rules loop, but it must NOT vanish silently either.
            logging.warning("MQTT publish to %r failed: %s", topic, exc)
    return publish
