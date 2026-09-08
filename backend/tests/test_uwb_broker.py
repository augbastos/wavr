"""Brokering a UWB ranging session, where every rule is a security rule.

Android's UWB API leaves the exchange of channel, addresses and session key to
the application, over a secure out-of-band channel. Every sample app invents its
own — a BLE characteristic, a QR code, a typed hex string — and all of them are
worse than what a household already has, because Wavr paired both devices and
holds an authenticated channel to each.

Which means these tests are about a key. Getting them wrong hands one to the
wrong device.
"""
import pytest

from wavr.uwb_broker import (
    MAX_SESSIONS, ROLE_CONTROLEE, ROLE_CONTROLLER, SESSION_KEY_BYTES,
    SESSION_TTL_S, UWB_CHANNELS, UwbBroker, UwbError,
)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def yes(_device):
    return True


def broker(clock=None):
    return UwbBroker(clock=clock or Clock())


# -- Who may be handed a key ---------------------------------------------------

def test_a_device_that_never_reported_uwb_is_refused_not_assumed():
    """The tristate rule, applied where guessing would mean handing a session key
    to something that cannot use it and may not have asked."""
    b = broker()
    with pytest.raises(UwbError, match="silent on it"):
        b.open("phone", ["tablet"], capable=lambda d: None if d == "tablet" else True)


def test_a_device_that_reported_no_uwb_is_refused_plainly():
    b = broker()
    with pytest.raises(UwbError, match="has not reported UWB"):
        b.open("phone", ["tablet"], capable=lambda d: d == "phone")


def test_both_ends_are_checked_not_only_the_controlee():
    b = broker()
    with pytest.raises(UwbError):
        b.open("phone", ["tablet"], capable=lambda d: d != "phone")


def test_a_device_cannot_range_with_itself():
    with pytest.raises(UwbError, match="range with itself"):
        broker().open("phone", ["phone"], capable=yes)


def test_a_session_needs_both_ends():
    b = broker()
    for bad in ((("", ["tablet"])), (("phone", []))):
        with pytest.raises(UwbError):
            b.open(bad[0], bad[1], capable=yes)


# -- The key ------------------------------------------------------------------

def test_the_key_is_never_in_the_ordinary_representation():
    """So no log line, diagnostics bundle or debug print can carry it by
    accident."""
    session = broker().open("phone", ["tablet"], capable=yes)
    body = session.to_dict()
    assert "session_key" not in body and "_key" not in body
    assert session._key.hex() not in str(body)


def test_the_key_is_random_and_the_right_length():
    b = broker()
    a = b.open("phone", ["tablet"], capable=yes)
    c = b.open("phone2", ["tablet2"], capable=yes)
    assert len(a._key) == SESSION_KEY_BYTES
    assert a._key != c._key


def test_each_device_collects_its_parameters_exactly_once():
    """A key that can be re-fetched leaks with any single replayed credential.
    The honest recovery from a lost key is a new session, not a second copy."""
    b = broker()
    session = b.open("phone", ["tablet"], capable=yes)
    first = b.claim(session.session_id, "phone")
    assert first["session_key"] == session._key.hex()
    with pytest.raises(UwbError, match="delivered once"):
        b.claim(session.session_id, "phone")


def test_the_other_end_can_still_collect_after_the_first_one_has():
    b = broker()
    session = b.open("phone", ["tablet"], capable=yes)
    b.claim(session.session_id, "phone")
    assert b.claim(session.session_id, "tablet")["session_key"]


def test_a_device_outside_the_session_is_refused():
    b = broker()
    session = b.open("phone", ["tablet"], capable=yes)
    with pytest.raises(UwbError, match="not part of this session"):
        b.claim(session.session_id, "somebody-elses-phone")


def test_each_end_is_told_its_role_and_its_peers():
    b = broker()
    session = b.open("phone", ["tablet", "watch"], capable=yes)
    assert b.claim(session.session_id, "phone")["role"] == ROLE_CONTROLLER
    controlee = b.claim(session.session_id, "tablet")
    assert controlee["role"] == ROLE_CONTROLEE
    assert controlee["peers"] == ["phone"], "a controlee has exactly one controller"


def test_the_controller_is_told_all_of_its_controlees():
    b = broker()
    session = b.open("phone", ["tablet", "watch"], capable=yes)
    assert set(b.claim(session.session_id, "phone")["peers"]) == {"tablet", "watch"}


# -- Lifetime ------------------------------------------------------------------

