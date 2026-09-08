"""The pairing tile shows two codes; only one of them pairs anything.

Measured in a real browser against a real Core (WAVR_MULTIDEVICE=1): the
accent-bordered, large-digit box read as "the one to type", and it held
verify6 — `Hub verification code` / "On your phone, enter this 6-digit code
to pair." verify6 is `SHA-256(cert_fingerprint | pair_code)`, a compare-only
value for catching an interception (`wavr.tls.verification_code`); `/api/pair`
has never accepted it. A phone typing the highlighted box got a 403 every
time, and the box that actually worked — the 8-digit `#pairCodeBox` — sat
below it, undecorated, saying "enter this code on the device" in the same
small type as everything else on the tile.

This drives the real endpoint (`POST /api/pair`) with both codes exactly as
rendered, rather than asserting on styling alone: a CSS regression that swaps
classes back would still leave the WORDS honest, and a copy regression that
reintroduces "enter this... to pair" on verify6 would still leave the box
plain — either alone is a real defect, so both are checked, and the network
round-trip is the one assertion a purely-visual review cannot fake.
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api",
                    reason="playwright is not installed")
from playwright.sync_api import sync_playwright  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def core(tmp_path_factory):
    """A real Core with multi-device pairing turned on, launched the way it
    actually ships when a household flips "Let other devices connect": via
    `python -m wavr.serve`, not `uvicorn wavr.app:app` directly.

    That distinction is load-bearing here. `serve.py`'s own docstring is
    explicit that the Dockerfile/scripts/`test_browser_ui.py`'s own `core`
    fixture all launch `uvicorn wavr.app:app` directly, which is ALWAYS plain
    HTTP — TLS, and therefore `cert_fingerprint`/verify6/the QR box, only
    exist on the `wavr.serve` path. Starting this fixture the other way would
    silently test the plain-HTTP fallback (verify6 absent, `#pairNoQrNote`
    shown) instead of the two-codes-on-screen scenario this file exists to
    catch.
    """
    port = _free_port()
    tmp = tmp_path_factory.mktemp("pairing_ui")
    db = tmp / "wavr.db"
    env = {
        **os.environ,
        "WAVR_DB": str(db),
        "WAVR_HOUSE_MAP": str(tmp / "house.json"),
        "WAVR_MULTIDEVICE": "1",
        "WAVR_BIND": "127.0.0.1",
        "WAVR_PORT": str(port),
        "WAVR_TLS_DIR": str(tmp / "tls"),   # never the real ~/.wavr/ cert
        "WAVR_LOCAL_TOKEN": "",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "wavr.serve"],
        cwd=str(BACKEND), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"https://127.0.0.1:{port}"
    insecure = ssl._create_unverified_context()   # self-signed local cert, same as a phone's "Proceed"
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("the Core exited while starting")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.25)
        else:
            raise RuntimeError("the Core did not start in time")

        import urllib.request
        req = urllib.request.Request(
            base + "/api/setup/create-space",
            data=json.dumps({"name": "Pairing UI Test", "kind": "home",
                             "owner_name": "Tester", "room": "sala"}).encode(),
            headers={"Content-Type": "application/json", "X-Wavr-Local": "1"},
            method="POST")
        with urllib.request.urlopen(req, timeout=20, context=insecure):
            pass
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:      # noqa: BLE001
            pytest.skip(f"no chromium available: {exc}")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    # `ignore_https_errors`: the same self-signed-cert leap of faith a phone
    # takes tapping Advanced -> Proceed on the real warning — this file tests
    # what is BEHIND that warning, not the warning itself.
    ctx = browser.new_context(viewport={"width": 1400, "height": 1100},
                              ignore_https_errors=True)
    pg = ctx.new_page()
    yield pg
    ctx.close()


def _open_pairing(page, core):
    """Settings > Devices, with the pairing tile actually populated.

    Waited on the CODE's own text rather than a fixed pause: `mintOnce()` is
    one `await fetch(...)` away from the panel becoming visible, and a pause
    long enough to be safe on a loaded CI box is a pause that makes a real
    regression here look like a slow test everywhere else.
    """
    page.goto(f"{core}/#gearSecDevices")
    page.wait_for_selector("#gearOverlay:not([hidden])", timeout=30000)
    page.wait_for_selector("#pairing:not([hidden])", timeout=30000)
    page.wait_for_function(
        "() => (document.getElementById('pairCode')?.textContent || '').length >= 8",
        timeout=30000)
    page.wait_for_function(
        "() => (document.getElementById('pairVerify6')?.textContent || '')"
        ".replace(/\\D/g, '').length === 6",
        timeout=30000)


def test_the_8_digit_code_is_the_one_styled_as_primary(page, core):
    """The box `/api/pair` actually accepts gets the accent-border hero
    treatment; the compare-only verify6 box does not."""
    _open_pairing(page, core)
    code_classes = page.eval_on_selector("#pairCodeBox", "el => el.className")
    verify_classes = page.eval_on_selector("#pairVerifyBox", "el => el.className")
    assert "pair-code-box-hero" in code_classes.split(), (
        f"the 8-digit box lost its primary styling: {code_classes!r}")
    assert "pair-code-box-hero" not in verify_classes.split(), (
        f"the compare-only verify6 box is styled as primary again: {verify_classes!r}")


def test_the_verify6_box_says_check_not_enter(page, core):
    """The copy next to verify6 must never tell a person to type it — that is
    the exact sentence that sent a phone's 6-digit attempt into a 403."""
    _open_pairing(page, core)
    hint = page.locator("#pairVerifyBox .pair-hint").inner_text().lower()
    assert "enter" not in hint and "type it in" not in hint.replace(
        "do not type it in", ""), (
        f"verify6's hint still tells a person to enter/type it: {hint!r}")
    assert "do not type" in hint, f"verify6's hint no longer warns against typing it: {hint!r}"


def test_only_the_8_digit_code_actually_pairs(page, core):
    """The end-to-end proof: submit both codes to the real `/api/pair` exactly
    as a phone would, using exactly what the tile renders for each."""
    _open_pairing(page, core)
    code = page.eval_on_selector("#pairCode", "el => el.textContent.trim()")
    verify6 = page.eval_on_selector(
        "#pairVerify6", "el => el.textContent.replace(/\\D/g, '')")
    assert len(code) == 8, f"the pairing code is not 8 digits: {code!r}"
    assert len(verify6) == 6, f"verify6 is not 6 digits: {verify6!r}"

    verify_status = page.evaluate(
        """async (v6) => {
             const r = await fetch('/api/pair', {method: 'POST',
               headers: {'Content-Type': 'application/json'},
               body: JSON.stringify({code: v6, device_name: 'test phone (verify6)'})});
             return r.status;
           }""", verify6)
    assert verify_status == 403, (
        f"verify6 (the box the hint used to say 'enter this to pair') was "
        f"accepted by /api/pair with status {verify_status} — it must stay "
        f"compare-only")

    pair_status = page.evaluate(
        """async (c) => {
             const r = await fetch('/api/pair', {method: 'POST',
               headers: {'Content-Type': 'application/json'},
               body: JSON.stringify({code: c, device_name: 'test phone (8-digit)'})});
             return r.status;
           }""", code)
    assert pair_status == 200, (
        f"the 8-digit code rendered on screen was refused by /api/pair "
        f"(status {pair_status}) — the ONE box a person should type from "
        f"does not work")
