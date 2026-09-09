"""What "other devices can reach me" is allowed to mean.

`lan_reachability` is the surface that answers the one question the whole
multi-device half of the product depends on. Two defects in it were found by
running it against the first user's real machine and reading the answer next to
the firewall.

## 1. It named the wrong rule

`netsh` prints its dashed separator UNDER a rule's name, not between rules:

    Rule Name:   wavr-core
    -------------------------
    Enabled:     Yes
    Action:      Allow

    Rule Name:   Microsoft Edge (mDNS-In)
    -------------------------

Splitting on the dashes closed each block AFTER reading the NEXT rule's name,
so every block carried one rule's fields under the following rule's name. The
verdict was right and the name was shifted by one, and the Core duly reported
that it was allowed through the firewall by a rule called
"Microsoft Edge (mDNS-In)". A report that names a rule the reader cannot find
is not checkable, and being checkable is the only thing this report is for.

## 2. It did not check the profile, and said so

The reason string ended "(scope not verified)", which was honest and was the
whole defect: a rule scoped to Public counts as coverage on a machine Windows
has since decided is Private. The first user's machine carried four enabled
ALLOW rules for the Core and every one of them was Public-only. His Wi-Fi
happened to be Public that afternoon. Changing routers, a VPN, or answering
"make this PC discoverable" once is enough to move it, and the Core would have
gone on reporting that other devices could reach it while nothing could.

Both are checked here against synthetic `netsh` output, so nothing depends on
the firewall of whatever machine runs the suite.
"""
from __future__ import annotations

import pytest

from wavr import lan_reachability as lr

# An invented account name. The Windows path SHAPE is the fixture -- this
# test parses real `netsh advfirewall` output, which quotes the executable
# by full path -- so the shape has to stay and the identity has to be fake.
EXE = r"C:\Users\someone\AppData\Local\Wavr Desktop\wavr-core.exe"   # publication-gate: synthetic

DUAS_REGRAS = f"""
Rule Name:                            wavr-core.exe
----------------------------------------------------------------------
Description:                          wavr-core.exe
Enabled:                              Yes
Direction:                            In
Profiles:                             Public
Protocol:                             TCP
LocalPort:                            Any
Program:                              {EXE}
Action:                               Allow

Rule Name:                            Microsoft Edge (mDNS-In)
----------------------------------------------------------------------
Description:                          Microsoft Edge (mDNS-In)
Enabled:                              Yes
Direction:                            In
Profiles:                             Public
Protocol:                             UDP
LocalPort:                            5353
Program:                              C:\\Program Files\\Edge\\msedge.exe
Action:                               Allow
"""


@pytest.fixture(autouse=True)
def _windows_com_regras(monkeypatch):
    """Pretend to be Windows with a known firewall. Cache cleared each time."""
    monkeypatch.setattr(lr, "_is_windows", lambda: True)
    monkeypatch.setattr(lr, "_netsh_rules", lambda: DUAS_REGRAS)
    lr.reset_cache()
    yield
    lr.reset_cache()


def _no_perfil(monkeypatch, *perfis: str) -> None:
    monkeypatch.setattr(lr, "_active_profiles", lambda: frozenset(perfis))


# -- 1. the name ------------------------------------------------------------

def test_the_rule_it_names_is_the_rule_that_allows_it(monkeypatch):
    _no_perfil(monkeypatch, "public")
    r = lr.check(EXE, 8000, force=True)
    assert r.state == lr.ALLOWED, r
    assert list(r.rules) == ["wavr-core.exe"], (
        "the answer names the wrong rule; the parser is attributing each "
        f"rule's fields to the next rule's name: {r.rules}")


def test_a_rule_that_has_nothing_to_do_with_us_is_never_named(monkeypatch):
    _no_perfil(monkeypatch, "public")
    r = lr.check(EXE, 8000, force=True)
    assert "Microsoft Edge (mDNS-In)" not in r.rules, (
        "a rule for another program is being reported as covering the Core")


def test_the_blocks_carry_their_own_fields(monkeypatch):
    """The parse, directly. A block must hold one rule and all of it."""
    blocos = [b for b in lr._blocks(DUAS_REGRAS) if b]
    assert len(blocos) == 2, f"expected two rules, parsed {len(blocos)}"
    primeiro = " ".join(blocos[0].values()).lower()
    assert "wavr-core.exe" in primeiro and "msedge" not in primeiro, (
        "the first block mixes two rules together")
    segundo = " ".join(blocos[1].values()).lower()
    assert "msedge" in segundo and "mdns-in" in segundo, (
        "the second block lost its own name or program")


# -- 2. the profile ---------------------------------------------------------

def test_a_public_only_rule_does_not_cover_a_private_network(monkeypatch):
    """The state this exists to stop being invisible.

    A rule exists, is enabled, allows this Core — and applies to a network this
    machine is not on. Nothing gets through, and every firewall page a person
    might open looks fine.
    """
    _no_perfil(monkeypatch, "private")
    r = lr.check(EXE, 8000, force=True)
    assert r.state != lr.ALLOWED, (
        "a Public-only rule was counted as coverage on a Private network")
    assert "different network profile" in r.reason, r.reason
    assert "wavr-core.exe" in r.rules, (
        "the rule that would work on another network should still be named — "
        "it is the thing the person has to go and widen")


def test_the_answer_says_which_profile_it_checked(monkeypatch):
    _no_perfil(monkeypatch, "public")
    r = lr.check(EXE, 8000, force=True)
    assert "public" in r.reason.lower(), (
        "the answer no longer says which profile it was judged against, so a "
        "reader cannot tell whether it applies to the network they are on: "
        + r.reason)
    assert "scope not verified" not in r.reason, (
        "the reason still admits it did not check the scope")


def test_an_unreadable_profile_discards_nothing(monkeypatch):
    """Not knowing which network you are on is a reason to stay quiet, never a
    reason to throw away the evidence you do have."""
    _no_perfil(monkeypatch)          # empty: netsh could not be read
    r = lr.check(EXE, 8000, force=True)
    assert r.state == lr.ALLOWED, (
        "an unreadable active profile made the check discard a rule that may "
        "well apply")


def test_a_rule_for_every_profile_still_counts(monkeypatch):
    qualquer = DUAS_REGRAS.replace("Profiles:                             Public",
                                   "Profiles:                             Any", 1)
    monkeypatch.setattr(lr, "_netsh_rules", lambda: qualquer)
    _no_perfil(monkeypatch, "domain")
    lr.reset_cache()
    r = lr.check(EXE, 8000, force=True)
    assert r.state == lr.ALLOWED, (
        "a rule scoped to Any stopped counting, which would report every "
        "correctly-configured machine as unreachable")
