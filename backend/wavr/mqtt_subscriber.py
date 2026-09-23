"""Reading somebody else's MQTT broker, for the adapters that need to.

Wavr already PUBLISHES to MQTT (`mqtt_publisher`). The ESPresense and Frigate
adapters are the first things that SUBSCRIBE, and they share this one reader
rather than each growing its own paho plumbing.

Three rules, each load-bearing:

**Exact topics only.** The caller names every topic it wants, and a wildcard is
refused here rather than trusted to the caller. Frigate publishes a RETAINED JPEG
on `frigate/<camera>/<label>/snapshot`, and ESPresense publishes phones' IRKs --
the keys that de-anonymise them -- on its settings topics. A `frigate/#` or an
`espresense/#` would receive both the moment it connected. `+` is allowed only
as the LAST level, where the adapters use it for "any node" under one device.

**paho is the [mqtt] extra and imported only when an adapter runs.** A Core with
no adapter configured never loads it (`test_the_base_core_stays_light`).

**A slow Wavr drops the oldest message, never grows without bound.** paho calls
back on its own network thread; messages cross into asyncio through a bounded
queue. A broker flooding a busy Core loses history, which is what a live sensor
reading can afford to lose, instead of memory.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import AsyncIterator

_LOG = logging.getLogger(__name__)

# Delivered to the consumer alongside real messages so it can tell "the broker
# went away" (its evidence is no longer being refreshed) from "nothing happened".
CONNECTED = "$wavr/connected"
DISCONNECTED = "$wavr/disconnected"


class TopicRefused(ValueError):
    """A subscription that could receive topics the caller did not name."""


def check_topic(topic: str) -> str:
    """The topic, if it names exactly what the adapter asked for."""
    if not isinstance(topic, str) or not topic or len(topic) > 512:
        raise TopicRefused("a topic must be a non-empty string")
    levels = topic.split("/")
    if "#" in topic:
        raise TopicRefused(f"{topic!r}: '#' would subscribe to everything below it")
    for i, level in enumerate(levels):
        if "+" in level and (level != "+" or i != len(levels) - 1):
            raise TopicRefused(f"{topic!r}: '+' is allowed only as the last level")
        if not level:
            raise TopicRefused(f"{topic!r}: empty topic level")
    return topic


async def mqtt_messages(host: str, port: int, topics, *, username: str = "",
                        password: str = "", client_id: str = "",
                        queue_max: int = 2000) -> AsyncIterator[tuple[str, bytes, bool]]:
    """Yield `(topic, payload, retained)` for every message on `topics`, forever.

    Reconnects on its own (paho's loop, 1 s to 60 s backoff) and yields a
    `CONNECTED` / `DISCONNECTED` marker on each transition. The subscription is
    re-issued on every reconnect because the session is clean. Closing the
    generator stops the network thread.
    """
    topics = [check_topic(t) for t in topics]
    import paho.mqtt.client as mqtt       # the [mqtt] extra; see module docstring

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=queue_max)

    def put(item) -> None:               # runs on the event loop
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(item)

    def on_connect(client, _userdata, _flags, reason_code, _properties=None):
        if getattr(reason_code, "is_failure", False):
            # Wrong credentials look like this. Said once per attempt, without
            # the password, which paho never hands back anyway.
            _LOG.warning("MQTT %s:%s refused the connection: %s", host, port, reason_code)
            return
        for t in topics:
            client.subscribe(t, qos=0)
        loop.call_soon_threadsafe(put, (CONNECTED, b"", False))

    def on_disconnect(_client, _userdata, _flags, _reason_code, _properties=None):
        loop.call_soon_threadsafe(put, (DISCONNECTED, b"", False))

    def on_message(_client, _userdata, msg):
        loop.call_soon_threadsafe(put, (msg.topic, bytes(msg.payload), bool(msg.retain)))

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=client_id or "", clean_session=True)
    if username:
        client.username_pw_set(username, password or None)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    client.connect_async(host, int(port))
    client.loop_start()
    try:
        while True:
            yield await queue.get()
    finally:
        with contextlib.suppress(Exception):
            client.disconnect()
        with contextlib.suppress(Exception):
            client.loop_stop()


async def tracked_events(stream, tracker, reassert_s: float, now_fn) -> AsyncIterator:
    """Messages from `stream` fed to `tracker.on_message`, plus a periodic
    `tracker.tick`, as one flow of the events those return.

    The stream is drained by its own task into a queue: waiting on the queue with
    a timeout is safe, while cancelling a pending `__anext__` of an async
    generator would close it -- and with it the broker connection.
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=2000)

    async def drain():
        try:
            async with contextlib.aclosing(stream):
                async for item in stream:
                    await queue.put(item)
        except Exception as exc:          # noqa: BLE001 -- handed to the consumer
            await queue.put(exc)
            return
        await queue.put(None)             # the stream ended

    task = asyncio.create_task(drain())
    next_tick = time.monotonic() + reassert_s
    try:
        while True:
            try:
                item = await asyncio.wait_for(
                    queue.get(), timeout=max(0.0, next_tick - time.monotonic()))
            except asyncio.TimeoutError:
                next_tick = time.monotonic() + reassert_s
                for ev in tracker.tick(now_fn()):
                    yield ev
                continue
            if isinstance(item, Exception):
                # A missing paho, a refused broker address: the supervisor
                # records it and backs off, so the fault is visible.
                raise item
            if item is None:
                return                     # supervisor sees `completed`
            topic, payload, _retained = item
            for ev in tracker.on_message(topic, payload, now_fn()):
                yield ev
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
