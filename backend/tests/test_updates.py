"""How a Wavr Core updates, and the several things it refuses to do about it.

An auto-updater is the commonest way a local-first product quietly breaks its own
promise: it phones home on a timer, carries an install identifier so the check can
be counted, and becomes the one component that has to reach outward for the rest
to keep working. Most of this file is about that not happening.
"""
import pytest

from wavr.updates import (
    CHANNEL_ANDROID, CHANNEL_DOCKER, CHANNEL_SCRIPT, CHANNEL_SOURCE,
    CHANNEL_UNKNOWN, CHANNEL_WINDOWS, CONNECTOR_ID, INSTRUCTIONS, RELEASES_URL,
    detect_channel, read_release, status,
)


# -- What this module will not do ---------------------------------------------

def test_nothing_reaches_the_network_from_this_module():
    """The fetch lives with the caller. That is what keeps an audit of "what can
    reach outward" from having to read this file."""
    import inspect

    from wavr import updates
    src = inspect.getsource(updates)
    for forbidden in ("urlopen", "requests.", "httpx", "socket.", "urlretrieve"):
        assert forbidden not in src, f"{forbidden} in updates.py"


def test_wavr_never_applies_an_update_to_itself():
    """A Core that changed overnight would change what the sensors do while
    nobody was watching, on a machine somebody chose because it does not do
    things behind their back."""
    body = status(running="1.0.0", check_enabled=False).to_dict()
    assert "never updates itself" in body["note"]


def test_no_check_is_made_by_default():
    body = status(running="1.0.0", check_enabled=False).to_dict()
    assert body["check_enabled"] is False and body["checked"] is False
    assert "off" in body["why_no_check"]


def test_the_check_is_a_connector_not_a_second_egress_path():
    """There is one screen in this product where anything outward is switched
    on, and this belongs on it rather than beside it."""
    assert CONNECTOR_ID == "update_check"
    body = status(running="1.0.0", check_enabled=False).to_dict()
    assert "Connectors" in body["why_no_check"]


def test_the_check_carries_nothing_identifying():
    """No install id, no version, no platform, no count. The request looks like
    anybody else's because it is."""
    assert "?" not in RELEASES_URL, "no query string, so nothing can ride in one"
    body = status(running="1.0.0", check_enabled=False).to_dict()
    assert "carries no identifier" in body["why_no_check"]


# -- The tristate that stops a false reassurance -------------------------------

def test_no_check_made_is_not_the_same_as_up_to_date():
    """A boolean here would make "nobody has looked" indistinguishable from
    "you are current", and the second is a claim with no basis behind it."""
    body = status(running="1.0.0", check_enabled=True, checked=False).to_dict()
    assert body["up_to_date"] is None


def test_a_completed_check_answers_the_question():
    behind = status(running="1.0.0", check_enabled=True, checked=True,
                    latest="1.2.0")
    assert behind.behind is True and behind.to_dict()["up_to_date"] is False
    current = status(running="1.2.0", check_enabled=True, checked=True,
                     latest="1.2.0")
    assert current.behind is False and current.to_dict()["up_to_date"] is True


def test_an_uncomparable_version_never_claims_an_update_exists():
    """Claiming one on the strength of a string comparison would send somebody
    to reinstall for nothing."""
    out = status(running="nightly", check_enabled=True, checked=True,
                 latest="1.2.0")
    assert out.behind is False


def test_a_failed_check_is_reported_rather_than_silently_up_to_date():
    body = status(running="1.0.0", check_enabled=True, checked=False,
                  error="could not reach github.com").to_dict()
    assert body["up_to_date"] is None
    assert "could not reach" in body["error"]


# -- The half that is actually useful ------------------------------------------

def test_every_channel_has_a_whole_instruction():
    """The question an operator has is "what do I type", and half an answer is
    why people end up on a forum."""
    for channel, text in INSTRUCTIONS.items():
        assert len(text) > 40, channel


def test_the_instruction_says_what_happens_to_the_space():
    """The unasked question behind every update: do I lose my floor plan?"""
    for channel in (CHANNEL_DOCKER, CHANNEL_ANDROID, CHANNEL_WINDOWS,
                    CHANNEL_SCRIPT):
        text = INSTRUCTIONS[channel].lower()
        assert "space" in text or "survives" in text or "leaves your" in text


def test_an_undetected_channel_refuses_to_guess():
    """A wrong instruction sends somebody to run a command that does nothing and
    leaves them believing they have updated."""
    assert "will not guess" in INSTRUCTIONS[CHANNEL_UNKNOWN]
    body = status(running="1.0.0", check_enabled=False,
                  channel=CHANNEL_UNKNOWN).to_dict()
    assert body["how_to_update"] == INSTRUCTIONS[CHANNEL_UNKNOWN]


# -- Detection, which is passed its signals so it can be tested at all ---------

def test_android_is_detected_from_its_own_environment():
    assert detect_channel(environ={"WAVR_ANDROID": "1"}) == CHANNEL_ANDROID
    assert detect_channel(environ={"ANDROID_ROOT": "/system"}) == CHANNEL_ANDROID


def test_docker_is_detected_from_the_image_variable():
    assert detect_channel(environ={"WAVR_DOCKER": "1"}) == CHANNEL_DOCKER


