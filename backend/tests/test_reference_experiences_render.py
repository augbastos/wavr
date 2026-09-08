"""The three pages the Core serves an integrator, rendered.

## Why a browser and not a unit test

These pages exist to show somebody how to read a room. They are the first Wavr
code an integrator runs, and the only executable documentation the product has.
A field read off the wrong key does not throw in JavaScript — it evaluates to
`undefined` and lands in the sentence. `capability-aware` shipped
`{screen: where.name}` on a context device that has never had a `name`, so its
headline example read "2 people are here, shown on undefined." on the one page
whose whole job is to be copied.

Nothing could have caught that from the outside. The TypeScript declaration
asserted the same wrong field, so it agreed; the Kotlin SDK read it too. Three
readers and one producer, and the producer was right.

## Why the payload comes from the producer

The context is served by `page.route`, and its BODY is built by calling
`wavr.experience.build_context(...)` — the real producer, the same function the
HTTP route, the MCP tool and the SDK endpoint all call. A hand-written fixture
would be a fourth opinion about the shape, and this repository has already been
bitten by a test whose fixture the product never produces.

So the assertion is narrow and total: render each page against a real context
and require that no literal "undefined" or "null" or "[object Object]" reaches
the text a person reads. That catches every field read off a key the Core does
not serve, in all three pages, without this file having to know which keys
those are.
"""
from __future__ import annotations

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
sys.path.insert(0, str(BACKEND))

pytest.importorskip("playwright.sync_api",
                    reason="browser cases run in the browser CI job")
from playwright.sync_api import sync_playwright  # noqa: E402

PAGES = ("spatial-web", "capability-aware", "anchor-demo")

# Text a person must never read. Each is what JavaScript renders when a field
# was looked up under a key its producer does not use.
LEAKED_UNDEFINED = ("undefined", "[object Object]", "NaN")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def core(tmp_path_factory):
    """A Core in developer mode, which is the only mode that serves these."""
    port = _free_port()
    db = tmp_path_factory.mktemp("experiences") / "wavr.db"
    env = {
        **os.environ,
        "WAVR_DB": str(db),
        "WAVR_HOUSE_MAP": str(db.parent / "house.json"),
        "WAVR_DEVELOPER_MODE": "1",
        "WAVR_LOCAL_TOKEN": "",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "wavr.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(BACKEND), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
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
            base + "/api/setup/create-space",
            data=json.dumps({"name": "Reference", "kind": "home",
                             "owner_name": "Tester", "room": "sala"}).encode(),
            headers={"Content-Type": "application/json", "X-Wavr-Local": "1"},
            method="POST")
        with urllib.request.urlopen(req, timeout=20):
            pass
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _real_verdict(space_context: dict) -> dict:
    """The compatibility answer, from the real evaluator.

    `capability-aware` asks TWO routes and renders the intersection: the room
    context, and a per-room verdict on its manifest. The first draft of this
    file answered only the context, so the page took the real Core's verdict —
    UNSUPPORTED, because a bare test Core watches nothing — and rendered
    "Nothing shown here." for every room. Every assertion below passed while
    the branch under test never ran. That is the failure this whole file is
    about, reproduced inside the file itself on the first attempt.
    """
    from wavr.experience_manifest import evaluate_space, parse

    # The page's OWN default manifest, verbatim, so the verdict this test feeds
    # back is the verdict the page would really have received.
    manifest = parse({
        "id": "occupancy-display",
        "name": "Occupancy Display",
        "requires": ["presence"],
        "optional": ["count", "display"],
        "scopes": ["room.presence", "room.count", "devices.read"],
    })
    return evaluate_space(manifest, space_context)


def _real_context() -> dict:
    """A Space context in the shape the PRODUCER emits, never a hand-written one.

    Populated enough that every branch these pages have has something to render:
    a counted room with a display and an anchor, and a second room nothing can
    answer for — which is the case they exist to demonstrate.
    """
    from wavr.experience import build_space_context

    rooms = ["sala", "sotao"]
    states = {
        "sala": {"room": "sala", "occupied": True, "person_count": 2,
                 "confidence": 0.82, "precision_level": "count"},
        "sotao": {"room": "sotao", "occupied": None, "person_count": None,
                  "confidence": 0.0, "precision_level": "none"},
    }
    coverage_rows = [
        {"room": "sala", "sensor_id": "cam-1", "modality": "camera",
         "health": "ok", "precision_level": "count", "calibrated": True},
    ]
    devices = [
        {"device_id": "dev-1", "room": "sala", "kind": "screen", "display": True},
        {"device_id": "dev-2", "room": "sala", "kind": "phone", "display": None},
    ]
    anchors = [{"anchor_id": "a-1", "room": "sala", "name": "the counter",
                "kind": "logical", "positioned": False}]
    ctx = build_space_context(
        rooms=rooms, space={"space_id": "sp_1", "name": "Reference"},
        states=states, coverage_rows=coverage_rows, devices=devices,
        anchors=anchors)
    return ctx if isinstance(ctx, dict) else ctx.to_dict()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        yield b
        b.close()


@pytest.mark.parametrize("name", PAGES)
def test_a_reference_page_renders_no_undefined(browser, core, name):
    """Every field these pages read must be one the Core serves."""
    context = _real_context()
    body = json.dumps(context)
    verdict = json.dumps(_real_verdict(context))
    page = browser.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        # BOTH routes, each answered with its own producer's shape. Answering
        # only the context left the page taking the bare Core's UNSUPPORTED
        # verdict and rendering "Nothing shown here." — passing this test
        # without ever reaching the branch it exists to check.
        page.route("**/api/experience/context**",
                   lambda route: route.fulfill(
                       status=200, content_type="application/json", body=body))
        page.route("**/api/experience/compatibility**",
                   lambda route: route.fulfill(
                       status=200, content_type="application/json", body=verdict))
        page.goto(f"{core}/experiences/{name}/", wait_until="domcontentloaded")
        page.wait_for_timeout(3500)

        text = page.locator("body").inner_text()
        assert text.strip(), f"{name} rendered nothing at all"
        # The page must have got PAST the "nothing to show" branch, or the
        # check below is measuring an empty screen.
        if name == "capability-aware":
            assert "people are here" in text or "person is here" in text, (
                "the headcount branch did not render, so this test is not "
                f"measuring what it exists to measure:\n{text[:500]}")
        for token in LEAKED_UNDEFINED:
            assert token not in text, (
                f"{name} rendered {token!r}, which is what JavaScript produces "
                f"when a field is read off a key its producer does not use:\n"
                + "\n".join(ln for ln in text.splitlines() if token in ln)[:400])
        assert not errors, f"{name} threw: {errors[:3]}"
    finally:
        page.close()


@pytest.mark.parametrize("name", PAGES)
def test_a_reference_page_survives_a_core_that_says_nothing(browser, core, name):
    """A dark Core is the case an integrator hits first, on the day they point
    the page at the wrong address. It must say so rather than sit blank or
    render a confident sentence about a room it never heard about."""
    page = browser.new_page()
    try:
        # Never fulfilled: the socket is open and the answer never comes, which
        # is what a dark host does and what a closed port does not.
        page.route("**/api/experience/context**", lambda route: None)
        page.goto(f"{core}/experiences/{name}/", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        text = page.locator("body").inner_text().lower()
        assert text.strip(), f"{name} is blank against a Core that says nothing"
        # It must not be claiming to know anything about the Space.
        for claim in ("people are here", "person is here", "someone is here"):
            assert claim not in text, (
                f"{name} asserts occupancy while the Core has answered nothing")
    finally:
        page.close()
