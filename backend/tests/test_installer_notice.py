"""The page somebody reads before agreeing to install must be true and current.

`desktop/installer-license.txt` is generated from two sources and COMMITTED,
because the installer build reads it — a clean clone that had to run a script
first would fail with a missing-file error that says nothing about what to do.

Committed generated files go stale. This is the thing that notices.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from build_installer_license import build, OUT   # noqa: E402


def test_the_committed_page_matches_its_sources():
    assert OUT.is_file(), (
        f"{OUT} is missing and the installer build reads it — "
        f"run scripts/build_installer_license.py")
    on_disk = OUT.read_text(encoding="utf-8")
    assert on_disk == build(), (
        "the installer page has drifted from INSTALL-NOTICE.txt or LICENSE — "
        "run scripts/build_installer_license.py")


def test_the_notice_states_every_thing_the_install_changes():
    """§33: an installer must not silently configure significant OS behaviour.

    Each phrase below corresponds to something installing Wavr actually does.
    Removing one from the notice while the behaviour remains is the failure —
    the page is the only place a person is told before they agree.
    """
    text = (ROOT / "desktop" / "INSTALL-NOTICE.txt").read_text(encoding="utf-8").lower()
    for claim, why in [
        ("tray", "closing the window does not stop it, and the tray is how you know"),
        ("quit wavr", "the one action that actually stops sensing"),
        ("start wavr when this machine starts", "launch-on-login, and that it is off"),
        ("127.0.0.1", "it listens on this machine only by default"),
        ("firewall", "windows may ask, and saying no still works"),
        ("no telemetry", "nothing is sent anywhere"),
        (".wavr", "where the database and floor plan live"),
        ("never written", "camera frames are not stored"),
        ("uninstall", "what removal does and does not delete"),
        # The distinctive word, not the abbreviation: the notice names the
        # licence in full because that is what a person can look up.
        ("affero", "the licence it is offered under"),
    ]:
        assert claim in text, f"the notice no longer says: {why}"


def test_the_notice_does_not_promise_what_the_product_does_not_do():
    """The notice claims no telemetry and no account. If either ever becomes
    untrue, this file is the last place anybody would think to look."""
    text = (ROOT / "desktop" / "INSTALL-NOTICE.txt").read_text(encoding="utf-8")
    assert "No account." in text
    assert "nothing leaves this machine unless you switch on a specific connector" in text
    # And the claim is checkable against the code that would break it: the
    # update check is the one outward thing that ships, and it is off.
    from wavr.updates import status
    assert status(running="0.0.0", check_enabled=False).to_dict()["check_enabled"] is False
