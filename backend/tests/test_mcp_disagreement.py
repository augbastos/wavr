"""An agent must be able to see sensors contradicting each other.

Wavr's differentiator is that it can be interrogated. A merged number with the
dissent hidden underneath is exactly the "86% that means nothing" the product
refuses — so the contradiction has to reach the AI surface, along with how Wavr
weighed it, or the agent has to guess which side won and will sometimes guess
wrong.
"""
from wavr.mcp import _NEXT_IN_WORDS, _disagreement, explain_room_state


def src(sensor_id, modality, presence, health="fresh", count=None):
    return {"sensor_id": sensor_id, "modality": modality, "presence": presence,
            "health": health, "count": count, "confidence": 0.9, "age_s": 0}


class _Provider:
    def __init__(self, state):
        self._state = state

    def list_rooms(self):
        return [self._state["room"]]

    def room_state(self, room):
        return self._state if room == self._state["room"] else None


def _state(sources, **kw):
    base = {"room": "office", "occupied": True, "confidence": 0.62,
            "person_count": None, "sources": sources,
            "explanation": "", "precision_level": "room",
            "precision_next": "add_counting_sensor",
            "ts": "2026-09-03T12:00:00+00:00"}
    base.update(kw)
    return base


# -- Detecting the contradiction ----------------------------------------------

def test_agreement_is_not_reported_as_disagreement():
    out = _disagreement([src("a", "camera", True), src("b", "ble", True)])
    assert out["disagree"] is False and out["sensors"] == []


def test_two_sensors_that_contradict_are_reported_with_both_positions():
    out = _disagreement([src("office-cam", "camera", False),
                         src("office-radar", "mmwave", True)])
    assert out["disagree"] is True
    says = {s["sensor_id"]: s["says"] for s in out["sensors"]}
    assert says == {"office-cam": "empty", "office-radar": "occupied"}


def test_a_stale_sensor_is_absent_not_dissenting():
    """Reporting a stale sensor as dissent manufactures a contradiction out of
    something being unplugged, and sends an agent looking for a fault that is
    really just a dead battery."""
    out = _disagreement([src("office-radar", "mmwave", True),
                         src("office-cam", "camera", False, health="stale")])
    assert out["disagree"] is False


def test_a_dead_sensor_is_ignored_too():
    out = _disagreement([src("a", "mmwave", True),
                         src("b", "camera", False, health="dead")])
    assert out["disagree"] is False


def test_conflicting_counts_are_flagged_separately():
    """Two counting sensors that agree somebody is there and disagree about how
    many is a different problem from one saying empty."""
    out = _disagreement([src("cam-a", "camera", True, count=2),
                         src("cam-b", "camera", True, count=5)])
    assert out["disagree"] is False, "they agree the room is occupied"

    out2 = _disagreement([src("cam-a", "camera", True, count=2),
                          src("cam-b", "camera", False, count=0)])
    assert out2["disagree"] is True and out2["counts_disagree"] is True


def test_the_report_says_how_wavr_weighed_it():
    """Otherwise the agent has to infer which side won, and will sometimes infer
    wrong. Absence carries no mass in the merge, and saying so is the difference
    between a fact and a puzzle."""
    out = _disagreement([src("a", "camera", False), src("b", "mmwave", True)])
    assert "no weight" in out["note"]
    assert "still person" in out["note"]


# -- It reaches the tool -------------------------------------------------------

def test_explain_room_state_carries_the_disagreement():
    provider = _Provider(_state([src("office-cam", "camera", False),
                                 src("office-radar", "mmwave", True)]))
    body = explain_room_state(provider, "office")
    assert body["disagreement"]["disagree"] is True
    assert len(body["disagreement"]["sensors"]) == 2


def test_a_quiet_room_carries_an_explicit_no_disagreement():
    """Present and False, not absent. An agent checking `if disagreement:` on a
    missing key would read every agreeing room as contradictory."""
    provider = _Provider(_state([src("a", "camera", True)]))
    body = explain_room_state(provider, "office")
    assert body["disagreement"] == {"disagree": False, "sensors": []}


# -- The next step, in words ---------------------------------------------------

def test_the_improvement_hint_is_plain_language():
    """`add_counting_sensor` requires knowing Wavr's internal vocabulary. An
    agent should be able to act on the answer without it."""
    provider = _Provider(_state([src("a", "ble", True)]))
    body = explain_room_state(provider, "office")
    assert "how many" in body["precision"]["how_to_improve"]
    assert body["precision"]["next"] == "add_counting_sensor", "the key survives"


def test_a_room_at_the_top_rung_offers_no_empty_suggestion():
    """An empty suggestion is worse than none — it reads as advice and says
    nothing."""
    provider = _Provider(_state([src("a", "camera", True)],
                                precision_level="position",
                                precision_next=None))
    body = explain_room_state(provider, "office")
    assert body["precision"]["how_to_improve"] is None


def test_every_known_next_key_has_words():
    """A key with no sentence would silently degrade to None and an agent would
    conclude nothing can be improved."""
    from wavr.fusion import _NEXT_BY_RANK

    for key in _NEXT_BY_RANK.values():
        if key is not None:
            assert key in _NEXT_IN_WORDS, f"{key} has no plain-language form"


# -- Privacy holds -------------------------------------------------------------

def test_the_disagreement_report_carries_no_person_data():
    provider = _Provider(_state(
        [src("office-cam", "camera", False), src("office-radar", "mmwave", True)],
        identities=[{"person": "Sam"}],
        targets=[{"x": 1.0, "y": 2.0}]))
    blob = str(explain_room_state(provider, "office")["disagreement"])
    assert "Sam" not in blob
    assert "x" not in blob.replace("counts_disagree", "")
