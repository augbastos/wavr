"""The Android launcher's native bridge, checked against its own comments.

`MainActivity.WavrNativeBridge` is `window.WavrNative` inside the Core panel's
WebView. Two of its methods carry a rule in prose:

  * `requestBatteryExemption(direct = true)` fires Android's one-tap
    battery-optimisation dialog, and the comment above it says it "is only
    legitimate from a button the operator pressed after reading
    [getPowerRationale]".
  * `getPowerRationale()` is described as "the text the panel MUST show
    before calling [requestBatteryExemption]".

Nothing checked either. A panel could go straight to the system dialog, and the
rationale -- which explains that the exemption grants no new permission, no new
data and no internet access, and that saying no is allowed -- would never be
read. "MUST" was a word in a comment and nowhere else, which is the shape this
repository keeps finding: a stated guarantee with no enforcement.

## Why this is a Python test reading Kotlin

The launcher's only JVM tests parse resources off disk; there is no Robolectric,
so an Activity's inner class cannot be instantiated in a unit test. Reading the
source is what is available, and it is the same thing `test_vocabulary.py`
already does to the Rust tray for the same reason. A structural check is weaker
than a behavioural one and it is not nothing: it fails the moment the gate is
deleted, which is the regression it exists for.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MAIN = (REPO / "core-launcher" / "app" / "src" / "main" / "java" / "dev"
        / "wavr" / "core" / "MainActivity.kt")

pytestmark = pytest.mark.skipif(
    not MAIN.is_file(),
    reason="the Android launcher is not in this checkout")


def _source() -> str:
    return MAIN.read_text(encoding="utf-8")


def test_the_bridge_is_still_where_this_reads_it():
    """A guard on the guards below: a test that parses nothing cannot fail."""
    src = _source()
    assert "fun requestBatteryExemption(" in src, (
        "the bridge method was renamed or removed; the checks below are "
        "reading nothing")
    assert "fun getPowerRationale(" in src


def test_the_one_tap_battery_dialog_needs_the_rationale_first():
    """The gate, in the code rather than in the comment above it."""
    src = _source()
    body = src[src.index("fun requestBatteryExemption("):]
    body = body[:body.index("@JavascriptInterface", 1)]
    assert re.search(r"direct\s*&&\s*!rationaleHandedOut", body), (
        "requestBatteryExemption fires the one-tap system dialog without "
        "checking that the panel ever asked for the rationale, which is the "
        "one thing the comment above it forbids")


def test_asking_for_the_rationale_is_what_unlocks_it():
    """The other half. A latch nothing ever sets refuses everything, which
    looks like a working gate and is a broken feature."""
    src = _source()
    body = src[src.index("fun getPowerRationale("):]
    body = body[:body.index("@JavascriptInterface", 1)]
    assert "rationaleHandedOut = true" in body, (
        "getPowerRationale does not record that it handed the text out, so "
        "the gate below can never open and the one-tap request is dead rather "
        "than guarded")


def test_the_untrusted_origin_check_still_comes_first():
    """The gate must not have displaced the origin check. A WebView that has
    navigated somewhere else is not the panel, and it may not reach any of
    this at all -- least of all a system dialog."""
    src = _source()
    for name in ("requestBatteryExemption", "getPowerRationale"):
        body = src[src.index(f"fun {name}("):]
        body = body[:body.index("@JavascriptInterface", 1)]
        assert "onTrustedOrigin()" in body, (
            f"{name} no longer checks the origin")
        # And it is the FIRST thing, not a later branch.
        first = body.index("onTrustedOrigin()")
        latch = body.find("rationaleHandedOut")
        assert latch == -1 or first < latch, (
            f"{name} consults the rationale latch before checking the origin")
