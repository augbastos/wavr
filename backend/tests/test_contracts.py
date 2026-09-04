"""Every published shape carries a version, and nothing defines its own.

This file exists because versions were scattered — eight shapes each defining
their own constant next to their producer, two carrying none at all. That is
fine until somebody adds a ninth, and a contract that ships unversioned cannot
be changed afterwards without breaking something silently: there is nothing in
the payload for a consumer to check.

So the tests here are guards on the ARRANGEMENT rather than on any one value.
"""
import re
from pathlib import Path

import pytest

from wavr.contracts import CONTRACTS, summary, version

WAVR = Path(__file__).resolve().parents[1] / "wavr"


def test_every_contract_has_a_version():
    for name, v in CONTRACTS.items():
        assert isinstance(v, int) and v >= 1, name


def test_an_unknown_contract_fails_loudly_rather_than_defaulting():
    """A producer stamping a contract this table has never heard of is a
    contract nobody can version, and a `0` would let it ship looking like it
    had one."""
    with pytest.raises(KeyError):
        version("something_nobody_declared")


def test_no_producer_defines_its_own_version_number():
    """The arrangement this file protects. A second definition drifts, and the
    drift is two components disagreeing about what "version 1" means."""
    offenders = []
    for path in WAVR.glob("*.py"):
        if path.name == "contracts.py":
            continue
        src = path.read_text(encoding="utf-8")
        for match in re.finditer(
                r"^([A-Z_]*(?:PROTOCOL|CONTRACT|FORMAT)?_?VERSION)\s*=\s*(\d+)\s*$",
                src, re.M):
            offenders.append(f"{path.name}: {match.group(1)} = {match.group(2)}")
    assert offenders == [], (
        "these define a version literal instead of importing it from "
        f"`contracts`: {offenders}")


def test_the_shapes_that_had_no_version_now_carry_one():
    """`spatial_events` and `anchors` shipped unversioned. A consumer had
    nothing to check when a field's meaning changed."""
    assert "spatial_events" in CONTRACTS and "anchors" in CONTRACTS


def test_an_event_carries_its_version_on_every_frame():
    """Not only at a discovery endpoint: an event arrives on a socket a consumer
    may have opened before that endpoint existed, and a version it has to fetch
    separately is a version half of them will not fetch."""
    from wavr.spatial_events import SpatialEvents
    ev = SpatialEvents()
    state = {"room": "kitchen", "occupied": False, "confidence": 0.0,
             "person_count": None, "precision_level": "room", "sources": [],
             "ts": "2026-09-04T12:00:00+00:00"}
    ev.observe(state)
    out = ev.observe({**state, "occupied": True})
    assert out[0]["v"] == version("spatial_events")


def test_an_anchor_listing_carries_its_version_once():
    """Anchors arrive as a list from one endpoint, so one stamp is enough and
    thirty copies would be noise."""
    from wavr.anchors import AnchorStore, summarize
    store = AnchorStore(":memory:")
    store.create("Counter", "kitchen")
    out = summarize(store.list(), ["kitchen"])
    assert out["v"] == version("anchors")
    assert "v" not in out["anchors"][0], "not repeated per row"


def test_the_summary_says_what_a_bump_means():
    """The bar is not "we changed something". It is "an old reader would now be
    wrong"."""
    body = summary()
    assert "Adding a field is not a break" in body["compatibility"]
    assert "removed or" in body["compatibility"]


def test_the_summary_tells_a_client_what_to_do_with_a_newer_core():
    """Refusing would break every installed application the day somebody updates
    their Core — and that somebody is not whoever wrote the app."""
    assert "Keep reading the fields you know" in summary()["if_this_core_is_newer"]


def test_the_context_the_export_and_the_bundle_all_read_from_the_table():
    from wavr.api_experience import EXPERIENCE_PROTOCOL_VERSION
    from wavr.apple import APPLE_PROTOCOL_VERSION
    from wavr.config_export import BUNDLE_VERSION, CONFIG_VERSION
    assert EXPERIENCE_PROTOCOL_VERSION == version("experience_context")
    assert CONFIG_VERSION == version("config_export")
    assert BUNDLE_VERSION == version("diagnostic_bundle")
    assert APPLE_PROTOCOL_VERSION == version("apple_spatial")


def test_the_developer_endpoint_reports_the_whole_table():
    """A developer asking "what does this Core speak" should not find out one
    404 at a time."""
    from wavr.api_developer import DEVELOPER_PROTOCOL
    assert set(DEVELOPER_PROTOCOL) <= set(CONTRACTS)
    for name, v in DEVELOPER_PROTOCOL.items():
        assert v == CONTRACTS[name], name
