"""A paired phone must see the same health the Core's own screen sees.

## The defect

`WavrAPI`'s docstring lists "the companion bearer token, for the modes that
carry one" under "What this owns". It did not send one. Every caller that
predates the module carries the header by hand, so nothing regressed while
those were the only users — and then the runtime chip and the attention badge
started polling in companion mode, trusted the docstring, and went out with no
credential.

The Core refused them, and the chip settled on "Wavr is not answering" over a
Core that was answering perfectly. Permanently, on every paired phone. It is
silent by construction: a refused request is exactly what "not answering" is
supposed to look like, so the wrong answer and the right one are the same
pixels.

## Why this is a browser test and not a unit test

The question is not whether a header can be composed. It is what a person
holding a phone sees, and that depends on `MODE` resolving to `companion`,
which depends on the hostname the page was served from. So: bind the Core to
every interface, reach it by an RFC1918 address, and read the chip.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]

pytest.importorskip("playwright.sync_api",
                    reason="browser cases run in the browser CI job")
from playwright.sync_api import sync_playwright  # noqa: E402


def _lan_ip() -> str | None:
    """This machine's own RFC1918 address, which is what makes the page a
    companion: `isLoopbackHost` accepts only localhost and 127.0.0.1, and
    anything that is not a private literal is the demo."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        addr = s.getsockname()[0]
        s.close()
    except OSError:
        return None
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return None
    return addr if ip.is_private and not ip.is_loopback else None


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def lan_core(tmp_path_factory):
    """A Core bound to every interface, so a LAN address can reach it."""
    lan = _lan_ip()
    if not lan:
        pytest.skip("no RFC1918 address on this machine")
    port = _free_port()
    db = tmp_path_factory.mktemp("companion") / "wavr.db"
    env = {
        **os.environ,
        "WAVR_DB": str(db),
        "WAVR_HOUSE_MAP": str(db.parent / "house.json"),
        "WAVR_MULTIDEVICE": "1",
        "WAVR_LOCAL_TOKEN": "",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "wavr.app:app",
         "--host", "0.0.0.0", "--port", str(port), "--log-level", "warning"],
        cwd=str(BACKEND), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    loopback = f"http://127.0.0.1:{port}"
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

        req = urllib.request.Request(
            loopback + "/api/setup/create-space",
            data=json.dumps({"name": "Companion Test", "kind": "home",
                             "owner_name": "Tester", "room": "sala"}).encode(),
            headers={"Content-Type": "application/json", "X-Wavr-Local": "1"},
            method="POST")
        with urllib.request.urlopen(req, timeout=20):
            pass

        # A credential a phone would really hold, obtained the way a phone
        # really obtains one: the operator mints a CODE on the Core's own
        # screen, and the phone redeems it for a token.
        #
        # The first version of this fixture stored the code itself. A code is
        # not a bearer, so the Core refused every request, `companionAuthFailed`
        # cleared it — correctly — and the next poll went out with no
        # credential at all. The test then reported the product's own
        # self-defence as the defect it was written to catch.
        def _post(path, body):
            req = urllib.request.Request(
                loopback + path, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "X-Wavr-Local": "1"},
                method="POST")
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read() or b"{}")

        try:
            code = _post("/api/pair-code", {"role": "user"}).get("code")
            token = _post("/api/pair", {"code": code,
                                        "device_name": "test phone"}).get("token")
        except Exception as exc:                           # noqa: BLE001
            pytest.skip(f"this Core does not mint a companion credential: {exc}")
        if not token:
            pytest.skip("pairing returned no token")
        yield loopback, f"http://{lan}:{port}", token
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        yield b
        b.close()


def test_a_companion_carries_its_credential_on_every_request(browser, lan_core):
    """The header, observed on the wire, on the polls that had none."""
    _loopback, lan_url, token = lan_core
    # A fresh CONTEXT, not just a page: the shell registers a service worker
    # that precaches every module, and a second page in the same context is
    # served the modules that worker already holds. A cachebust on the document
    # URL does not touch them — which is the browser's own version of the warm
    # Gradle cache that hid a failing test in this project once already.
    ctx = browser.new_context(service_workers="block")
    page = ctx.new_page()
    seen: dict[str, dict] = {}

    def record(r):
        if "/api/" in r.url:
            # LAST wins, not first. `setdefault` kept the requests from the
            # load BEFORE the token was stored, so the test reported "no
            # credential" whatever the product did — a fixture measuring its
            # own setup, which is the failure mode this whole file is about.
            seen[r.url.split("?")[0].rsplit("/", 2)[-1]] = dict(r.headers)

    page.on("request", record)
    try:
        page.goto(lan_url + f"/?cachebust={time.time()}", wait_until="domcontentloaded")
        page.wait_for_function("() => typeof MODE !== 'undefined'", timeout=30000)
        mode = page.evaluate("() => MODE")
        assert mode == "companion", (
            f"this address did not make the page a companion ({mode!r}), so "
            f"the test is measuring the wrong mode")
        # Hand it the credential the operator would have paired it with, then
        # let the pollers run.
        page.evaluate("t => localStorage.setItem('wavr.token.' + location.origin, t)",
                      token)
        page.reload()
        page.wait_for_function("() => typeof MODE !== 'undefined'", timeout=30000)
        # No `seen.clear()` here: the reload's own first poll fires within
        # milliseconds of MODE existing, and clearing after it discarded the
        # only requests this test is about. `record` keeps the LAST headers per
        # route, so the pre-token load cannot be what is measured.
        page.wait_for_timeout(6000)

        polled = {k: v for k, v in seen.items() if k in ("runtime", "attention")}
        assert polled, (
            "neither the runtime chip nor the attention badge polled at all, "
            f"so there is nothing to check. Requests seen: {sorted(seen)}")
        naked = [k for k, h in polled.items()
                 if not h.get("authorization", "").startswith("Bearer ")]
        assert not naked, (
            f"{naked} went out with no credential, so the Core refuses them "
            f"and the chip reports a healthy Core as not answering")
    finally:
        page.close()
        ctx.close()


def test_a_companion_is_not_told_its_live_core_is_dead(browser, lan_core):
    """What the person actually reads. The header is the mechanism; this is
    the consequence, and it is the reason the mechanism matters."""
    _loopback, lan_url, token = lan_core
    # A fresh CONTEXT, not just a page: the shell registers a service worker
    # that precaches every module, and a second page in the same context is
    # served the modules that worker already holds. A cachebust on the document
    # URL does not touch them — which is the browser's own version of the warm
    # Gradle cache that hid a failing test in this project once already.
    ctx = browser.new_context(service_workers="block")
    page = ctx.new_page()
    try:
        page.goto(lan_url + f"/?cachebust={time.time()}", wait_until="domcontentloaded")
        page.wait_for_function("() => typeof MODE !== 'undefined'", timeout=30000)
        page.evaluate("t => localStorage.setItem('wavr.token.' + location.origin, t)",
                      token)
        page.reload()
        page.wait_for_selector("#runtimeChip:not([hidden])", timeout=30000)
        page.wait_for_timeout(6000)

        state = page.locator("#runtimeChip").get_attribute("data-state")
        text = page.locator("#runtimeChip").inner_text()
        assert state != "unavailable", (
            f"a paired phone is being told a live Core is not answering: "
            f"state={state!r} text={text!r}")
        assert "not answering" not in text.lower(), text
    finally:
        page.close()
        ctx.close()
