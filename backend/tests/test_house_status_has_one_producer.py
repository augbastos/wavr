"""Whatever the dashboard is told about the house, MQTT is told the same.

## The defect

`compose_house_status` was called from two places in `create_app`: once inside
`_compute_house_status`, which `GET /api/house-status` and the
`get_house_status` MCP tool both read, and once inside `_publish_derived_mqtt`,
which publishes RETAINED to `wavr/house/status`.

When the "nothing is watching" safeguard was added — the rule that a house
nothing observes must answer `unknown` rather than "Everything looks normal." —
it was added to the first call site only. The two then answered differently
about the same house at the same moment, and the reassuring one was the one
Home Assistant kept: retained, so a subscriber holds it until it is replaced,
and with no `checked` field to say which answer it was holding.

`_compute_house_status`'s own comment says it exists so these "can never
drift". That comment was true of the MCP tool and false of MQTT.

## What this pins

Not the wording, and not the verdict — those change as the house changes. It
pins that ONE function produces both, by driving a real app and comparing what
reached the broker with what the route returns.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from wavr.app import create_app


def _app(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "wavr.db"))
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(tmp_path / "house.json"))
    monkeypatch.setenv("WAVR_LOCAL_TOKEN", "")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    published: list[tuple[str, str, bool]] = []
    app = create_app(rules_publish=lambda t, p, r: published.append((t, p, r)))
    return app, published


def _house_topic(published):
    for topic, payload, _retain in reversed(published):
        if topic.endswith("/house/status"):
            return json.loads(payload)
    return None


@pytest.mark.parametrize("net_inventory", ["0", "1"])
def test_mqtt_and_the_route_answer_with_one_voice(tmp_path, monkeypatch,
                                                  net_inventory):
    """Both readings, from one app, one moment."""
    app, published = _app(tmp_path, monkeypatch,
                          WAVR_NET_INVENTORY=net_inventory)
    client = TestClient(app)

    asyncio.run(app.state.publish_derived_mqtt())
    broker = _house_topic(published)
    route = client.get("/api/house-status",
                       headers={"X-Wavr-Local": "1"}).json()

    assert broker is not None, (
        "nothing was published to the house-status topic, so this test is "
        f"measuring nothing. Topics seen: {sorted({t for t, _, _ in published})}")
    assert broker.get("status") == route.get("status"), (
        f"the broker was told {broker.get('status')!r} and the dashboard "
        f"{route.get('status')!r} about the same house at the same moment. "
        f"The broker's copy is RETAINED, so a subscriber keeps it.")


def test_the_broker_is_told_which_layers_were_checked(tmp_path, monkeypatch):
    """`checked` is what separates "nothing is wrong" from "nothing looked".

    Without it on the wire, a subscriber holding `status: ok` cannot tell
    whether anything was watching — which is the whole distinction the
    safeguard exists to make.
    """
    app, published = _app(tmp_path, monkeypatch, WAVR_NET_INVENTORY="0")
    asyncio.run(app.state.publish_derived_mqtt())
    broker = _house_topic(published)
    assert broker is not None
    assert "checked" in broker, (
        "the retained house-status payload does not say which layers reported, "
        f"so `status` cannot be read honestly: {sorted(broker)}")


def test_a_house_nothing_watches_is_unknown_on_both_surfaces(tmp_path, monkeypatch):
    """The safeguard itself, on the product's real defaults.

    The occupancy log is ON by default, and counting its empty table as a layer
    that reported made `unknown` unreachable on every fresh install: the tile
    said "Everything looks normal." over a Core with no cameras, no Watch and
    no network monitor. The existing tests call `compose_house_status` directly
    with a hand-made `sources_checked`, which cannot see that.
    """
    app, published = _app(tmp_path, monkeypatch, WAVR_NET_INVENTORY="0")
    client = TestClient(app)
    route = client.get("/api/house-status",
                       headers={"X-Wavr-Local": "1"}).json()
    assert route.get("status") == "unknown", (
        "a Core with nothing observing reported "
        f"{route.get('status')!r} with checked={route.get('checked')!r} — "
        f"reassurance nobody earned")
    assert not route.get("checked"), route.get("checked")

    asyncio.run(app.state.publish_derived_mqtt())
    broker = _house_topic(published)
    assert broker and broker.get("status") == "unknown", broker
