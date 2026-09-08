"""`wavr status`, treated as a real interface.

Two things are tested here and they are not the same:

  * **The rendering**, because a CLI is a human surface and
    `core=true db=1 src=8/9` is a machine talking to itself in front of a
    person.
  * **The TLS scope**, because the sibling tool claimed one it did not enforce.

The second is the one that matters. `doctor.py` disabled certificate
verification unconditionally under a comment saying the exemption was "scoped to
localhost only". Nothing scoped it — `--url` accepts anything — so pointing it
at a Core across the LAN accepted whatever certificate was offered. The claim
was in a comment; the enforcement was nowhere.
"""
import json
import ssl
import sys

import pytest

from wavr.status import (ATTENTION, OK, UNREACHABLE, context_for, is_loopback,
                         render)


# -- The scope that used to be a comment ---------------------------------------

@pytest.mark.parametrize("url", [
    "https://127.0.0.1:8000",
    "https://localhost:8000",
    "https://127.0.0.5:8000",
    "https://[::1]:8000",
])
def test_loopback_accepts_the_cores_own_self_signed_certificate(url):
    """It generated that certificate on this machine and the packet never
    reaches a network. Verifying it would fail every time and teach people to
    pass a flag that turns checking off permanently."""
    assert is_loopback(url)
    assert context_for(url).verify_mode == ssl.CERT_NONE


@pytest.mark.parametrize("url", [
    "https://192.168.1.57:8000",
    "https://core.example.com",
    "https://10.0.0.9",
])
def test_anything_off_loopback_still_verifies(url):
    """The whole point. A Core across the LAN is a Core somebody can be in the
    middle of, and silently accepting any certificate there is worse than
    refusing to connect — refusing is visible."""
    assert not is_loopback(url)
    ctx = context_for(url)
    assert ctx.verify_mode != ssl.CERT_NONE
    assert ctx.check_hostname is True


def test_the_doctor_tool_shares_this_rule_rather_than_keeping_its_own():
    """Two copies of a security decision is one copy that gets fixed and one
    that does not."""
    import inspect

    from wavr import doctor

    assert "CERT_NONE" not in inspect.getsource(doctor), (
        "doctor.py has its own certificate exemption again")
    assert "context_for" in inspect.getsource(doctor.fetch_doctor)


# -- The rendering -------------------------------------------------------------

HEALTHY = {"state": "healthy", "space": "My Home", "uptime_s": 93600,
           "last_state_age_s": 12,
           "findings": [{"key": "sensors", "state": "healthy",
                         "text": "3 sensors reporting."}]}


def test_a_person_can_read_it_at_a_glance():
    out = render(HEALTHY, {"total": 0, "headline": "Nothing needs your attention"})
    assert "Space" in out and "My Home" in out
    assert "Core" in out and "Healthy" in out
    # The number that distinguishes working from merely running.
    assert "Last reading" in out and "12s ago" in out
    # One fact per line, aligned — not a key=value soup.
    assert "=" not in out


def test_a_core_that_has_never_produced_a_reading_says_so_rather_than_zero():
    out = render({**HEALTHY, "last_state_age_s": None}, {"total": 0,
                                                        "headline": "x"})
    assert "none yet" in out
    assert "0s ago" not in out


def test_being_unable_to_check_is_not_reported_as_nothing_waiting():
    """A CLI that prints reassurance over a source it failed to read is the same
    lie as a green tray icon over a dead Core."""
    out = render(HEALTHY, None)
    assert "could not check" in out
    assert "Nothing needs" not in out


def test_a_degraded_core_explains_itself_under_the_answer():
    out = render(
        {"state": "degraded", "space": "My Home", "uptime_s": 60,
         "last_state_age_s": 1200,
         "findings": [{"key": "state", "state": "degraded",
                       "text": "The last room reading was 20 minutes ago."},
                      {"key": "sensors", "state": "degraded",
                       "text": "1 of 3 sensors are not reporting."}]},
        {"total": 1, "headline": "1 thing needs your attention"})
    assert "! Degraded" in out
    assert "20 minutes ago" in out
    assert "1 of 3" in out


def test_the_exit_codes_are_usable_without_parsing_anything():
    """A monitor should be able to call this and act on the result alone —
    parsing the human output freezes its wording forever."""
    assert (OK, ATTENTION, UNREACHABLE) == (0, 1, 2)


def _serve(answer, monkeypatch):
    """Answer `_get` from a table instead of a socket."""
    from wavr import status as mod
    monkeypatch.setattr(mod, "_get", lambda url, path, token: answer(path))
    return mod


def _healthy_runtime():
    return {"state": "healthy", "headline": "Wavr — running", "findings": []}


