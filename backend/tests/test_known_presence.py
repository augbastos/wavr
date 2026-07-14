from datetime import datetime, timedelta, timezone

from wavr.known_presence import compose_known_presence

NOW = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)


def _casa(*, network_presence: bool, confidence: float = 0.4) -> dict:
    """A minimal already-fused 'casa' RoomState dict, dict-shaped (mirrors
    RoomState.to_dict()) -- the composer must accept the plain dict form, not
    just a real RoomState object."""
    return {
        "room": "casa", "occupied": False, "confidence": confidence,
        "sources": [{"modality": "network", "presence": network_presence,
                     "confidence": 0.8, "age_s": 1, "health": "fresh", "count": None}],
        "identities": [], "person_count": None, "explanation": "", "ts": NOW.isoformat(),
    }


def _row(*, last_seen=None, first_seen=None, device_type=None) -> dict:
    return {"first_seen": first_seen, "last_seen": last_seen, "device_type": device_type}


# ---- house-scope shape --------------------------------------------------------

def test_no_registry_yields_empty_house_scope_summary():
    out = compose_known_presence(
        casa_state=None, net_registry={}, detailed_addrs=set(), meta_rows={}, now=NOW)
    assert out["scope"] == "house"
    assert out["modality"] == "network"
    assert out["likely_home"] is False
    assert out["confidence"] == 0.0
    assert out["confidence_label"] == "coarse"
    assert out["corroborators"] == []


def test_confidence_label_always_coarse_even_at_high_confidence():
    casa = _casa(network_presence=True, confidence=0.95)
    out = compose_known_presence(
        casa_state=casa, net_registry={}, detailed_addrs=set(), meta_rows={}, now=NOW)
    assert out["confidence_label"] == "coarse"
    assert out["confidence"] == 0.95   # copied verbatim, never re-derived/boosted


# ---- presence corroboration: consent #1 (row existence) only -----------------

def test_present_requires_both_network_source_and_fresh_last_seen():
    net_registry = {"aa:bb:cc:dd:ee:ff": "alice"}
    meta_rows = {"aa:bb:cc:dd:ee:ff": _row(last_seen=(NOW - timedelta(minutes=1)).isoformat())}
    casa = _casa(network_presence=True)
    out = compose_known_presence(
        casa_state=casa, net_registry=net_registry, detailed_addrs=set(),
        meta_rows=meta_rows, now=NOW)
    assert out["likely_home"] is True
    assert out["corroborators"] == [
        {"person": "alice", "mac_prefix": "aa:bb:cc", "present": True, "details": None}
    ]


def test_stale_last_seen_reads_absent_never_fabricated():
    net_registry = {"aa:bb:cc:dd:ee:ff": "alice"}
    meta_rows = {"aa:bb:cc:dd:ee:ff": _row(last_seen=(NOW - timedelta(hours=2)).isoformat())}
    casa = _casa(network_presence=True)
    out = compose_known_presence(
        casa_state=casa, net_registry=net_registry, detailed_addrs=set(),
        meta_rows=meta_rows, now=NOW)
    assert out["corroborators"][0]["present"] is False
    assert out["likely_home"] is False


def test_unseen_registered_device_reads_absent_not_fabricated():
    # Registered (consent #1) but device_meta has no row at all -- never seen.
    net_registry = {"aa:bb:cc:dd:ee:ff": "alice"}
    casa = _casa(network_presence=True)
    out = compose_known_presence(
        casa_state=casa, net_registry=net_registry, detailed_addrs=set(),
        meta_rows={}, now=NOW)
    assert out["corroborators"][0]["present"] is False
    assert out["likely_home"] is False


def test_network_source_absent_overrides_fresh_last_seen():
    # The house-level ARP evidence itself says absent this cycle -- a per-MAC
    # last_seen freshness alone must never override that.
    net_registry = {"aa:bb:cc:dd:ee:ff": "alice"}
    meta_rows = {"aa:bb:cc:dd:ee:ff": _row(last_seen=NOW.isoformat())}
    casa = _casa(network_presence=False)
    out = compose_known_presence(
        casa_state=casa, net_registry=net_registry, detailed_addrs=set(),
        meta_rows=meta_rows, now=NOW)
    assert out["corroborators"][0]["present"] is False
    assert out["likely_home"] is False


