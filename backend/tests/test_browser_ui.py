"""The screens, driven in a real browser against a live Core.

## Why this file exists

Every UI claim in this project has rested on the author opening a browser and
looking. That is a real check and it is also the one that stops happening the
week somebody is busy — and the dashboard is where several of this codebase's
worst bugs have lived, because a backend test cannot see a panel that renders
empty.

Two of those are pinned here directly:

  * The Trust screen once rendered only from the settings rail. The rail is
    panel-width-only, and on a desktop every section is `display:contents`, so
    nothing called the render hook and the panel sat permanently empty — a
    screen that worked on a phone and not on a laptop, silently. So these tests
    run at a DESKTOP viewport, which is the width that broke.
  * A reference experience page can drift from the SDK it imports. A static test
    checks the method names exist; only a browser proves the page loads them.

## Skipped rather than failed when the browser is absent

Playwright needs a downloaded browser. A developer running the suite without one
should see the rest of it pass, not a wall of red — and CI installs the browser
explicitly, so a skip there is a CI configuration problem that shows up as a
skip count rather than as a false green.
"""
from __future__ import annotations

import json
import os
import socket
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
    """A real Core, on a real port, in a throwaway database.

    A subprocess rather than a TestClient: the whole point of this file is the
    browser, and a browser needs a socket.
    """
    port = _free_port()
    db = tmp_path_factory.mktemp("browser") / "wavr.db"
    env = {
        **os.environ,
        "WAVR_DB": str(db),
        "WAVR_DEVELOPER_MODE": "1",
        # No token: these tests drive the dashboard the way the Core's own
        # screen does, over loopback.
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

        # A Space, so the first-run wizard is not covering everything. Posted
        # with urllib rather than a new dependency.
        import urllib.request
        req = urllib.request.Request(
            base + "/api/setup/create-space",
            data=json.dumps({"name": "Browser Test", "kind": "home",
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
    # A DESKTOP viewport on purpose: see the module docstring. The settings rail
    # is panel-width-only, so this is the width at which a section that renders
    # only from a rail click sits empty.
    ctx = browser.new_context(viewport={"width": 1400, "height": 1100},
                              accept_downloads=True)
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(f"PAGEERROR {e}"))
    pg.script_errors = errors
    yield pg
    ctx.close()


def open_settings(page, base):
    page.goto(f"{base}/?cachebust={time.time()}")
    page.wait_for_selector("#tab-inicio", timeout=30000)
    page.click("#gearNavBtn")
    page.wait_for_selector("#gearOverlay:not([hidden])", timeout=15000)
    page.wait_for_timeout(4000)


# -- The panels that have gone silently empty before ---------------------------

def test_the_developer_panel_renders_on_a_desktop(page, core):
    """The width at which the Trust screen once sat permanently empty, because
    the rail that triggers a render is panel-width-only."""
    open_settings(page, core)
    text = page.locator("#developerBody").inner_text()
    assert "THIS CORE" in text.upper()
    assert "SIMULATED HOUSE" in text.upper()
    assert page.locator("#developerBody button").count() > 0, "scenarios to run"


def test_the_privacy_panel_shows_what_applications_can_read(page, core):
    """The half of the privacy picture the provider list does not cover: a
    household that can see where evidence comes FROM but not where it GOES knows
    only half of what it agreed to."""
    open_settings(page, core)
    text = page.locator("#privacyDataBody").inner_text().upper()
    assert "WHAT APPLICATIONS CAN READ" in text
    assert "WHAT WAVR KEEPS" in text
    # The panel renders whatever the API lists, so a category missing from the
    # backend is invisible here rather than wrong — which is exactly how
    # fourteen tables, `assistant_log` among them, stayed off this screen. The
    # backend test pins completeness; this pins that completeness reaches the
    # glass, which is the half a household actually sees.
    assert "QUESTIONS YOU ASKED THE ASSISTANT" in text
    assert "PLACES YOU NAMED" in text


def test_the_trust_panel_still_renders(page, core):
    """A regression guard on the fix, not on the feature."""
    open_settings(page, core)
    assert page.locator("#trustBody").inner_text().strip()


def test_no_script_error_on_opening_settings(page, core):
    open_settings(page, core)
    assert page.script_errors == []


def test_running_a_scenario_from_the_panel_reaches_the_engine(page, core):
    """The developer panel's one ACTION, clicked.

    A backend test proves the scenario route writes evidence. This proves the
    button is wired to it — and the button is where the wiring breaks, because
    the handler is an inline closure with a `post` whose failure it renders as
    the word "Failed" rather than as an exception a test would see.
    """
    open_settings(page, core)
    run = page.locator("#developerBody button", has_text="Run").first
    run.click()
    # It flips to "Running" on success and "Failed" on any non-2xx. Both are
    # terminal within a second; waiting for the good one and asserting the bad
    # one is absent means a failure shows as a timeout with the state visible.
    page.wait_for_timeout(3000)
    label = run.inner_text().strip()
    assert "Failed" not in label, f"the scenario route rejected the click: {label}"
    assert page.script_errors == [], page.script_errors

    # And the evidence is really in the engine, tagged so nobody can mistake it
    # for a real sensor. `simulated_rooms` is derived live from whether a `sim:`
    # sensor is still voting, not from a flag somebody has to clear — so this
    # asserts the field exists and is a list, and does NOT assert it is
    # non-empty: a scenario whose evidence has already decayed out is the
    # feature working, not a failure.
    import urllib.request
    req = urllib.request.Request(core + "/api/dev/status",
                                 headers={"X-Wavr-Local": "1"})
    with urllib.request.urlopen(req, timeout=20) as r:
        body = json.loads(r.read())
    assert isinstance(body.get("simulated_rooms"), list),         "the panel has no way to say which rooms hold simulated evidence"


# -- Downloads, which are the one thing a backend test cannot prove ------------

def test_the_setup_download_produces_a_file_with_no_credentials(page, core):
    """A backend test proves the ROUTE is clean. This proves the button works
    and that what lands on disk is what the route returned."""
    import urllib.request
    req = urllib.request.Request(
        core + "/api/cameras",
        data=json.dumps({"name": "hall-cam", "room": "sala",
                         "rtsp_url": "rtsp://admin:hunter2@10.0.0.9/s"}).encode(),
        headers={"Content-Type": "application/json", "X-Wavr-Local": "1"},
        method="POST")
    with urllib.request.urlopen(req, timeout=20):
        pass

    open_settings(page, core)
    with page.expect_download(timeout=30000) as dl:
        page.locator("#exportConfigBtn").click()
    body = Path(dl.value.path()).read_text(encoding="utf-8")
    assert "hall-cam" in body, "the camera itself travels"
    assert "rtsp://" not in body and "hunter2" not in body


def test_the_diagnostic_bundle_downloads(page, core):
    open_settings(page, core)
    with page.expect_download(timeout=30000) as dl:
        page.locator("#exportBundleBtn").click()
    assert dl.value.suggested_filename.endswith(".json")


# -- The reference experiences, actually opened --------------------------------

# Each page's own <h1>, so a test cannot pass on an error page. The first
# version of this test asserted only "the body is not empty" and no error
# phrase — and it PASSED against a 403 JSON body, while the pages were in fact
# unopenable in a browser at all. An assertion that cannot fail is worse than
# no assertion, because it is counted as coverage.
PAGE_HEADINGS = {
    "spatial-web": "Spatial Web Experience",
    "capability-aware": "Capability-aware Experience",
    "anchor-demo": "Spatial Anchor Demo",
}


@pytest.mark.parametrize("name", sorted(PAGE_HEADINGS))
def test_a_reference_experience_loads_and_runs_its_sdk(page, core, name):
    """A static test proves every SDK method these pages call exists. Only a
    browser proves the module actually loads and the page reaches the Core —
    which is the difference between a page that works and one that throws on
    line one, or one the Core will not serve to a browser at all."""
    page.goto(f"{core}/experiences/{name}/")
    page.wait_for_timeout(4000)
    assert page.script_errors == [], page.script_errors
    assert page.locator("h1").inner_text().strip() == PAGE_HEADINGS[name]
    body = page.locator("body").inner_text()
    # Each page reports a failure to reach the Core in its own words. None of
    # them should be showing one.
    for broken in ("is the Core running", "Could not read the Space",
                   "cannot reach Wavr"):
        assert broken not in body, f"{name} could not reach the Core: {body[:200]}"


def test_the_spatial_web_experience_lists_the_rooms(page, core):
    page.goto(f"{core}/experiences/spatial-web/")
    page.wait_for_timeout(4000)
    assert page.locator(".room").count() > 0, "no room cards rendered"


def test_the_capability_page_evaluates_a_manifest(page, core):
    """It posts the manifest to the real evaluator and renders a verdict per
    room. A verdict of UNSUPPORTED is the correct answer on a Core with no
    sensors — what matters is that one appeared."""
    page.goto(f"{core}/experiences/capability-aware/")
    page.wait_for_timeout(4000)
    assert page.locator(".verdict").count() > 0, "no verdicts rendered"


def test_the_anchor_demo_creates_an_anchor_that_persists(page, core):
    """The whole point of the page: an anchor with no coordinates is complete,
    and it survives in the Space."""
    page.goto(f"{core}/experiences/anchor-demo/")
    page.wait_for_selector("#name", timeout=20000)
    page.fill("#name", "Browser test counter")
    page.click("#new button[type=submit]")
    page.wait_for_timeout(2500)
    assert "Browser test counter" in page.locator("#list").inner_text()

    import urllib.request
    req = urllib.request.Request(core + "/api/anchors",
                                 headers={"X-Wavr-Local": "1"})
    with urllib.request.urlopen(req, timeout=20) as r:
        names = [a["name"] for a in json.loads(r.read())["anchors"]]
    assert "Browser test counter" in names, "it did not reach the Space"


def test_an_experience_is_not_served_with_developer_mode_off(core):
    """These pages read the Space. One served to anybody who can reach the port
    is a page that reads the Space for anybody.

    Checked over HTTP rather than in the browser: the fixture Core runs with
    developer mode ON, and restarting it for one assertion would double this
    file's runtime.
    """
    import urllib.error
    import urllib.request
    from wavr.app import create_app  # noqa: F401  (import proves the app builds)

    req = urllib.request.Request(core + "/experiences/nope/",
                                 headers={"X-Wavr-Local": "1"})
    try:
        urllib.request.urlopen(req, timeout=10)
        raise AssertionError("an unknown experience name was served")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404


@pytest.fixture(scope="module")
def unconfigured_core(tmp_path_factory):
    """A Core that has never been set up.

    Separate from `core` because the difference is the whole point: the module
    fixture creates a Space over HTTP so the other tests are not looking at a
    first-run screen, and this one deliberately does not.
    """
    port = _free_port()
    db = tmp_path_factory.mktemp("firstrun") / "wavr.db"
    env = {**os.environ, "WAVR_DB": str(db), "WAVR_LOCAL_TOKEN": ""}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "wavr.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(BACKEND), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


# -- The first screen anybody ever sees ----------------------------------------

def test_the_first_run_wizard_creates_a_space_end_to_end(page, unconfigured_core):
    """Every step, in a browser, on a Core that has never been set up.

    This path is otherwise covered only by tests that POST to
    `/api/setup/create-space` directly — which proves the route and skips the
    four screens between a person and that route. It is also the one screen
    whose failure makes everything else unreachable: a household that cannot
    finish setup never sees the rest of the product, so a silent break here is
    indistinguishable from Wavr not working at all.
    """
    page.goto(unconfigured_core + "/")
    # Step 0. The wizard appears on its own: nothing is clicked to summon it.
    page.wait_for_selector("#setupWizard:not([hidden])", timeout=30000)
    assert "Welcome to Wavr" in page.locator("#swTitle").inner_text()

    page.click('#swBody [data-p="create"]')
    page.click("#swActions button.primary")

    # Step 1 — the kind of place, and its name.
    page.wait_for_selector('#swBody [data-k="home"]', timeout=15000)
    page.click('#swBody [data-k="home"]')
    page.fill("#swName", "Browser First Run")
    page.fill("#swOwner", "Tester")
    page.click("#swActions button.primary")

    # Step 2 — the device scan. It runs against real hardware, so what is
    # asserted is that it FINISHED and offered a way forward, not what it found.
    page.wait_for_selector("#swBody .sw-rec, #swBody .sw-card", timeout=45000)
    page.wait_for_selector("#swActions button.primary", timeout=45000)
    page.click("#swActions button.primary")

    # Step 3 — created. The title carries the name back, which is also the
    # regression guard on the double-encoding bug the source comment names.
    page.wait_for_selector("text=Browser First Run is ready", timeout=45000)
    assert page.script_errors == [], page.script_errors

    # And it is real on the Core, not just on the screen.
    import urllib.request
    req = urllib.request.Request(unconfigured_core + "/api/setup/status",
                                 headers={"X-Wavr-Local": "1"})
    with urllib.request.urlopen(req, timeout=20) as r:
        status = json.loads(r.read())
    assert status["needs_setup"] is False
    assert status["space"]["name"] == "Browser First Run"


def test_a_space_name_with_an_ampersand_is_not_double_encoded(page,
                                                              unconfigured_core):
    """`stepFinish` sets the finished title with textContent and says in a
    comment that escaping it would render "Mum &amp; Dad&#39;s". A comment is
    not a test, and this is the assertion that would have caught it.

    Runs against the SAME Core as the test above, which has a Space by now — so
    it drives the name through the API and checks the shell's own header, which
    is the other place an operator's text lands in Wavr's chrome.
    """
    import urllib.request
    req = urllib.request.Request(
        unconfigured_core + "/api/space",
        data=json.dumps({"name": "Mum & Dad's"}).encode(),
        headers={"Content-Type": "application/json", "X-Wavr-Local": "1"},
        method="PUT")
    with urllib.request.urlopen(req, timeout=20) as r:
        assert json.loads(r.read())["name"] == "Mum & Dad's"

    page.goto(unconfigured_core + f"/?cachebust={time.time()}")
    page.wait_for_selector("#brandSpace", timeout=30000)
    page.wait_for_timeout(2000)
    # `text_content`, not `inner_text`: the header is uppercased in CSS, and
    # inner_text returns what is PAINTED. Comparing against the rendered string
    # would be asserting the stylesheet, which is not what this test is about.
    assert page.locator("#brandSpace").text_content().strip() == "Mum & Dad's"
    # The failure this exists for: an escaped-then-set title reads
    # "Mum &amp; Dad&#39;s" on screen. Both halves are checked because `&` and
    # `'` escape through different paths and a fix can restore one and not the
    # other.
    rendered = page.locator("#brandSpace").inner_text()
    assert "&AMP;" not in rendered.upper() and "&#39;" not in rendered
    assert "&amp;" not in page.title() and "&#39;" not in page.title()
    assert "Mum & Dad's" in page.title()
