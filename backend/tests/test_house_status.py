"""Build A10 v0: unit tests for the pure `wavr.house_status.compose_house_status`
composer -- the ok path, each layer firing on its own, the recency-window honesty
rule for network alerts, the worst-reason score/status ranking, and the evidence-
trail shape. App-level wiring (GET /api/house-status through the real create_app)
is covered separately in tests/test_house_status_wiring.py.
"""
from datetime import datetime, timedelta, timezone

from wavr.alert_severity import (SEVERITY_ALERT, SEVERITY_CRITICAL, SEVERITY_INFO,
                                 SEVERITY_NOTE, SEVERITY_WATCH)
from wavr.house_status import compose_house_status

NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)


def _ts(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def test_ok_when_nothing_fires():
    out = compose_house_status(now=NOW)
    assert out == {"status": "ok", "score": 0, "reasons": [], "ts": NOW.isoformat()}


def test_network_alert_within_window_surfaces_as_a_reason():
    alerts = [{"kind": "rogue_dhcp", "severity": SEVERITY_ALERT, "ts": _ts(5),
              "extra_server": "10.0.0.99"}]
    out = compose_house_status(network_alerts=alerts, now=NOW)
    assert out["status"] == "alert" and out["score"] == 4
    r = out["reasons"][0]
    assert r["layer"] == "network" and r["kind"] == "rogue_dhcp"
    assert "10.0.0.99" in r["what"]
    assert r["severity"] == SEVERITY_ALERT and r["ts"] == _ts(5)


def test_network_alert_outside_window_is_dropped():
    # default window is 60 minutes -- a two-hour-old sighting must not pin the
    # LIVE house-status banner at alert forever (see module docstring).
    alerts = [{"kind": "rogue_dhcp", "severity": SEVERITY_ALERT, "ts": _ts(120)}]
    out = compose_house_status(network_alerts=alerts, now=NOW)
    assert out == {"status": "ok", "score": 0, "reasons": [], "ts": NOW.isoformat()}


def test_network_alert_window_is_overridable():
    alerts = [{"kind": "rogue_dhcp", "severity": SEVERITY_ALERT, "ts": _ts(120)}]
    out = compose_house_status(network_alerts=alerts, now=NOW, window_minutes=180)
    assert out["status"] == "alert"


def test_network_alert_malformed_ts_never_crashes():
    alerts = [{"kind": "rogue_dhcp", "severity": SEVERITY_ALERT, "ts": "not-a-timestamp"},
              {"kind": "rogue_dhcp", "severity": SEVERITY_ALERT, "ts": None}]
    out = compose_house_status(network_alerts=alerts, now=NOW)
    assert out == {"status": "ok", "score": 0, "reasons": [], "ts": NOW.isoformat()}


def test_unknown_network_kind_gets_a_generic_caption_not_a_crash():
    alerts = [{"kind": "future_kind", "severity": SEVERITY_WATCH, "ts": _ts(1)}]
    out = compose_house_status(network_alerts=alerts, now=NOW)
    assert out["reasons"][0]["what"] == "future kind"


def test_intrusion_reason_is_physical_layer_alert():
    class _Alert:
        def to_dict(self):
            return {"kind": "intrusion", "room": "sala", "person_count": 2,
                    "known_present": 1, "severity": SEVERITY_ALERT, "ts": _ts(1)}

    out = compose_house_status(intrusion_alerts=[_Alert()], now=NOW)
    assert out["status"] == "alert" and out["score"] == 4
    r = out["reasons"][0]
    assert r["layer"] == "physical" and r["kind"] == "intrusion"
    assert r["what"] == "unrecognized person in sala"


def test_intrusion_accepts_plain_dicts_too():
    out = compose_house_status(intrusion_alerts=[{"room": "cozinha", "severity": SEVERITY_ALERT,
                                                   "ts": _ts(1)}], now=NOW)
    assert out["reasons"][0]["what"] == "unrecognized person in cozinha"


def test_routine_anomaly_is_physical_layer_note_never_urgent():
    out = compose_house_status(routine_flags=[{"room": "quarto", "ts": _ts(1)}], now=NOW)
    assert out["status"] == "notice"                 # note-tier, below alert
    r = out["reasons"][0]
    assert r["layer"] == "physical" and r["kind"] == "routine_anomaly"
    assert r["severity"] == SEVERITY_NOTE
    assert r["what"] == "quarto occupancy is unusual for this hour"


def test_worst_reason_wins_a_pile_of_notes_never_outranks_one_alert():
    notes = [{"room": f"room{i}", "ts": _ts(i)} for i in range(5)]
    alerts = [{"kind": "gateway_identity", "severity": SEVERITY_ALERT, "ts": _ts(1),
              "gateway_ip": "192.168.1.1"}]
    out = compose_house_status(network_alerts=alerts, routine_flags=notes, now=NOW)
    assert out["status"] == "alert" and out["score"] == 4
    assert len(out["reasons"]) == 6


def test_critical_network_alert_still_ranks_alert_status_and_top_score():
    alerts = [{"kind": "gateway_identity", "severity": SEVERITY_CRITICAL, "ts": _ts(1)}]
    out = compose_house_status(network_alerts=alerts, now=NOW)
    assert out["status"] == "alert" and out["score"] == 5


def test_info_and_note_tier_alerts_only_ever_notice_not_alert():
    alerts = [{"kind": "rogue_device", "severity": SEVERITY_INFO, "ts": _ts(1)},
              {"kind": "rogue_device", "severity": SEVERITY_NOTE, "ts": _ts(2)}]
    out = compose_house_status(network_alerts=alerts, now=NOW)
    assert out["status"] == "notice" and out["score"] == 2


def test_reasons_are_chronologically_sorted():
    alerts = [{"kind": "rogue_device", "severity": SEVERITY_NOTE, "ts": _ts(1)}]
    routine = [{"room": "quarto", "ts": _ts(30)}]
    out = compose_house_status(network_alerts=alerts, routine_flags=routine, now=NOW)
    tss = [r["ts"] for r in out["reasons"]]
    assert tss == sorted(tss)


def test_evidence_trail_shape_every_reason_has_the_required_fields():
    alerts = [{"kind": "rogue_dhcp", "severity": SEVERITY_ALERT, "ts": _ts(1)}]
    intrusion = [{"room": "sala", "severity": SEVERITY_ALERT, "ts": _ts(1)}]
    routine = [{"room": "quarto", "ts": _ts(1)}]
    out = compose_house_status(network_alerts=alerts, intrusion_alerts=intrusion,
                               routine_flags=routine, now=NOW)
    for r in out["reasons"]:
        assert {"layer", "kind", "what", "severity", "ts"} <= set(r)
        assert r["layer"] in ("network", "physical")

    # never leaks raw personal data (geometry/identity/vitals) anywhere in the payload
    import json
    blob = json.dumps(out)
    for leak in ("targets", "identities", "vitals", "\"x\"", "\"y\""):
        assert leak not in blob