def test_an_unreadable_inbox_is_never_reported_as_nothing_waiting(monkeypatch):
    """`-q` prints nothing, so the exit code IS the answer.

    Returning 0 there said "nothing needs you" about a list nobody managed to
    read — the same claim the attention route itself refuses to make, which is
    why it reports `could_not_check` rather than an empty list. This was the
    consumer ignoring it.
    """
    def answer(path):
        if path == "/api/runtime":
            return _healthy_runtime()
        raise RuntimeError("the inbox is unreadable")

    mod = _serve(answer, monkeypatch)
    assert mod.main(["--url", "http://x", "-q"]) == ATTENTION


def test_a_partially_read_inbox_is_not_a_clean_bill_either(monkeypatch):
    """`could_not_check` names the sources that failed. Zero items out of a
    partial read is not zero items."""
    def answer(path):
        if path == "/api/runtime":
            return _healthy_runtime()
        return {"total": 0, "items": [], "could_not_check": ["pending_pairings"]}

    mod = _serve(answer, monkeypatch)
    assert mod.main(["--url", "http://x", "-q"]) == ATTENTION


def test_a_fully_read_empty_inbox_is_still_a_clean_zero(monkeypatch):
    """The guard above must not turn every healthy Core into an alert."""
    def answer(path):
        if path == "/api/runtime":
            return _healthy_runtime()
        return {"total": 0, "items": [], "could_not_check": []}

    mod = _serve(answer, monkeypatch)
    assert mod.main(["--url", "http://x", "-q"]) == OK


def test_it_prints_a_status_when_stdout_cannot_encode_its_marks(monkeypatch):
    """The documented cron use, on the platform the quickstart documents.

    The marks are "●", "○" and "×", and a redirected stdout on Windows gets a
    legacy code page that encodes none of them — so the command printed a
    UnicodeEncodeError traceback instead of a status the moment nobody was
    watching, which is every use where the exit code has to be trustworthy.
    """
    import io

    def answer(path):
        if path == "/api/runtime":
            return _healthy_runtime()
        return {"total": 0, "items": [], "could_not_check": []}

    mod = _serve(answer, monkeypatch)
    raw = io.BytesIO()
    narrow = io.TextIOWrapper(raw, encoding="cp1252", errors="strict",
                              newline="")
    monkeypatch.setattr(sys, "stdout", narrow)
    code = mod.main(["--url", "http://x"])          # must not raise
    narrow.flush()
    assert code == OK
    assert raw.getvalue().strip(), "it printed nothing at all"


# -- The interface `--help` calls stable ---------------------------------------

def _refused(path):
    raise OSError("connection refused")


def test_the_machine_readable_output_keeps_one_shape(monkeypatch, capsys):
    """`--json`'s help text says the human format is not a stable interface and
    this one is. It was not.

    A reachable Core printed `{"runtime": ..., "attention": ...}`; an
    unreachable one printed `{"state": ..., "error": ..., "url": ...}` — a
    different object, in the one case somebody runs a monitor for. A script
    reading `["runtime"]["state"]` raised KeyError exactly when the Core was
    down, and from the script's side that is indistinguishable from the script
    being broken.
    """
    def up(path):
        if path == "/api/runtime":
            return _healthy_runtime()
        return {"total": 0, "items": [], "could_not_check": []}

    mod = _serve(up, monkeypatch)
    assert mod.main(["--url", "http://x", "--json"]) == OK
    reachable = json.loads(capsys.readouterr().out)

    mod = _serve(_refused, monkeypatch)
    assert mod.main(["--url", "http://x", "--json"]) == UNREACHABLE
    silent = json.loads(capsys.readouterr().out)

    assert set(reachable) <= set(silent), (
        "the unreachable answer dropped keys the reachable one has: "
        f"{sorted(set(reachable) - set(silent))}")
    assert silent["runtime"]["state"] == "unavailable"
    assert silent["error"], "and it still says what actually went wrong"
    assert silent["url"] == "http://x", "and where it looked"


def test_the_one_answer_for_silence_is_the_one_this_prints(monkeypatch, capsys):
    """`runtime_status.unreachable()` exists so that a tray, a menu bar and a
    browser tab cannot disagree about what "I got no answer" means.

    Nothing in production called it. So the guarantee was held by nobody, and
    this module — whose own heading reads "it renders; it does not decide" —
    was quietly the second implementation, three lines into its failure branch.
    """
    from wavr.runtime_status import unreachable

    mod = _serve(_refused, monkeypatch)
    mod.main(["--url", "http://x", "--json"])
    printed = json.loads(capsys.readouterr().out)["runtime"]
    assert printed == unreachable().to_dict()