def test_docker_wins_over_a_checkout():
    """A developer running the image from a checkout is on the Docker channel
    for update purposes — `git pull` would not change the running container."""
    assert detect_channel(environ={"WAVR_DOCKER": "1"},
                          repo_marker=True) == CHANNEL_DOCKER


def test_a_frozen_build_is_the_installer_channel():
    assert detect_channel(environ={}, frozen=True, repo_marker=False) in (
        CHANNEL_WINDOWS, CHANNEL_SCRIPT)


def test_a_checkout_is_the_source_channel():
    assert detect_channel(environ={}, frozen=False,
                          repo_marker=True) == CHANNEL_SOURCE


def test_an_operator_may_state_the_channel_when_detection_cannot():
    out = detect_channel(environ={"WAVR_INSTALL_CHANNEL": CHANNEL_SCRIPT},
                         frozen=False, repo_marker=False)
    assert out == CHANNEL_SCRIPT


def test_an_operator_cannot_state_a_channel_that_does_not_exist():
    out = detect_channel(environ={"WAVR_INSTALL_CHANNEL": "carrier-pigeon"},
                         frozen=False, repo_marker=False)
    assert out == CHANNEL_UNKNOWN


# -- Reading somebody else's response ------------------------------------------

def test_a_release_payload_is_untrusted_input():
    """A body that changed shape would otherwise become a traceback on
    somebody's dashboard."""
    assert read_release({"tag_name": "v1.4.0"}) == "v1.4.0"
    for bad in (None, "a string", [], {"tag_name": {"nested": 1}},
                {"tag_name": "not-a-version"}, {}):
        assert read_release(bad) == ""


def test_a_tag_is_bounded():
    """It ends up rendered next to "a new version is available"."""
    assert len(read_release({"tag_name": "1." + "9" * 500})) <= 64


# -- Over HTTP -----------------------------------------------------------------

class FakeConnectors:
    def __init__(self, enabled=False, egress=True):
        self._enabled = enabled
        self._egress = egress

    def is_enabled(self, _id):
        return self._enabled

    def egress_allowed(self):
        return self._egress


def _router(enabled=False, egress=True, fetch=None):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from wavr.api_updates import build_router
    app = FastAPI()
    app.include_router(build_router(
        running_version="1.0.0", connectors=FakeConnectors(enabled, egress),
        require_local=lambda: None, require_scope=lambda _s: (lambda: None),
        fetch=fetch or (lambda: {"tag_name": "v1.4.0"})))
    return TestClient(app)


def test_reading_the_status_makes_no_request():
    """On a Core that never enabled the check this route is entirely local,
    which is the normal case and should stay the cheap one."""
    called = []
    c = _router(fetch=lambda: called.append(1) or {})
    body = c.get("/api/updates").json()
    assert called == []
    assert body["check_enabled"] is False and body["up_to_date"] is None


def test_a_check_with_the_connector_off_is_refused_and_reaches_nothing():
    called = []
    c = _router(enabled=False, fetch=lambda: called.append(1) or {})
    r = c.post("/api/updates/check")
    assert r.status_code == 409
    assert called == [], "fail-closed: zero network attempted"


def test_the_egress_master_switch_also_blocks_it():
    """The same gate every other outward-reaching feature goes through. A second
    path beside it would be a second place to audit and a second place to
    forget."""
    called = []
    c = _router(enabled=True, egress=False, fetch=lambda: called.append(1) or {})
    assert c.post("/api/updates/check").status_code == 409
    assert called == []


def test_an_enabled_check_reports_the_newer_release():
    body = _router(enabled=True).post("/api/updates/check").json()
    assert body["latest"] == "v1.4.0"
    assert body["up_to_date"] is False


def test_a_failed_check_is_reported_not_raised():
    """A failed update check is not a broken Core, and a 500 would make it look
    like one."""
    def boom():
        raise OSError("dns is down")
    r = _router(enabled=True, fetch=boom).post("/api/updates/check")
    assert r.status_code == 200
    assert "could not reach" in r.json()["error"]
    assert r.json()["up_to_date"] is None


def test_a_garbage_release_body_does_not_claim_an_update():
    r = _router(enabled=True, fetch=lambda: {"unexpected": "shape"})
    body = r.post("/api/updates/check").json()
    assert "latest" not in body and body["up_to_date"] is None


def test_there_is_no_timer_anywhere_in_the_update_path():
    """A check happens because somebody pressed something. That is also why it
    needs no schedule, no backoff and no jitter.

    Checked against the module's IMPORTS and call graph rather than its text: an
    earlier version grepped the source and fired on the word "schedule" inside
    this very explanation, which is the third time a source-text check has
    caught its own comment."""
    import ast
    import inspect

    from wavr import api_updates
    tree = ast.parse(inspect.getsource(api_updates))
    imported = {n.module for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom) and n.module}
    imported |= {a.name for n in ast.walk(tree)
                 if isinstance(n, ast.Import) for a in n.names}
    assert "asyncio" not in imported, "a timer would need it"
    assert "threading" not in imported
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.While)], (
        "no loop, so nothing can poll on its own")
