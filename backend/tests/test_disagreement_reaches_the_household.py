"""A contradiction between sensors must reach the people who own the house.

The computation itself is tested in `test_mcp_disagreement.py`, which predates
this and still passes against the same function. What is tested here is who gets
told.

For a while the answer was: an AI agent, and nobody else. `_disagreement` lived
inside `mcp.py`, so a program talking to the house could be told that the radar
and the camera contradict each other, and the household could not — they got a
confidence number, which is a summary, and a summary of a contradiction is the
one place a summary lies.
"""
import pytest

from wavr.disagreement import disagreement


def src(sensor_id, modality, presence, health="fresh", count=None):
    return {"sensor_id": sensor_id, "modality": modality, "presence": presence,
            "health": health, "count": count}


def test_one_producer_serves_both_the_agent_and_the_dashboard():
    """`mcp` must not have its own copy. Two implementations of "do these
    sensors agree" eventually differ, in front of somebody with no way to tell
    which is right."""
    from wavr import mcp

    assert mcp._disagreement is disagreement, (
        "the MCP surface has drifted onto its own copy of the disagreement rule")


def test_the_room_state_the_dashboard_reads_carries_the_disagreement():
    """`/api/state` is what the dashboard renders. The field has to be in the
    room dict, not computed by the client — a client computing it is the second
    implementation this module exists to prevent."""
    import inspect

    from wavr import app as appmod

    source = inspect.getsource(appmod.create_app)
    assert 'd["disagreement"] = room_disagreement(' in source, (
        "the publish path no longer attaches the disagreement, so the dashboard "
        "is back to being told less than an agent")


def test_a_stale_sensor_is_absent_rather_than_dissenting():
    """A sensor that stopped reporting is not disagreeing. Counting it as
    dissent manufactures a contradiction out of something being unplugged, and
    a person acting on that would go looking for a fault that is not there."""
    out = disagreement([src("cam", "camera", False, health="stale"),
                        src("radar", "mmwave", True)])
    assert out["disagree"] is False


def test_the_report_says_how_wavr_WEIGHED_it_not_only_that_it_happened():
    """Two sensors saying empty and one saying occupied looks like a majority
    that should have won. It did not, and the reason has to travel with the
    disagreement or the room's answer reads as a mistake."""
    out = disagreement([src("cam", "camera", False),
                        src("ble", "ble", False),
                        src("radar", "mmwave", True)])
    assert out["disagree"] is True
    assert "no weight" in out["note"] or "weaker evidence" in out["note"]
    says = {s["sensor_id"]: s["says"] for s in out["sensors"]}
    assert says == {"cam": "empty", "ble": "empty", "radar": "occupied"}


def test_a_count_disagreement_is_flagged_separately_from_a_presence_one():
    """"Somebody is here" and "how many" are different questions, and a room can
    agree on the first while disagreeing on the second."""
    out = disagreement([src("cam", "camera", True, count=2),
                        src("radar", "mmwave", False, count=None)])
    assert out["disagree"] is True
    assert "counts_disagree" in out
