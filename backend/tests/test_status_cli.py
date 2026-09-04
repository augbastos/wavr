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
import ssl

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