def test_likely_home_true_if_any_of_several_corroborators_present():
    net_registry = {"aa:bb:cc:dd:ee:ff": "alice", "11:22:33:44:55:66": "housemate"}
    meta_rows = {
        "aa:bb:cc:dd:ee:ff": _row(last_seen=(NOW - timedelta(hours=2)).isoformat()),  # stale
        "11:22:33:44:55:66": _row(last_seen=NOW.isoformat()),                          # fresh
    }
    casa = _casa(network_presence=True)
    out = compose_known_presence(
        casa_state=casa, net_registry=net_registry, detailed_addrs=set(),
        meta_rows=meta_rows, now=NOW)
    assert out["likely_home"] is True
    present_by_mac = {c["mac_prefix"]: c["present"] for c in out["corroborators"]}
    assert present_by_mac == {"aa:bb:cc": False, "11:22:33": True}


# ---- consent #2 gate: `details` -----------------------------------------------

def test_details_none_when_mac_not_in_allowlist():
    net_registry = {"aa:bb:cc:dd:ee:ff": "alice"}
    meta_rows = {"aa:bb:cc:dd:ee:ff": _row(last_seen=NOW.isoformat(), device_type="phone")}
    casa = _casa(network_presence=True)
    out = compose_known_presence(
        casa_state=casa, net_registry=net_registry, detailed_addrs=set(),  # not opted in
        meta_rows=meta_rows, now=NOW)
    assert out["corroborators"][0]["details"] is None
    assert out["corroborators"][0]["present"] is True   # presence unaffected by opt-out


def test_details_populated_when_mac_in_allowlist():
    net_registry = {"aa:bb:cc:dd:ee:ff": "alice"}
    last_seen = NOW - timedelta(seconds=30)
    meta_rows = {"aa:bb:cc:dd:ee:ff": _row(
        first_seen=(NOW - timedelta(days=1)).isoformat(),
        last_seen=last_seen.isoformat(), device_type="phone")}
    casa = _casa(network_presence=True)
    out = compose_known_presence(
        casa_state=casa, net_registry=net_registry, detailed_addrs={"aa:bb:cc:dd:ee:ff"},
        meta_rows=meta_rows, now=NOW)
    details = out["corroborators"][0]["details"]
    assert details["device_type"] == "phone"
    assert details["last_seen"] == last_seen.isoformat()
    assert details["quiet_for_seconds"] == 30.0


def test_details_present_independent_of_presence_flag():
    # A device opted into consent #2 but currently absent still gets its details
    # block (the metadata is honest regardless of "right now" presence) -- only
    # `present` reflects freshness, `details` reflects the opt-in alone.
    net_registry = {"aa:bb:cc:dd:ee:ff": "alice"}
    meta_rows = {"aa:bb:cc:dd:ee:ff": _row(last_seen=(NOW - timedelta(hours=3)).isoformat())}
    casa = _casa(network_presence=True)
    out = compose_known_presence(
        casa_state=casa, net_registry=net_registry, detailed_addrs={"aa:bb:cc:dd:ee:ff"},
        meta_rows=meta_rows, now=NOW)
    c = out["corroborators"][0]
    assert c["present"] is False
    assert c["details"] is not None


# ---- PII containment -----------------------------------------------------------

def test_mac_prefix_only_full_mac_never_echoed():
    net_registry = {"aa:bb:cc:dd:ee:ff": "alice"}
    casa = _casa(network_presence=True)
    out = compose_known_presence(
        casa_state=casa, net_registry=net_registry, detailed_addrs=set(),
        meta_rows={}, now=NOW)
    dumped = str(out)
    assert "aa:bb:cc:dd:ee:ff" not in dumped
    assert out["corroborators"][0]["mac_prefix"] == "aa:bb:cc"


# ---- casa_state shape flexibility ----------------------------------------------

def test_accepts_dict_shaped_casa_state():
    # Covered implicitly by every test above (they all pass a dict), but assert
    # explicitly that a missing/absent casa_state (None) never raises.
    out = compose_known_presence(
        casa_state=None, net_registry={"aa:bb:cc:dd:ee:ff": "x"}, detailed_addrs=set(),
        meta_rows={}, now=NOW)
    assert out["likely_home"] is False
    assert out["corroborators"][0]["present"] is False
