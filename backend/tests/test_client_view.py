"""The native client snapshot projects the Core's answers; it never decides."""
import json

from wavr import client_view as cv


def _healthy():
    return {"state": "healthy", "headline": "ok", "findings": []}


def test_non_finite_and_boolean_numbers_are_unknown_not_values():
    snap = cv.snapshot({**_healthy(), "uptime_s": float("nan")}, None,
                       {"office": {"confidence": float("inf"), "person_count": True,
                                   "occupied": 1}})
    room = snap["rooms"][0]
    assert snap["runtime"]["uptime_s"] is None
    assert room["confidence"] is None and room["person_count"] is None
    assert room["occupied"] is None          # 1 is not a boolean answer
    json.dumps(snap, allow_nan=False)        # the snapshot is always strict JSON


def test_the_snapshot_never_carries_positions_identities_or_vitals():
    room = {"occupied": True, "targets": [{"x": 1, "y": 2}], "identities": ["alice"],
            "vitals": {"hr": 60}, "sources": []}
    text = json.dumps(cv.snapshot(_healthy(), None, {"office": room}))
    for leak in ("alice", "targets", "identities", "vitals", "hr"):
        assert leak not in text


def test_a_garbled_verdict_is_never_a_clean_bill():
    assert cv.snapshot({"state": 3}, None, {})["exit_code"] == cv.UNREACHABLE
    assert cv.snapshot(_healthy(), {"total": "0", "could_not_check": []}, {})["exit_code"] \
        == cv.ATTENTION
    # Control: the same answers, well formed, are clean.
    assert cv.snapshot(_healthy(), {"total": 0, "could_not_check": []}, {})["exit_code"] == cv.OK


def test_the_exit_code_is_wavr_status_own():
    from wavr.status import exit_code
    for rt, att in [({"state": "healthy"}, {"total": 2, "could_not_check": []}),
                    ({"state": "degraded"}, {"total": 0, "could_not_check": []}),
                    ({"state": "unavailable"}, None),
                    ({"state": "healthy"}, {"total": 0, "could_not_check": ["x"]})]:
        assert cv.snapshot(rt, att, {})["exit_code"] == exit_code(rt, att)