def test_a_session_expires():
    """Ranging is a foreground activity. A session that outlived the interaction
    would be a key sitting in memory for an afternoon."""
    clock = Clock()
    b = broker(clock)
    session = b.open("phone", ["tablet"], capable=yes)
    clock.advance(SESSION_TTL_S + 1)
    assert b.get(session.session_id) is None
    with pytest.raises(UwbError, match="expired"):
        b.claim(session.session_id, "phone")


def test_a_claim_reports_how_long_is_left():
    clock = Clock()
    b = broker(clock)
    session = b.open("phone", ["tablet"], capable=yes)
    clock.advance(60)
    assert b.claim(session.session_id, "phone")["expires_in_s"] == pytest.approx(
        SESSION_TTL_S - 60, abs=0.5)


def test_sessions_are_bounded_and_the_stalest_goes_first():
    clock = Clock()
    b = broker(clock)
    first = b.open("phone", ["tablet"], capable=yes)
    for i in range(MAX_SESSIONS + 3):
        clock.advance(1)
        b.open(f"c{i}", [f"p{i}"], capable=yes)
    assert b.get(first.session_id) is None
    assert len(b.list()) <= MAX_SESSIONS


def test_closing_a_session_removes_it():
    b = broker()
    session = b.open("phone", ["tablet"], capable=yes)
    assert b.close(session.session_id) is True
    assert b.get(session.session_id) is None


def test_wavr_picks_the_channel_so_two_sessions_do_not_collide():
    """Android lets the Controller choose. Wavr chooses for it, because two
    sessions in one house landing on the same channel by coincidence is a
    debugging afternoon nobody needs."""
    b = broker()
    a = b.open("p1", ["t1"], capable=yes)
    c = b.open("p2", ["t2"], capable=yes)
    assert a.channel in UWB_CHANNELS and c.channel in UWB_CHANNELS
    assert a.channel != c.channel


def test_sessions_do_not_survive_a_restart():
    """A brokered session is a live arrangement between two devices. Persisting
    one would mean a key survived a restart neither device knows about."""
    import inspect

    from wavr import uwb_broker
    assert "sqlite3" not in inspect.getsource(uwb_broker)


# -- Over HTTP: where the device id comes from ---------------------------------

def _app(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from wavr.app import create_app
    from wavr.camera_store import CameraStore
    from wavr.fusion import FusionEngine
    from wavr.hub import Hub
    from wavr.storage import Storage
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "w.db"))
    app = create_app(sources=[], storage=Storage(":memory:"), hub=Hub(),
                     fusion=FusionEngine(), camera_store=CameraStore(":memory:"),
                     health_resolvers={}, health_check=lambda: True)
    return app, TestClient(app, headers={"X-Wavr-Local": "1"})


def test_the_claiming_device_is_taken_from_the_token_never_the_request(
        monkeypatch, tmp_path, served_routes):
    """A device-id PARAMETER here would let any paired device ask for another's
    session key, which is the entire secret of a ranging session.

    Checked against the route's real signature rather than its source text: an
    earlier version grepped the source and fired on the word inside this very
    explanation, which is the same false positive a `.innerHTML` check hit once
    before."""
    import inspect

    app, _client = _app(monkeypatch, tmp_path)
    # `served_routes` walks into the included routers. Read straight off
    # `app.routes` this raised StopIteration -- a security check that could
    # not find the endpoint it guards.
    route = next(r for r in served_routes(app)
                 if r.path == "/api/uwb/sessions/{session_id}/claim")
    params = set(inspect.signature(route.endpoint).parameters)
    assert "device_id" not in params
    assert params <= {"session_id", "authorization", "_"}, params


def test_a_device_with_no_manifest_cannot_open_a_session(monkeypatch, tmp_path):
    """Nothing has reported UWB on a fresh install, so the tristate refusal is
    the default behaviour rather than an edge case."""
    app, client = _app(monkeypatch, tmp_path)
    with client as c:
        r = c.post("/api/uwb/sessions",
                   json={"controller": "a", "controlees": ["b"]})
        assert r.status_code == 400
        assert "UWB" in r.json()["detail"]


def test_loopback_root_cannot_claim_because_it_is_not_a_device(monkeypatch, tmp_path):
    app, client = _app(monkeypatch, tmp_path)
    with client as c:
        r = c.post("/api/uwb/sessions/uwb_nope/claim")
        assert r.status_code == 400
        assert "paired device" in r.json()["detail"]


def test_listing_sessions_never_carries_a_key(monkeypatch, tmp_path):
    """`to_dict` does not carry it at all, so this route cannot leak one by
    forgetting to strip it."""
    app, client = _app(monkeypatch, tmp_path)
    with client as c:
        body = c.get("/api/uwb/sessions").json()
        assert "session_key" not in str(body)
        assert "never listed" in body["note"]
