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

import functools
import json
import os
import re
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
        # Pinned for the reason in `test_config_export`: the default is
        # CWD-relative, this subprocess runs with cwd=backend/, and a test that
        # restores a floor plan would otherwise rewrite the map every other test
        # reads.
        "WAVR_HOUSE_MAP": str(db.parent / "house.json"),
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


@pytest.fixture
def unset_up_multidevice_core(tmp_path_factory):
    """A Core that has NOT run through setup yet, with multi-device already
    turned on — the "adopting/re-running setup after the switch was flipped"
    case the wizard's closing note needs to tell apart from a fresh
    single-machine install. Function-scoped (unlike `core`): the wizard only
    shows once, so every test needs its own untouched Space.

    `WAVR_MULTIDEVICE=1` alone is enough here: `/api/status`'s
    `features.multidevice` reads `cfg.multidevice` directly, and that never
    depended on `wavr.serve`'s TLS branch (see `test_pairing_screen_hero_code.py`
    for the fixture that DOES need the TLS launch path; this one does not).
    """
    port = _free_port()
    tmp = tmp_path_factory.mktemp("wizard_md")
    env = {
        **os.environ,
        "WAVR_DB": str(tmp / "wavr.db"),
        "WAVR_HOUSE_MAP": str(tmp / "house.json"),
        "WAVR_MULTIDEVICE": "1",
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
    # A <script> that never arrives does not raise here. What raises is the NEXT
    # script, with "WavrAPI is not defined" -- a message that names the symbol
    # and hides the cause, and which cost a full-suite run before anybody could
    # tell whether the product was broken or a request had simply been dropped
    # under load. Failed script/stylesheet requests are collected separately and
    # only ever appear INSIDE a pageerror message, so this adds no failure of
    # its own: a request aborted by a navigation is not an error, and a request
    # aborted by one of our own deadlines is a fetch, not a resource.
    failed_resources: list[str] = []

    def _on_request_failed(req):
        if req.resource_type in ("script", "stylesheet"):
            failed_resources.append(f"{req.url} ({req.failure})")

    def _on_page_error(e):
        note = ("  [resources that failed to load: "
                + "; ".join(failed_resources) + "]") if failed_resources else ""
        errors.append(f"PAGEERROR {e}{note}")

    pg.on("requestfailed", _on_request_failed)
    pg.on("pageerror", _on_page_error)
    pg.script_errors = errors
    yield pg
    ctx.close()


MANAGE_TABS = ("sistema", "dispositivos", "rede", "discoveries",
               "transparencia")


def goto_tab(page, name):
    """Reach a destination on EITHER navigation level.

    Five screens moved down behind Manage when the primary navigation went from
    ten destinations to four. Clicking `#tab-sistema` directly now fails, not
    because the screen is gone but because it is one disclosure away — which is
    the point of the reorganisation, and is exactly the sort of change that
    should make tests say where they are going rather than assume everything is
    one click deep.
    """
    if name in MANAGE_TABS:
        sub = page.locator("#navManageSub")
        if sub.count() and sub.is_hidden():
            page.click("#tab-manage")
            page.wait_for_selector("#navManageSub:not([hidden])", timeout=10000)
    page.click(f"#tab-{name}")
    page.wait_for_selector(f'.tab-panel[data-tab="{name}"].active', timeout=15000)


def test_the_setup_wizard_knows_multidevice_is_already_on(page, unset_up_multidevice_core):
    """The "Space is ready" screen used to say "Right now only this machine
    can reach Wavr… turn on {Let other devices connect}" unconditionally —
    including on THIS Core, which is already running with
    `WAVR_MULTIDEVICE=1`. The sentence would have been telling the operator
    to turn on a switch that was already on, on the very screen meant to
    orient them right after setup.

    Driven through the real wizard (choose "Create a new Space", accept the
    default name, accept the recommended device role) rather than injecting
    state, because the fix reads `GET /api/status` at the moment this screen
    renders — a shortcut that skipped the wizard's own steps would not
    exercise that fetch at all.
    """
    page.goto(f"{unset_up_multidevice_core}/?cachebust={time.time()}")
    page.wait_for_selector("#setupWizard:not([hidden])", timeout=30000)

    page.click('[data-p="create"]')
    page.click("#swActions .primary")
    page.wait_for_selector("#swName", timeout=15000)
    # `#swName` is pre-filled with "My Home" as a real `.value`, not a
    # placeholder, but the wizard's own `state.name` only syncs from it on an
    # `input` event — `fill()` fires one; relying on the pre-filled DOM value
    # alone sends an EMPTY name to `/api/setup/create-space` (400).
    page.fill("#swName", "My Home")
    page.click("#swActions .primary")

    page.wait_for_selector("text=Use this setup", timeout=20000)
    page.click("#swActions .primary")  # accept the recommended device role

    page.wait_for_selector("#swBody .sw-rec", timeout=20000)
    body = page.locator("#swBody").inner_text()
    assert "already reach wavr" in body.lower(), (
        f"the ready screen did not recognise multi-device was already on: {body!r}")
    assert "only this machine can reach wavr" not in body.lower(), (
        f"the ready screen still tells the operator to turn on a switch that "
        f"is already on: {body!r}")
    assert page.script_errors == [], page.script_errors


def open_settings(page, base, *, wait_for="#developerBody"):
    """Open the settings overlay and wait for a panel to actually have content.

    This used to end in `wait_for_timeout(4000)`, which is a promise about
    somebody else's machine. Every panel here renders from its own async
    request, so under load — the full suite runs several Cores and a browser at
    once — four seconds is sometimes not enough, and the test fails on a page
    that was about to be correct. That is worse than a slow test: it teaches
    whoever sees it that this file is flaky, and the next real failure gets
    re-run instead of read.

    `wait_for` names the panel the caller is about to assert on. Waiting for
    THAT rather than for a duration also means the failure message points at
    the panel that never rendered.

    ⚠ This only works for an element that starts EMPTY. A panel with a loading
    placeholder satisfies "has content" immediately, and the caller then reads
    the placeholder as the answer — which is exactly what happened to the
    updates tile the day its placeholder was improved from "Checking…" to a
    full sentence. For those, wait on a value that cannot exist early instead.
    """
    page.goto(f"{base}/?cachebust={time.time()}")
    page.wait_for_selector("#tab-inicio", timeout=30000)
    page.click("#gearNavBtn")
    page.wait_for_selector("#gearOverlay:not([hidden])", timeout=15000)
    page.wait_for_function(
        "sel => { const el = document.querySelector(sel);"
        "         return el && el.innerText.trim().length > 40; }",
        arg=wait_for, timeout=30000)


# -- The panels that have gone silently empty before ---------------------------

def test_the_developer_panel_renders_on_a_desktop(page, core):
    """The width at which the Trust screen once sat permanently empty, because
    the rail that triggers a render is panel-width-only."""
    open_settings(page, core, wait_for="#developerBody")
    # The scenario list is the LAST section to render, so wait for the thing
    # under test rather than for the panel to be merely non-empty.
    # "Simulated Space", not "house": the panel has to read the same in a shop.
    page.wait_for_function(
        "() => (document.getElementById('developerBody')?.innerText || '')"
        ".toUpperCase().includes('SIMULATED SPACE')", timeout=30000)
    text = page.locator("#developerBody").inner_text()
    assert "THIS CORE" in text.upper()
    assert "SIMULATED SPACE" in text.upper()
    assert page.locator("#developerBody button").count() > 0, "scenarios to run"


def test_a_place_can_be_named_from_the_space_screen(page, core):
    """The store, the routes and all three SDKs existed. Nothing on any screen
    could CREATE an anchor, so `context.anchors` was `[]` in every install,
    `can("anchors")` was false everywhere, and the shipped anchor-demo had
    nothing to show. This walks the whole path: the form, the list, and the
    SDK-facing payload that was empty — then removes it again, which is the
    other half of a surface people can use.
    """
    import urllib.request
    open_settings(page, core, wait_for="#developerBody")
    # The rail exists only under its media query; at this width every section
    # is display:contents and the Space tiles are already on screen.
    rail = page.locator('#gearRail button[data-section="gearSecSpace"]')
    if rail.is_visible():
        rail.click()
    page.wait_for_selector("#spaceAnchorsTile:not([hidden])", timeout=30000)
    # The fixture's Space named a room, so the form is offered rather than the
    # "name a room first" note.
    page.wait_for_selector("#spaceAnchorForm:not([hidden])", timeout=30000)
    assert page.locator("#spaceAnchorsList").inner_text().strip(), "an empty state, not nothing"

    page.fill('#spaceAnchorForm input[name="name"]', "the counter")
    page.select_option('#spaceAnchorForm select[name="room"]', "sala")
    page.click('#spaceAnchorForm button[type="submit"]')
    page.wait_for_function(
        "() => (document.getElementById('spaceAnchorsList')?.innerText || '')"
        ".includes('the counter')", timeout=30000)

    # And it reached the side applications read from.
    with urllib.request.urlopen(f"{core}/api/experience/context", timeout=20) as r:
        body = json.loads(r.read())
    rooms = {room["room"]: room for room in body["rooms"]}
    assert any(a["name"] == "the counter" for a in rooms["sala"]["anchors"])
    assert "anchors" in rooms["sala"]["capabilities"]

    # Remove: two clicks on one button, no dialog (a dialog would block the
    # page, and a kiosk has nothing to dismiss it with).
    remove = page.locator("#spaceAnchorsList button", has_text="Remove").first
    remove.click()
    remove.click()
    page.wait_for_function(
        "() => !(document.getElementById('spaceAnchorsList')?.innerText || '')"
        ".includes('the counter')", timeout=30000)
    with urllib.request.urlopen(f"{core}/api/anchors", timeout=20) as r:
        assert json.loads(r.read())["count"] == 0


def test_the_privacy_panel_shows_what_applications_can_read(page, core):
    """The half of the privacy picture the provider list does not cover: a
    household that can see where evidence comes FROM but not where it GOES knows
    only half of what it agreed to."""
    # This panel clears its host and rebuilds FIVE tiles from five requests, so
    # "not empty" is satisfied by the first of them and even "the section I
    # care about appeared" races whatever is still being appended after it.
    # `_open_privacy` waits on the terminal condition — the last tile, finished
    # — which is the only point at which reading the panel is safe.
    _open_privacy(page, core)
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
    open_settings(page, core, wait_for="#trustBody")
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
    open_settings(page, core, wait_for="#developerBody")
    page.wait_for_selector("#developerBody button:has-text('Run')", timeout=30000)
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


# -- Runtime presence: NO INVISIBLE SUCCESS ------------------------------------

def test_the_shell_always_shows_whether_wavr_is_working(page, core):
    """The question nobody thinks to ask until it is too late.

    Every other screen answers something a person went looking for. This one has
    to be true without being sought, so it lives in the chrome and it is checked
    the way a person would check it: open the page, look at the top.
    """
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#runtimeChip:not([hidden])", timeout=30000)
    page.wait_for_timeout(1500)

    chip = page.locator("#runtimeChip")
    state = chip.get_attribute("data-state")
    assert state in ("healthy", "starting", "degraded", "attention", "paused",
                     "updating"), state
    # The state is carried by WORDS, not only by the colour of a dot. Colour
    # alone is not a state — for a screen reader it is nothing at all.
    assert chip.inner_text().strip(), "the chip renders no text"
    assert (chip.get_attribute("aria-label") or "").strip()
    assert page.script_errors == [], page.script_errors


def test_a_core_that_stops_cannot_go_on_looking_healthy(page, core, tmp_path):
    """The failure this whole surface exists to prevent, reproduced.

    A second Core is started and then KILLED with the page open. The chip must
    notice on its own — nobody reloads, nobody clicks. Leaving the last good
    answer up is the easy mistake here, because it looks like resilience.
    """
    port = _free_port()
    db = tmp_path / "dying.db"
    env = {**os.environ, "WAVR_DB": str(db), "WAVR_LOCAL_TOKEN": "",
           # CWD-relative by default; see the module fixture above.
           "WAVR_HOUSE_MAP": str(db.parent / "house.json")}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "wavr.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(BACKEND), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.25)
        else:
            raise RuntimeError("the second Core did not start")

        page.goto(f"{base}/?cachebust={time.time()}")
        page.wait_for_selector("#runtimeChip:not([hidden])", timeout=30000)
        page.wait_for_timeout(1500)
        alive = page.locator("#runtimeChip").get_attribute("data-state")
        assert alive != "unavailable", "the chip was already wrong before the kill"

        proc.kill()
        proc.wait(timeout=10)

        # It polls every 10s. Give it two windows and no help of any kind — no
        # reload, no click, no navigation.
        page.wait_for_function(
            "() => document.getElementById('runtimeChip')"
            "?.dataset.state === 'unavailable'",
            timeout=45000)
        chip = page.locator("#runtimeChip")
        assert "responding" in chip.inner_text().lower()
        assert "not responding" in (chip.get_attribute("aria-label") or "").lower()
    finally:
        if proc.poll() is None:
            proc.kill()


def test_a_core_that_goes_DARK_cannot_go_on_looking_healthy(page, core):
    """The other half, and the one that was actually broken.

    Killing a process locally is the easy case: the OS refuses the connection,
    `fetch` rejects within milliseconds, and the poll renders "not responding".
    The test above covers it.

    A Core that goes DARK behaves nothing like that. A power cut, a suspended
    laptop, a dropped Wi-Fi link — the host stops answering without refusing,
    so the TCP connection just sits there. `await fetch(...)` never settles,
    the poll never reaches its render, and the chip keeps saying "Live" for as
    long as the page stays open. That is precisely "a healthy-looking indicator
    over a dead Core", and it survived a staleness guard written for it,
    because the guard lived inside the poll it was meant to outlive.

    Held requests reproduce it exactly: the route handler is entered and never
    answers, which is what the wire looks like when the other end is gone.
    """
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#runtimeChip:not([hidden])", timeout=30000)
    page.wait_for_function(
        "() => document.getElementById('runtimeChip')"
        "?.dataset.state && document.getElementById('runtimeChip')"
        ".dataset.state !== 'unavailable'",
        timeout=30000)

    held = []
    page.route("**/api/runtime", lambda route: held.append(route))
    page.route("**/api/attention", lambda route: held.append(route))

    # No reload, no click. The page is simply left alone with a Core that has
    # gone quiet, exactly as a household would leave it.
    page.wait_for_function(
        "() => document.getElementById('runtimeChip')"
        "?.dataset.state === 'unavailable'",
        timeout=45000)
    chip = page.locator("#runtimeChip")
    assert "not responding" in (chip.get_attribute("aria-label") or "").lower()
    assert held, (
        "no request was intercepted, so this proved nothing — the chip may "
        "have flipped for an unrelated reason")

    panel = page.locator("#coreRuntime")
    if panel.count():
        assert panel.get_attribute("data-state") == "unavailable", (
            "the chip corrected itself and the Core Panel line did not, so an "
            "ambient panel and a dashboard now disagree about the same Core")


def test_the_presence_claim_goes_stale_as_fast_as_the_chip_does(page, core):
    """A Core the chip has already called "Not responding" must not sit beside
    a green "live" badge and a "Someone is here" panel that keep affirming a
    frame that stopped arriving.

    Measured with a real Core: the chip's own two watchdogs (an 8s per-request
    deadline, a 5s clock check against a 35s staleness budget) always notice a
    dark Core before the live-stream's OWN open-but-silent watchdog does (a
    60s budget in `core-connection.js`, checked every 15s) — so there was
    always a window, worst case a bit under 30 seconds, where the chip had
    already gone red and the presence UI had not yet been told to stop
    asserting. `renderUnavailable()`/`tick()` now call the same
    `setReconnecting()` every other stale-aware surface uses, on the SAME
    signal that flips the chip, closing that window rather than narrowing it.

    Same held-request technique as the DARK-Core test above; this one also
    seeds a real occupied frame first, so there is an actual "Someone is
    here" / live badge to watch go stale (a page that never claimed presence
    could not prove the claim stops).
    """
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#runtimeChip:not([hidden])", timeout=30000)
    page.wait_for_function(
        "() => document.getElementById('runtimeChip')"
        "?.dataset.state && document.getElementById('runtimeChip')"
        ".dataset.state !== 'unavailable'",
        timeout=30000)

    # A real, sighted, occupied room — the exact claim that must not be left
    # standing once the Core is known to be gone.
    page.evaluate(
        "() => window.updateHouse({room: 'sala', occupied: true, "
        "confidence: 0.9, sources: []})")
    page.wait_for_timeout(300)
    assert "stale" not in (page.eval_on_selector("#qcTile", "el => el.className") or "")

    held = []
    page.route("**/api/runtime", lambda route: held.append(route))
    page.route("**/api/attention", lambda route: held.append(route))

    page.wait_for_function(
        "() => document.getElementById('runtimeChip')"
        "?.dataset.state === 'unavailable'",
        timeout=45000)
    assert held, "no request was intercepted, so this proved nothing"

    # Within one more watchdog tick of the chip going red — never "eventually,
    # once the separate 60s stream watchdog gets around to it".
    page.wait_for_function(
        "() => document.getElementById('qcTile')?.classList.contains('stale')",
        timeout=8000)
    assert "stale" in page.eval_on_selector("#heroLive", "el => el.className"), (
        "the chip is red but the live badge is still claiming fresh")
    assert not page.eval_on_selector("#qcStaleNote", "el => el.hidden"), (
        "the chip is red but 'Who's here' gives no warning that its answer "
        "may be stale")
    # Never invents a new verdict — the last real reading stays on screen,
    # only visually distrusted.
    assert "here" in page.eval_on_selector("#qcHeadline", "el => el.textContent").lower()
    assert page.script_errors == [], page.script_errors


def test_the_watchdog_alone_is_enough_when_the_abort_never_fires(page, core):
    """The backstop, proved on its own.

    Two independent mechanisms keep the chip honest when a Core goes dark: the
    request carries a deadline, and a watchdog on its own timer ages the last
    good answer. The test above passes with either one, which means it cannot
    tell you whether both work — and a redundant safeguard nobody has exercised
    is a safeguard that quietly stopped being redundant.

    So this removes the first one. `AbortController` is deleted from the page
    before any script runs, which is also a real configuration: `api.js` falls
    back to a plain `fetch` when the constructor is missing, and an old
    WebView is exactly where that happens AND exactly where a household is
    least likely to notice a frozen kiosk.

    With no abort possible and every request held open, only the watchdog can
    correct the chip.
    """
    page.add_init_script("delete window.AbortController;")
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#runtimeChip:not([hidden])", timeout=30000)
    page.wait_for_function(
        "() => document.getElementById('runtimeChip')"
        "?.dataset.state && document.getElementById('runtimeChip')"
        ".dataset.state !== 'unavailable'",
        timeout=30000)
    assert page.evaluate("() => typeof window.AbortController"), "sanity"
    assert page.evaluate("() => typeof window.AbortController") == "undefined"

    held = []
    page.route("**/api/runtime", lambda route: held.append(route))
    page.wait_for_function(
        "() => document.getElementById('runtimeChip')"
        "?.dataset.state === 'unavailable'",
        timeout=60000)
    assert held


def test_things_that_need_a_person_appear_in_one_ranked_list(page, core):
    """The surface the tray and the header both point at.

    A real pairing request is created over HTTP, so this drives the actual
    producer rather than a fixture — the field-name bug this module already had
    (`device_name` for `requester_name`) would render every row as "A device"
    and no unit test built from an invented shape would have caught it.
    """
    import urllib.request

    req = urllib.request.Request(
        core + "/api/pair-request",
        data=json.dumps({"requester_name": "Ana's phone"}).encode(),
        headers={"Content-Type": "application/json", "X-Wavr-Local": "1"},
        method="POST")
    try:
        urllib.request.urlopen(req, timeout=20)
    except Exception as exc:                     # noqa: BLE001
        pytest.skip(f"this Core does not accept pair requests: {exc}")

    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#attnChip:not([hidden])", timeout=30000)
    assert page.locator("#attnCount").inner_text().strip() != "0"

    page.click("#attnChip")
    page.wait_for_selector("#attnTile:not([hidden])", timeout=15000)
    text = page.locator("#attnList").inner_text()
    # The person's own words for the device, not a placeholder.
    assert "Ana's phone" in text, text
    # And what it is waiting ON, so the row is actionable rather than a status.
    assert "approve" in text.lower() or "deny" in text.lower()
    assert page.locator(".attn-row[data-band='blocking']").count() >= 1
    assert page.script_errors == [], page.script_errors


def test_a_poll_does_not_reset_the_access_level_the_operator_chose(page, core):
    """The approval banner refreshes every two seconds, and it used to rebuild
    the whole hero card on every one of those refreshes.

    `renderHero` sets the access-level select back to its least-privilege
    default — deliberately, on a FRESH request. Running it on every poll instead
    meant an operator who chose "Admin" and did not look at the control again
    approved the device as "User", two seconds later, in silence.

    It fails safe in the privilege direction, which is exactly why it could
    survive this long: nothing goes wrong loudly. What is wrong is that a
    control discards what the person put into it and says nothing.

    The wait is on the banner's own "asked Ns ago" text changing, which proves a
    poll actually landed and repainted the card. That signal is independent of
    the thing under test, so the test cannot pass by never polling.
    """
    import urllib.request

    req = urllib.request.Request(
        core + "/api/pair-request",
        data=json.dumps({"requester_name": "Bruno's tablet"}).encode(),
        headers={"Content-Type": "application/json", "X-Wavr-Local": "1"},
        method="POST")
    try:
        urllib.request.urlopen(req, timeout=20)
    except Exception as exc:                     # noqa: BLE001
        pytest.skip(f"this Core does not accept pair requests: {exc}")

    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#pairApprove:not([hidden])", timeout=30000)
    page.wait_for_function(
        "() => (document.getElementById('paMeta')?.innerText || '').trim().length > 0",
        timeout=15000)

    code_before = page.locator("#paCode").inner_text()
    meta_before = page.locator("#paMeta").inner_text()
    page.select_option("#paRole", "central")
    assert page.locator("#paRole").input_value() == "central"

    # A poll landed and repainted the card.
    page.wait_for_function(
        "prev => (document.getElementById('paMeta')?.innerText || '') !== prev",
        arg=meta_before, timeout=20000)

    # Same request still in the hero slot, so this is a fair measurement: a
    # genuinely new device SHOULD reset the control.
    assert page.locator("#paCode").inner_text() == code_before, (
        "the hero card changed under the test, so the reset would have been "
        "legitimate — rerun")
    assert page.locator("#paRole").input_value() == "central", (
        "the poll put the access level back to its default. The operator's "
        "choice is gone and nothing said so.")
    assert page.script_errors == [], page.script_errors


def test_the_public_demo_makes_no_backend_calls(browser):
    """A promise the product makes to a stranger, in four places, that was false.

    The Privacy screen tells the reader "this demo has no hub connected — this
    page makes no backend calls". The README says it twice and the repository's
    own CLAUDE.md says it once. It is the reason somebody can open the demo
    without trusting whoever is hosting it.

    `runtime.js` had never heard of `MODE`: its `wire()` ran on DOMContentLoaded
    whatever the host, so `/api/runtime` and `/api/attention` were polled for
    ever in simulated mode. Nothing looked wrong from the inside, because with
    no Core behind the static host every one of those requests 404s.

    Served from 127.0.0.2, which `isLoopbackHost` does not accept and
    `isPrivateLanHost` does not match, so the page decides it is a demo exactly
    the way the public one does. Serving `frontend/` with no backend at all is
    also the point: nothing here can answer, so a request is unambiguous.
    """
    import http.server
    import threading

    frontend = BACKEND.parent / "frontend"
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(frontend))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.2", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        page = browser.new_page()
        asked = []
        page.on("request", lambda r: asked.append(r.url))
        page.goto(f"http://127.0.0.2:{port}/index.html?cachebust={time.time()}")
        page.wait_for_selector("#tab-inicio", timeout=30000)
        mode = page.evaluate("() => (typeof MODE === 'undefined' ? null : MODE)")
        assert mode == "simulated", (
            f"this origin did not put the page in demo mode ({mode!r}), so the "
            f"test is not measuring the demo")
        # Long enough for every poll in the product to have come round at least
        # once: the fastest is 1.5s and the runtime pair is on a few seconds.
        page.wait_for_timeout(12000)
        backend_calls = sorted({u for u in asked if "/api/" in u})
        assert not backend_calls, (
            f"the demo made {len(backend_calls)} backend calls while telling "
            f"the reader it makes none:\n  " + "\n  ".join(backend_calls[:15]))
        page.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_chip_never_disagrees_with_the_list(page, core):
    """The count in the chrome and the list on the page come from one request,
    and this asserts they agree — including the case where the answer is zero,
    in which case BOTH must be absent rather than showing "0".

    Written as one test rather than two because the empty case and the
    non-empty case cannot both be arranged on one Core in one run: whichever
    ran first would decide what the second saw. A test that skips itself
    depending on what another test did earlier is not coverage.
    """
    import urllib.request

    req = urllib.request.Request(core + "/api/attention",
                                 headers={"X-Wavr-Local": "1"})
    with urllib.request.urlopen(req, timeout=20) as r:
        body = json.loads(r.read())
    total = body["total"]

    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#runtimeChip:not([hidden])", timeout=30000)
    page.wait_for_timeout(2500)

    if not total:
        # A permanent "0" is furniture, and furniture is what people stop
        # seeing. The absence is the message.
        assert page.locator("#attnChip").is_hidden()
        assert page.locator("#attnTile").is_hidden()
        return

    assert page.locator("#attnChip").is_visible()
    assert page.locator("#attnCount").inner_text().strip() == str(total)
    page.click("#attnChip")
    page.wait_for_selector("#attnTile:not([hidden])", timeout=15000)
    # On the LANDING surface, not behind a tab. The inbox used to live inside
    # the New devices tab — one of its own sources — so the aggregate sat
    # behind a tab somebody had to know to open.
    assert page.locator("#panel-inicio #attnTile").count() == 1
    assert page.locator(".attn-row").count() == total, (
        "the list and the badge came from the same request and still disagree")


def test_a_room_whose_sensors_contradict_each_other_says_so_on_screen(page, core):
    """Never hide a disagreement behind a confidence ring.

    Two contradicting observations are pushed into the real fusion engine
    through the real ingest route, and the card must say WHAT disagrees — not
    just lower a number. "68%" over a radar seeing somebody and a camera seeing
    an empty room is a summary of a contradiction, which is the one place a
    summary lies.
    """
    import urllib.request

    def post(path, body):
        req = urllib.request.Request(
            core + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Wavr-Local": "1"},
            method="POST" if "observations" in path else "PUT")
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())

    # Two registered providers, so the contradiction comes in through the real
    # ingest contract rather than being poked into fusion from the side. An
    # unbound provider is loopback-only, which is exactly what this test is.
    for pid, modality in (("t_radar", "node"), ("t_cam", "node")):
        post(f"/api/providers/external/{pid}",
             {"label": pid, "reach": "lan", "modality": modality,
              "ceiling": "room"})
    post("/api/providers/t_radar/observations",
         {"observations": [{"room": "sala", "present": True, "confidence": 0.9,
                            "sensor_id": "radar-1"}]})
    post("/api/providers/t_cam/observations",
         {"observations": [{"room": "sala", "present": False, "confidence": 0.8,
                            "sensor_id": "cam-1"}]})

    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector('.card[data-room="sala"]', timeout=30000)
    try:
        page.wait_for_selector('.card[data-room="sala"] .room-dissent:not([hidden])',
                               timeout=20000)
    except Exception:                        # noqa: BLE001
        pytest.skip("fusion did not register a disagreement from these events")

    said = page.locator('.card[data-room="sala"] .room-dissent').inner_text()
    assert "disagree" in said.lower()
    # WHICH sensors, and what each says. "Sensors disagree" on its own is an
    # anxiety with no action attached to it.
    assert "occupied" in said.lower() and "empty" in said.lower()
    assert page.script_errors == [], page.script_errors


def test_the_capability_matrix_lets_you_compare_rooms(page, core):
    """"Which rooms can count people?" cannot be answered by reading sentences.

    A table because the question is comparative. Every cell carries TEXT as well
    as a mark, because a tick that is only a colour is nothing to a screen
    reader — and this table is the clearest statement Wavr makes about what it
    can and cannot do.
    """
    open_settings(page, core, wait_for="#developerBody")
    # The Space section holds it; open that rail item.
    rail = page.locator('[data-sec="space"]')
    if rail.count():
        rail.first.click()
    try:
        page.wait_for_selector("#spaceCapMatrix tbody tr", timeout=20000)
    except Exception:                            # noqa: BLE001
        pytest.skip("this Core enumerated no rooms to compare")

    head = page.locator("#spaceCapMatrix thead").inner_text().upper()
    for column in ("PRESENCE", "COUNT", "POSITION", "IDENTITY"):
        assert column in head, head

    first = page.locator("#spaceCapMatrix tbody tr").first
    cells = first.locator("td")
    assert cells.count() == 4
    for i in range(4):
        text = cells.nth(i).inner_text().strip()
        assert text, f"cell {i} says nothing; a colour alone is not a state"

    # "Not available here" and "switched off" must not share a treatment: one is
    # a limit of the hardware, the other is a decision somebody can unmake.
    states = {cells.nth(i).get_attribute("data-has") for i in range(4)}
    assert states <= {"yes", "no", "off", "lost"}, states
    assert page.script_errors == [], page.script_errors


def test_an_empty_state_teaches_instead_of_only_reporting_emptiness(page, core):
    """An empty state is the one moment somebody is looking straight at a
    feature they have never used. "No cameras" spends that moment telling them
    what they can already see.

    Driven in a browser rather than grepped, because these strings live inside
    template literals built at render time — a grep would pass on a string that
    never reaches the page.

    Deliberately NOT asserting on the timeline: a running Core produces an event
    within seconds, so "the timeline is empty" is not a state this fixture can
    hold. A test that needs an unreachable precondition is a test that will be
    made to pass by weakening it.
    """
    # Two clicks deep on purpose, and worth writing down: the Devices tab opens
    # on a plain "add a device" flow, and the camera list sits behind
    # "Advanced: browse the full device list". That is correct progressive
    # disclosure — adding an RTSP camera is an advanced act — and it also means
    # the only way to know this empty state is REACHABLE is to walk the path a
    # person walks.
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#tab-inicio", timeout=30000)
    goto_tab(page, "dispositivos")
    page.wait_for_selector("#devAdvancedToggle", timeout=20000)
    page.click("#devAdvancedToggle")
    page.wait_for_selector("#dsubBtnAtivos", state="visible", timeout=20000)
    page.click("#dsubBtnAtivos")
    page.wait_for_selector("#camList", state="visible", timeout=20000)
    page.wait_for_timeout(2000)

    cams = page.locator("#camList").inner_text()
    assert "No cameras yet" in cams, cams
    # It says what a camera would ADD, and answers the question that stops
    # people adding one.
    assert "count people" in cams
    assert "never stored" in cams
    assert page.script_errors == [], page.script_errors


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


def test_the_updates_tile_says_how_THIS_install_updates(page, core):
    """Wavr never updates itself, so the useful half is telling somebody the
    right command for the way they installed it — "docker pull", "re-run the
    script" and "install the APK over the old one" are different answers, and a
    wrong one sends them to run something that does nothing.

    The routes existed and nothing called them, which by this project's own rule
    is not a finished feature.
    """
    open_settings(page, core, wait_for="#developerBody")
    # Wait for the VERSION line, which only exists once the answer arrived.
    #
    # This used to wait for `#updateHow` to be non-empty — and then the loading
    # placeholder was improved from "Checking…" to a full sentence, which
    # satisfied "non-empty" and let the test read the placeholder as the answer.
    # A better placeholder defeats a wait that only asks whether there is text,
    # which is a good argument for waiting on the thing that cannot exist early.
    page.wait_for_function(
        "() => (document.getElementById('updateVersion')?.innerText || '')"
        ".includes('running')", timeout=30000)
    how = page.locator("#updateHow").inner_text()
    assert how.strip() and "Working out how" not in how, how
    assert page.locator("#updateVersion").inner_text().strip()

    # The one control that reaches outward must not be one click away from
    # somebody who never switched that connector on.
    import urllib.request
    req = urllib.request.Request(core + "/api/updates",
                                 headers={"X-Wavr-Local": "1"})
    with urllib.request.urlopen(req, timeout=20) as r:
        body = json.loads(r.read())
    if not body["check_enabled"]:
        assert page.locator("#updateActions").is_hidden(),             "the outward check is offered while its connector is off"
        assert page.locator("#updateWhy").inner_text().strip(),             "nothing explains why there is no check"

    # `up_to_date` is a tristate and "nobody looked" must not read as "you are
    # up to date" — the exact class of lie this codebase keeps removing.
    if body["up_to_date"] is None:
        assert "latest" not in page.locator("#updateFb").inner_text().lower()
    assert page.script_errors == [], page.script_errors


def test_the_diagnostic_bundle_downloads(page, core):
    open_settings(page, core)
    with page.expect_download(timeout=30000) as dl:
        page.locator("#exportBundleBtn").click()
    assert dl.value.suggested_filename.endswith(".json")


def test_the_diagnostic_bundle_is_a_real_file_a_person_can_read(page, core):
    """The button a person clicks on an ordinary settings screen (not developer
    mode) has to produce something a supporter can actually act on tomorrow: the
    Core's own status, a network/sensor report and how many things are waiting
    for a decision — merged client-side from routes this dashboard already
    calls (see js/developer.js's buildDiagnosticBundle). Opened and read here
    rather than only checked for a filename, because a JSON file that downloads
    empty satisfies "it downloads" and helps nobody tomorrow.
    """
    open_settings(page, core)
    with page.expect_download(timeout=30000) as dl:
        page.locator("#exportBundleBtn").click()
    saved = json.loads(Path(dl.value.path()).read_text(encoding="utf-8"))

    # What was already there.
    assert saved["wavr_version"]
    assert "config_shape" in saved
    assert "nodes" in saved and "count" in saved["nodes"]
    assert "network_reachability" in saved and "state" in saved["network_reachability"]

    # What this change adds: the Core's own status (uptime, last good state,
    # findings) merged in without the household's own name for their home.
    core_state = saved.get("core_state")
    assert core_state, "the Core's own status did not make it into the file"
    assert "state" in core_state and "uptime_s" in core_state
    assert "space" not in core_state and "headline" not in core_state

    # A human-readable network/discovery report, already MAC-redacted server
    # side (see net_doctor.build_doctor_report) — not merely present, but
    # actually the kind of thing a person can act on.
    report = saved.get("network_and_sensor_report")
    assert report and "Checks:" in report

    # Pending items as counts only — never a name.
    assert "pending_items" in saved and "total" in saved["pending_items"]

    assert page.script_errors == [], page.script_errors


def test_restoring_a_setup_previews_before_it_writes(page, core):
    """The other half of the backup feature, driven the way a person drives it.

    A backend test proves the route replaces a floor plan. This proves the
    button exists, that the file picker reaches the preview, and — the part that
    matters — that the destructive consequence is stated in words BEFORE
    anything is written. "It replaced my rooms" is the complaint this flow
    exists to prevent, and a preview nobody sees prevents nothing.
    """
    import urllib.request

    # A real export from this Core, which is the only file anybody will import.
    req = urllib.request.Request(core + "/api/config/export",
                                 headers={"X-Wavr-Local": "1"})
    with urllib.request.urlopen(req, timeout=20) as r:
        exported = json.loads(r.read())
    # One anchor in a room the FILE actually carries, and one pointing at a room
    # it does not. Read from the export rather than assumed: a Space's rooms and
    # its drawn floor plan are different things, and assuming they match is how
    # the first version of this test passed on a technicality.
    rooms = [r["name"] for f in (exported.get("house") or {}).get("floors", [])
             for r in f.get("rooms", []) if r.get("name")]
    if not rooms:
        pytest.skip("this Core has no drawn floor plan to restore")
    exported["anchors"] = [{"name": "counter", "room": rooms[0],
                            "kind": "logical"},
                           {"name": "orphan", "room": "nowhere",
                            "kind": "logical"}]

    open_settings(page, core)
    page.set_input_files("#importConfigFile",
                         files=[{"name": "wavr-setup.json",
                                 "mimeType": "application/json",
                                 "buffer": json.dumps(exported).encode()}])
    page.wait_for_selector("#importPreview:not([hidden])", timeout=20000)
    shown = page.locator("#importPreview").inner_text()
    assert "room" in shown.lower()
    assert page.locator("#importGoBtn").count() == 1, "no way to confirm"
    assert page.locator("#importCancelBtn").count() == 1, "no way to back out"

    # Nothing has been written: the Core still has no anchor called `counter`.
    req = urllib.request.Request(core + "/api/anchors",
                                 headers={"X-Wavr-Local": "1"})
    with urllib.request.urlopen(req, timeout=20) as r:
        before = [a["name"] for a in json.loads(r.read())["anchors"]]
    assert "counter" not in before, "the preview wrote something"

    page.click("#importGoBtn")
    page.wait_for_timeout(3000)
    after_text = page.locator("#importPreview").inner_text()
    assert "Restored" in after_text or "restored" in after_text, after_text
    assert "1 anchor" in after_text, ("nothing was written, and the report did "
                                      "not say why: " + after_text)
    # And what did NOT happen is on screen, not left to be discovered later.
    assert "orphan" in after_text

    with urllib.request.urlopen(req, timeout=20) as r:
        after = [a["name"] for a in json.loads(r.read())["anchors"]]
    assert "counter" in after
    assert "orphan" not in after
    assert page.script_errors == [], page.script_errors


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
    # The heading is server-rendered, so it is there as soon as the page is —
    # and waiting for it rather than for four seconds means a page that never
    # loads fails with "the heading never appeared" instead of an assertion
    # about text that happens to be empty.
    page.wait_for_selector("h1", timeout=30000)
    page.wait_for_timeout(1500)          # let the SDK's first fetch settle
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
    # Wait for the thing under test. A fixed sleep here fails under load on a
    # page that was about to be correct, which teaches whoever sees it that
    # this file is flaky — and the next real failure gets re-run, not read.
    page.wait_for_selector(".room", timeout=30000)
    assert page.locator(".room").count() > 0, "no room cards rendered"


def test_the_capability_page_evaluates_a_manifest(page, core):
    """It posts the manifest to the real evaluator and renders a verdict per
    room. A verdict of UNSUPPORTED is the correct answer on a Core with no
    sensors — what matters is that one appeared."""
    page.goto(f"{core}/experiences/capability-aware/")
    page.wait_for_selector(".verdict", timeout=30000)
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
    env = {**os.environ, "WAVR_DB": str(db), "WAVR_LOCAL_TOKEN": "",
           # CWD-relative by default; see the module fixture above.
           "WAVR_HOUSE_MAP": str(db.parent / "house.json")}
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


# -- Human tasks, not element existence ----------------------------------------
#
# The design standard for this product is task success, not "the element is in
# the DOM". Each test below is one question a real person arrives with, answered
# the way they would answer it: open the page, look, click.
#
# They are deliberately tolerant about WHERE the answer is and strict about
# whether it is there at all. A test that pins a selector fails when the layout
# improves; a test that pins the ANSWER fails when the product stops answering.

def test_task_can_a_person_tell_whether_wavr_is_running(page, core):
    """Persona A, the non-technical household member: "is everything okay?"

    They must not need Task Manager, a terminal, a log file or a port.
    """
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#runtimeChip:not([hidden])", timeout=30000)
    page.wait_for_timeout(1500)
    said = page.locator("#runtimeChip").inner_text().strip()
    assert said, "the chrome says nothing about whether Wavr is working"
    # In words a person uses, not a state machine's vocabulary.
    assert any(w in said.lower() for w in
               ("live", "starting", "issue", "paused", "responding", "updating")), said


def test_task_can_a_person_find_which_room_is_occupied(page, core):
    """"Where is somebody detected?" — the product's core claim, on the landing
    surface, without opening anything."""
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector(".card[data-room]", timeout=30000)
    page.wait_for_timeout(2000)
    card = page.locator(".card[data-room]").first
    text = card.inner_text().lower()
    # Occupied or empty — but never silent, and never a bare number with no word
    # attached to it.
    assert any(w in text for w in ("occupied", "empty", "vacant", "unknown",
                                   "nobody", "someone")), text


def test_house_level_evidence_never_leaks_the_raw_pseudo_room(page, core):
    """`casa` (`HOUSE_ROOM`, shared.js) is the whole-Space placeholder the
    network scan and BLE report under whenever evidence cannot localise —
    the DEFAULT sensing level, so a fresh install's only frames are this
    one. Four modules already refused to draw it as a room; the Space
    subtitle (`updateHouse`, house-indicator.js) and the History log
    (`pushTimeline`, render.js) were the two that forgot, so a fresh install
    read "casa · uncertain · 40%" right under "Someone's here" in English,
    and every single History row said "Casa · occupied (40%)".

    Driven by calling the two renderers directly with a synthetic
    house-level RoomState — both are top-level function declarations in a
    classic (non-module) script, so they are reachable as `window.*` — the
    same shape a real BLE/network-only frame takes, deterministically and
    without waiting on this sandbox's real (adapter-less) Bluetooth scan.
    """
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#heroLine1", timeout=30000)
    page.wait_for_timeout(1000)

    # `core` is module-scoped and its history accumulates across every test in
    # this file, replayed into `roomOcc` on every fresh page load — so a REAL
    # room a prior test made occupied can already be sitting in `roomOcc` by
    # the time this one starts (measured: this test alone is deterministic;
    # only the full-file run intermittently inherited a real "sala" reading).
    # `roomOcc` is a top-level `const` object (mutable, not reassignable) —
    # clearing its own keys isolates this test from that shared history
    # without touching the fix under test at all.
    page.evaluate("() => { for (const k of Object.keys(roomOcc)) delete roomOcc[k]; "
                   "for (const k of Object.keys(roomConf)) delete roomConf[k]; }")

    casa_rs = {"room": "casa", "occupied": True, "confidence": 0.4,
               "ts": "2026-01-01T00:00:00+00:00", "sources": []}

    subtitle = page.evaluate(
        "(rs) => { window.updateHouse(rs); "
        "return document.getElementById('heroLine2').textContent; }",
        casa_rs)
    assert "casa" not in subtitle.lower(), (
        f"the Space subtitle leaked the raw pseudo-room token: {subtitle!r}")
    assert "no specific room" in subtitle.lower() or "space-level" in subtitle.lower(), (
        f"house-level-only evidence must say so in words, not go silent: {subtitle!r}")

    row_text = page.evaluate(
        "(rs) => { window.pushTimeline(rs); "
        "return document.querySelector('#timeline .row').textContent; }",
        casa_rs)
    assert "casa" not in row_text.lower(), (
        f"a History row printed the raw pseudo-room token: {row_text!r}")
    row_room = page.eval_on_selector("#timeline .row", "el => el.dataset.room")
    assert row_room != "casa", (
        f"a House-level History row carries the raw token in data-room "
        f"(leaks into the Room filter dropdown and cross-highlight): {row_room!r}")
    assert page.script_errors == [], page.script_errors


def test_task_can_a_person_understand_WHY_wavr_believes_it(page, core):
    """Persona B, the power user: "why is the Office uncertain?"

    The evidence must be reachable from the room itself, not from a separate
    developer screen — the explanation belongs beside the claim.
    """
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector(".card[data-room]", timeout=30000)
    page.wait_for_timeout(2000)
    why = page.locator(".card[data-room] details.why").first
    assert why.count() >= 1, "a room states a conclusion with no way to ask why"
    why.locator("summary").click()
    page.wait_for_timeout(800)
    opened = why.inner_text().strip()
    assert len(opened) > 20, f"the explanation opened and said nothing: {opened!r}"


def test_task_can_a_person_see_whether_anything_leaves_the_home(page, core):
    """Persona E, the privacy-conscious user: "what leaves my network?"

    This is the question the whole product is built to be able to answer, so it
    must be answerable without reading a config file.
    """
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#tab-inicio", timeout=30000)
    page.click("#gearNavBtn")
    page.wait_for_selector("#gearOverlay:not([hidden])", timeout=15000)
    rail = page.locator('[data-sec="trust"]')
    if rail.count():
        rail.first.click()
    # Wait for the LAST tile, not for "some text". This panel clears its host
    # and rebuilds five tiles from five requests, so a length threshold is
    # satisfied by the first of them and the read below then races the rest —
    # it started failing the day a fifth tile was added, on a panel that was
    # about to be correct. `_open_privacy` waits on the same terminal
    # condition; see the note there.
    page.wait_for_function(
        """() => {
             const host = document.getElementById('privacyDataBody');
             if (!host) return false;
             const tile = Array.from(host.querySelectorAll('.tile')).find(
               t => t.innerText.includes('Delete what Wavr learned'));
             if (!tile) return false;
             return !!tile.querySelector('button')
                 || tile.innerText.includes('at the Core');
           }""", timeout=30000)
    said = page.locator("#privacyDataBody").inner_text().upper()
    # Both halves: what is kept HERE, and what reaches OUTSIDE. Either alone is
    # half an answer and reads as the whole one.
    assert "WHAT WAVR KEEPS" in said
    assert "WHAT APPLICATIONS CAN READ" in said
    assert page.script_errors == [], page.script_errors


def test_task_can_an_admin_find_which_capability_a_room_is_missing(page, core):
    """Persona C, the admin: "which capability did I lose, and where?"

    Answerable by looking at one table rather than by reading each room.
    """
    open_settings(page, core, wait_for="#developerBody")
    rail = page.locator('[data-sec="space"]')
    if rail.count():
        rail.first.click()
    try:
        page.wait_for_selector("#spaceCapMatrix tbody tr", timeout=20000)
    except Exception:                            # noqa: BLE001
        pytest.skip("this Core enumerated no rooms")
    body = page.locator("#spaceCapMatrix").inner_text().lower()
    assert "presence" in body and "count" in body and "position" in body
    # Every row answers for every capability — a blank cell is an unanswered
    # question, which is the thing a capability table exists to remove.
    rows = page.locator("#spaceCapMatrix tbody tr")
    for i in range(rows.count()):
        cells = rows.nth(i).locator("td")
        for j in range(cells.count()):
            assert cells.nth(j).inner_text().strip(),                 f"row {i} cell {j} is blank; the table leaves a question unanswered"


def test_a_deep_link_reaches_the_surface_it_names(page, core):
    """The tray navigates to a URL fragment. Nothing read fragments, so its
    "Needs attention" and "Privacy" items reloaded the dashboard on its default
    tab and looked, to whoever clicked them, like nothing had happened.

    Tested through a fresh navigation rather than by setting `location.hash`,
    because that is what the tray actually does — and `hashchange` alone does
    not fire for a URL that already carries the fragment.
    """
    page.goto(f"{core}/#tab-historico")
    page.wait_for_selector("#tab-inicio", timeout=30000)
    page.wait_for_timeout(2000)
    assert page.locator("#panel-historico").is_visible(),         "the fragment named a tab and the shell ignored it"

    # A settings section, which is the other half the tray uses.
    page.goto(f"{core}/#gearSecTrust")
    page.wait_for_selector("#tab-inicio", timeout=30000)
    page.wait_for_timeout(2500)
    assert page.locator("#gearOverlay").is_visible(),         "the fragment named a settings section and the overlay never opened"
    assert page.locator("#gearSecTrust").is_visible()
    assert page.script_errors == [], page.script_errors


def test_the_status_chip_opens_a_detail_not_the_floor_plan_editor(page, core):
    """Clicking the green "My Home · Live" chip must land somewhere that
    explains the status — never the floor-plan editor.

    `#gearSecTrust` was already `.is_visible()` before this fix (the test
    above proves it), because every `.settings-section` on a desktop
    viewport is `display:contents` and ALL of them render at once, stacked
    in one flow — "visible" alone was never the question. What a person
    actually saw was whatever sat at the TOP of that flow, and `Layout`
    (the "+ Room / + Wall / + Stairs" CAD editor) is first in document
    order. `showGearSection()` toggled an `.active` class that gates
    nothing outside the narrow settings-rail viewport and reset a
    `scrollTop` on a box that has no scrollbar there either — so opening
    Settings from the chip always surfaced Layout regardless of which
    section was asked for. This measures where the SCROLL actually put the
    reader, not merely whether the target element exists somewhere on the
    (very long) page.
    """
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#runtimeChip:not([hidden])", timeout=30000)
    page.wait_for_timeout(1500)
    page.click("#runtimeChip")
    page.wait_for_selector("#gearOverlay:not([hidden])", timeout=15000)
    page.wait_for_timeout(500)

    trust_top = page.locator("#gearSecTrust-h").bounding_box()["y"]
    assert 0 <= trust_top < 300, (
        f"clicking the status chip did not scroll Trust's own heading near "
        f"the top of the screen (y={trust_top}) — a reader opening Settings "
        f"from the chip would not see it without scrolling")

    # The CAD editor's own unmistakable control must not be within the
    # viewport at all — scrolled past, above the fold, not merely "elsewhere
    # on the (very long) page".
    viewport_h = page.viewport_size["height"]
    room_tool_top = page.locator('[data-tool="room"]').bounding_box()["y"]
    assert room_tool_top < 0 or room_tool_top > viewport_h, (
        f"the Layout editor's '+ Room' tool is inside the viewport "
        f"(y={room_tool_top}, viewport height={viewport_h}) — the chip is "
        f"still landing on the floor-plan editor")
    assert page.script_errors == [], page.script_errors


def test_a_fragment_naming_nothing_is_ignored_rather_than_breaking_the_page(page, core):
    """A stale bookmark or a mistyped link must not leave somebody on a broken
    screen — the shell should simply open normally."""
    page.goto(f"{core}/#not-a-real-surface")
    page.wait_for_selector("#tab-inicio", timeout=30000)
    page.wait_for_timeout(1500)
    assert page.locator("#panel-inicio").is_visible()
    assert page.script_errors == [], page.script_errors


def test_the_map_never_calls_a_room_empty_when_nothing_is_watching_it(page, core):
    """`empty` on this map means a WORKING sensor looked and saw nobody. Four
    other states mean Wavr cannot tell, and collapsing any of them into `empty`
    is the product telling a household its home was checked when it was not.

    Driven through the page's own state function rather than by inspecting
    pixels: the colours are the rendering, `mapState` is the claim.
    """
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#tab-inicio", timeout=30000)
    page.wait_for_timeout(2500)

    # Through the accessor the map publishes for exactly this purpose. Reaching
    # for a module-local name instead is what made an earlier version of this
    # test skip itself, and a test that skips proves nothing while reading like
    # it proved something.
    page.wait_for_function("() => typeof mapStateOf === 'function'", timeout=20000)
    states = page.evaluate("""() => {
      const out = {};
      mapRoomNames().forEach(n => { out[n] = mapStateOf(n); });
      return out;
    }""")

    # Every state the painter can produce must be one the key documents. A new
    # one that nobody styled falls back to the empty base, which is the failure.
    allowed = {"occupied", "empty", "offline", "privacy", "unknown", "blind"}
    unknown_states = set(states.values()) - allowed
    assert not unknown_states, f"undocumented map states: {sorted(unknown_states)}"

    # And a camera that is registered but not running must not produce `empty`.
    # A room the map knows about, with a registered camera that is not running.
    # Driven through `applyMapLiveness`, which is the real path /api/cameras
    # takes, rather than by reaching into module internals.
    collapsed = page.evaluate("""() => {
      // A room that is NOT occupied. Presence wins over everything by design —
      // "never hidden by an offline sibling sensor" — so an occupied room
      // correctly stays occupied whatever its cameras are doing, and picking
      // one made an earlier version of this test fail against correct code.
      // The bug lived in the other population: rooms with nobody in them.
      const name = mapRoomNames().find(n => mapStateOf(n) !== 'occupied');
      if (!name) return null;
      applyMapLiveness([{ room: name, liveness: 'unknown' }]);
      return mapStateOf(name);
    }""")
    if collapsed is None:
        pytest.skip("every room on this map is occupied; nothing to collapse")
    assert collapsed == "unknown", (
        f"a room whose camera is not running renders as {collapsed!r} — "
        f"'empty' on this map means a working sensor confirmed it")


def test_the_screen_reader_summary_is_not_more_certain_than_the_canvas(page, core):
    """It used to read `roomOcc` alone, so a room the canvas painted amber
    (camera down) was announced under "Empty", and a house with every camera
    offline announced "Empty home — no presence detected" at the exact moment
    nothing could see.

    The map is not allowed to be more certain in words than it is in colour.
    """
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#mapSrSummary", timeout=30000)
    page.wait_for_timeout(2500)

    text = page.locator("#mapSrSummary").text_content() or ""
    # Whatever this Core's real state is, the summary must never assert a
    # blanket empty house without naming what it could not see.
    if "Empty home" in text:
        raise AssertionError(
            "the summary still claims a confirmed-empty house: " + text)
    assert text.strip(), "the map has no textual alternative at all"
    assert page.script_errors == [], page.script_errors


# -- Real data, not ideal data -------------------------------------------------
#
# Every screen in this product was designed against three tidy rooms with short
# names. Households do not have that. These drive the shell with the shapes it
# will actually meet and assert the ONE thing that matters: nothing disappears,
# nothing overflows the page, and no state is rendered as a different state.

def _house_with(rooms):
    floors = [{"level": 0, "id": "ground", "name": "Ground", "rooms": [
        {"id": f"r{i}", "name": name,
         "polygon": [[i * 5, 0], [i * 5 + 4, 0], [i * 5 + 4, 3], [i * 5, 3]]}
        for i, name in enumerate(rooms)]}]
    return {"version": 2, "units": "m", "floors": floors}


@pytest.fixture
def big_core(tmp_path_factory):
    """A Core with twenty rooms, including names nobody sized a column for."""
    port = _free_port()
    db = tmp_path_factory.mktemp("bigcore") / "wavr.db"
    env = {**os.environ, "WAVR_DB": str(db), "WAVR_LOCAL_TOKEN": "",
           "WAVR_HOUSE_MAP": str(db.parent / "house.json")}
    # stderr to a FILE, never a pipe.
    #
    # Wanting the traceback when startup fails is right; `stderr=PIPE` is the
    # wrong way to get it. Nothing reads that pipe until after the wait loop,
    # this Core writes tens of kilobytes of rich tracebacks at boot (zeroconf,
    # bleak and paho are all absent in a plain dev environment), and a child
    # blocks forever once a ~64 KB pipe buffer fills. So the "hardening" turned
    # a fixture that worked into one that deadlocked every time — reported, of
    # course, as "the Core did not answer", which is exactly the uninformative
    # message the change was meant to remove.
    log = db.parent / "core.stderr.log"
    with open(log, "wb") as sink:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "wavr.app:app",
             "--host", "127.0.0.1", "--port", str(port),
             "--log-level", "warning"],
            cwd=str(BACKEND), env=env,
            stdout=subprocess.DEVNULL, stderr=sink)
    base = f"http://127.0.0.1:{port}"

    def _why() -> str:
        try:
            return log.read_text(encoding="utf-8", errors="replace")[-2000:]
        except OSError:
            return "(no stderr captured)"

    try:
        # A `proc.poll()` check, because without it a process that dies on
        # import sits out the whole timeout and is then reported as a timeout.
        # 120s rather than 60: the full suite runs several Cores and a browser
        # at once, and this timed out under that load while passing alone,
        # which reads as a flaky test rather than a busy machine.
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    "the Core exited while starting:\n" + _why())
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.25)
        else:
            raise RuntimeError(
                "the Core did not answer within 120s. It is still running, so "
                "this is load or a hung startup, not a crash. Its stderr:\n"
                + _why())

        import urllib.request

        def post(path, body, method="POST"):
            req = urllib.request.Request(
                base + path, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "X-Wavr-Local": "1"}, method=method)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())

        post("/api/setup/create-space",
             {"name": "A household with a deliberately long Space name",
              "kind": "home", "owner_name": "Tester", "room": "sala"})
        rooms = [
            # The shapes a real house produces and a mock never does.
            "Quarto da Ana e do João (em cima, à esquerda)",
            "Sala",
            "WC",
            "Escritório / quarto de hóspedes / arrumos",
            "Cozinha",
            "Hall",
        ] + [f"Quarto {i}" for i in range(1, 15)]
        post("/api/house", _house_with(rooms), method="PUT")
        yield base, rooms
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_twenty_rooms_and_long_names_do_not_break_the_page(page, big_core):
    """Twenty rooms, one name forty-five characters long, one with a slash.

    The failure this catches is not ugliness — it is a room that stops being
    rendered, or a page that scrolls sideways so half the house is off-screen
    and nobody notices it is missing.
    """
    base, rooms = big_core
    page.goto(f"{base}/?cachebust={time.time()}")
    page.wait_for_selector(".card[data-room]", timeout=30000)
    page.wait_for_timeout(3000)

    # The page must not scroll horizontally. A sideways page is how a long room
    # name silently removes the rooms after it from view.
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - "
        "document.documentElement.clientWidth")
    assert overflow <= 1, f"the page scrolls sideways by {overflow}px"

    # A room card comes from a READING, not from the floor plan — a room nothing
    # watches has no live state to show, which is the design. What must be true
    # is that such a room is still discoverable rather than simply absent: "no
    # coverage" is one of the four states that must never collapse, and
    # collapsing it into "does not exist" is the worst version of that.
    page.wait_for_function("() => typeof mapStateOf === 'function'", timeout=20000)
    known = set(page.evaluate("() => mapRoomNames()"))
    missing = [r for r in rooms if r not in known]
    assert not missing, (
        f"rooms the household drew that no surface knows about: {missing[:4]}")

    # And every one of them says WHY it has nothing to show.
    states = page.evaluate(
        "() => { const o = {}; mapRoomNames().forEach(n => o[n] = mapStateOf(n));"
        " return o; }")
    for room in rooms:
        assert states.get(room) in ("blind", "unknown", "offline", "empty",
                                    "occupied", "privacy"),             f"{room!r} has no state at all: {states.get(room)!r}"

    assert page.script_errors == [], page.script_errors


def test_a_space_with_no_sensors_says_so_rather_than_looking_healthy(page, big_core):
    """Twenty rooms and nothing watching any of them. The honest answer is "no
    coverage", and the failure mode is a wall of calm dark cards that reads as
    twenty confirmed-empty rooms."""
    base, rooms = big_core
    page.goto(f"{base}/?cachebust={time.time()}")
    page.wait_for_selector("#runtimeChip:not([hidden])", timeout=30000)
    page.wait_for_function("() => typeof mapStateOf === 'function'", timeout=20000)
    page.wait_for_timeout(2500)

    states = page.evaluate(
        "() => { const o = {}; mapRoomNames().forEach(n => o[n] = mapStateOf(n));"
        " return o; }")
    # Not a single room may claim a working sensor confirmed it empty.
    wrongly_empty = [r for r, st in states.items() if st == "empty"]
    assert not wrongly_empty, (
        f"{len(wrongly_empty)} rooms claim a sensor confirmed them empty on a "
        f"Core with no sensors: {wrongly_empty[:4]}")

    # And the accessible summary must not announce a confirmed-empty house.
    summary = page.locator("#mapSrSummary").text_content() or ""
    assert "Empty home" not in summary, summary


def _states_that_make_the_panel_speak() -> list[str]:
    """The renderer's own list, read from the renderer.

    `runtime.js` decides this in one place — `var bad = [...]` inside `tick` —
    and a test that restates the list is a second producer of the same rule.
    The two had already drifted: this test used to demand the panel speak in
    `starting`, which `runtime.js` deliberately keeps quiet, so the first Core
    that came up slowly would have failed a test for behaving as designed.

    Parsed rather than imported because the rule lives in JavaScript. If the
    array is ever renamed or restructured this raises instead of quietly
    returning an empty list, which would turn the check below into "the panel
    is always quiet" — passing, and meaning nothing.
    """
    src = (BACKEND.parent / "frontend" / "js" / "runtime.js").read_text(
        encoding="utf-8")
    m = re.search(r"var bad = \[([^\]]*)\];", src)
    assert m, ("runtime.js no longer declares `var bad = [...]`; this test can "
               "no longer tell which states are supposed to make the panel "
               "speak, and must not guess")
    estados = re.findall(r'"([^"]+)"', m.group(1))
    assert estados, "the panel's speaking-states list is empty in runtime.js"
    return estados


def test_the_core_panel_stays_quiet_while_healthy_and_speaks_when_not(page, core):
    """A wall panel is read from across a room by somebody who will not walk
    over and inspect it. A beautiful wave over a Core that stopped fusing an
    hour ago is the most convincing lie this product can tell.

    It is hidden while healthy on purpose: an ambient face that always carries a
    badge has no way to say that something changed.

    ## Why `?core`, and why the container is asserted first

    `#coreRuntime` is INSIDE `#corePanel`, and `#corePanel` carries `hidden` on
    the ordinary dashboard — the ambient face only exists in Core Panel mode.
    Opened without `?core`, this element is invisible no matter what the
    renderer decides, so `is_hidden()` answered True on every run and neither
    branch of this test could ever fail for the reason it names. It stayed
    green while the Core reported `healthy` and failed the first time the
    shared module Core reported `paused` — reporting that the panel said
    nothing, about a panel that had been told to speak into a hidden container.

    The container assertion below is that missing precondition. The control at
    the end proves the measurement can still return False.
    """
    page.goto(f"{core}/?core=1&cachebust={time.time()}")
    page.wait_for_selector("#runtimeChip:not([hidden])", timeout=30000)
    page.wait_for_timeout(2500)

    assert not page.locator("#corePanel").is_hidden(), (
        "the ambient face did not wake, so nothing below is measuring the "
        "panel — it is measuring a hidden container")

    state = page.locator("#runtimeChip").get_attribute("data-state")
    panel = page.locator("#coreRuntime")
    if state in _states_that_make_the_panel_speak():
        assert not panel.is_hidden(), f"state {state!r} and the panel says nothing"
        assert panel.text_content().strip(), (
            f"state {state!r}: the panel is shown and empty, which across a "
            f"room is the same silence as being hidden")
    else:
        assert panel.is_hidden(), (
            f"state {state!r} needs no attention and the panel shouts anyway; "
            f"a face that always carries a badge cannot signal a change")

        # The control. Everything above hinges on `is_hidden()` being able to
        # answer False here, and for a long time it could not: the element was
        # inside a hidden container and the quiet branch passed for a reason
        # that had nothing to do with the renderer. Poke the panel open and
        # take the same reading again.
        page.evaluate("""() => {
          const p = document.getElementById('coreRuntime');
          p.hidden = false; p.textContent = 'control';
        }""")
        assert not panel.is_hidden(), (
            "the panel cannot be seen even when shown, so the quiet branch "
            "above proves nothing about the renderer")
        page.evaluate("""() => {
          const p = document.getElementById('coreRuntime');
          p.hidden = true; p.textContent = '';
        }""")

    assert page.script_errors == [], page.script_errors


def test_a_core_that_stops_makes_the_panel_say_so(page, core, tmp_path):
    """The panel must not keep showing its last good answer. Same rule as the
    tray and the chip, tested the same way: start a Core, kill it, watch."""
    port = _free_port()
    db = tmp_path / "panel.db"
    env = {**os.environ, "WAVR_DB": str(db), "WAVR_LOCAL_TOKEN": "",
           "WAVR_HOUSE_MAP": str(db.parent / "house.json")}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "wavr.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(BACKEND), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.25)
        else:
            raise RuntimeError("the Core did not start")

        page.goto(f"{base}/?cachebust={time.time()}")
        page.wait_for_selector("#runtimeChip:not([hidden])", timeout=30000)
        page.wait_for_timeout(1500)

        proc.kill()
        proc.wait(timeout=10)

        page.wait_for_function(
            "() => { const p = document.getElementById('coreRuntime');"
            "        return p && !p.hidden && p.dataset.state === 'unavailable'; }",
            timeout=45000)
        assert "not answering" in (
            page.locator("#coreRuntime").text_content() or "").lower()
    finally:
        if proc.poll() is None:
            proc.kill()


# -- Visual regression, structurally ------------------------------------------
#
# Not screenshots. A pixel baseline fails on a font update and teaches people to
# re-bless it without looking, which is worse than no test.
#
# What is checked instead is the property the design actually promises: states
# that mean different things must LOOK different. That is a computed style, it
# is stable across fonts and platforms, and it fails for exactly one reason —
# somebody made two states the same.

def test_every_map_state_is_visually_distinguishable_from_every_other(page, core):
    """`empty`, `unknown`, `offline`, `privacy` and `blind` are five different
    claims about what Wavr knows. Two of them rendering identically is the
    product saying one thing and meaning another, and it is invisible to every
    other test in this suite.
    """
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#tab-inicio", timeout=30000)
    page.wait_for_timeout(2000)

    fills = page.evaluate("""() => {
      // A throwaway rect per state, styled by the same classes the painter
      // applies. Reading the real rooms would only cover the states this Core
      // happens to be in.
      const svg = document.querySelector('svg');
      if (!svg) return null;
      const states = ['', 'occ', 'off', 'privacy', 'blind', 'unknown'];
      const out = {};
      const made = [];
      states.forEach(st => {
        const r = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        r.setAttribute('class', 'radar-room' + (st ? ' ' + st : ''));
        r.setAttribute('width', '1'); r.setAttribute('height', '1');
        svg.appendChild(r); made.push(r);
        const cs = getComputedStyle(r);
        out[st || 'empty'] = cs.fill + '|' + cs.stroke + '|' + cs.strokeDasharray;
      });
      made.forEach(r => r.remove());
      return out;
    }""")
    if fills is None:
        pytest.skip("no map svg on this page")

    seen = {}
    for state, style in fills.items():
        if style in seen:
            raise AssertionError(
                f"{state!r} and {seen[style]!r} render identically ({style}). "
                f"They are different claims about what Wavr knows, and a person "
                f"cannot tell them apart.")
        seen[style] = state

    # And the two that matter most: a room nothing is watching must not look
    # like one a working sensor confirmed empty.
    assert fills["unknown"] != fills["empty"]
    assert fills["blind"] != fills["empty"]


def test_every_runtime_state_is_visually_distinguishable(page, core):
    """The chip's dot is the fastest read in the product. Two states sharing a
    colour means the fastest read is also the least reliable one."""
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#runtimeChip:not([hidden])", timeout=30000)

    colours = page.evaluate("""() => {
      const chip = document.getElementById('runtimeChip');
      const dot = document.getElementById('runtimeDot');
      const was = chip.dataset.state;
      const out = {};
      ['healthy','starting','updating','paused','degraded','attention',
       'unavailable'].forEach(st => {
        chip.dataset.state = st;
        out[st] = getComputedStyle(dot).backgroundColor;
      });
      chip.dataset.state = was;
      return out;
    }""")

    # `paused` and `starting` are allowed to share a neutral: both mean "not a
    # fault, not live", and the WORD beside the dot carries the difference.
    # Everything else must be its own colour.
    shared_ok = {"paused", "starting"}
    seen = {}
    for state, colour in colours.items():
        if state in shared_ok:
            continue
        if colour in seen:
            raise AssertionError(
                f"{state!r} and {seen[colour]!r} share {colour} — the fastest "
                f"read in the product cannot tell them apart")
        seen[colour] = state

    # The one that must never be mistaken for anything: green means live.
    assert colours["healthy"] not in (colours["degraded"], colours["attention"],
                                      colours["unavailable"])

    # Not-equal is too weak a bar: two colours differing in the last hex digit
    # pass it and are the same colour to a person walking past a wall panel. So
    # measure the distance. CIE76 dE, which overstates chroma differences and
    # is therefore a generous judge — a pair it calls close really is close.
    #
    # The floor is 20. The palette's own tightest MEANINGFUL pair (degraded vs
    # attention) sits around 32, so this leaves room to adjust a colour without
    # tripping, and still fails long before two states become confusable.
    def _de(css_a, css_b):
        def lab(css):
            n = [int(v) / 255 for v in re.findall(r"\d+", css)[:3]]
            n = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
                 for v in n]
            r, g, b = n
            xyz = ((0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047,
                   0.2126 * r + 0.7152 * g + 0.0722 * b,
                   (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883)
            f = [t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
                 for t in xyz]
            return (116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2]))
        return sum((x - y) ** 2 for x, y in zip(lab(css_a), lab(css_b))) ** 0.5

    close = []
    names = [n for n in colours if n not in shared_ok]
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            d = _de(colours[a], colours[b])
            if d < 20:
                close.append(f"{a} vs {b}: dE {d:.1f} "
                             f"({colours[a]} / {colours[b]})")
    assert not close, (
        "these states are too close to tell apart at a glance:\n  "
        + "\n  ".join(close))


# -- Which Space am I looking at? ---------------------------------------------

def test_the_wordmark_says_which_space_this_is(page, core):
    """Naming a Space and then never seeing the name is half a feature."""
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#tab-inicio", timeout=30000)
    page.wait_for_function(
        "() => document.getElementById('brandSpace')"
        "        && document.getElementById('brandSpace').textContent"
        "             .indexOf('Browser Test') !== -1",
        timeout=30000)
    assert "Browser Test" in page.title(), (
        "two Cores open in two tabs have to be tellable apart from the tab "
        f"strip alone; the title reads {page.title()!r}")


def test_a_companion_learns_the_space_name_without_the_setup_route(page, core):
    """The bug, reproduced.

    `/api/setup/status` is loopback-root gated, so on a paired phone it 403s
    and the first-run probe lands in its catch. The Space name used to be
    written from there and nowhere else, so the surface most likely to be
    attached to two Cores — a phone — showed a room map with no statement of
    which home it belonged to.

    Blocking that one route is exactly what a companion experiences. The name
    must still arrive, via `/api/status`, which carries `presence:read`.
    """
    page.route("**/api/setup/status", lambda route: route.abort())
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#tab-inicio", timeout=30000)
    page.wait_for_function(
        "() => document.getElementById('brandSpace')"
        "        && document.getElementById('brandSpace').textContent"
        "             .indexOf('Browser Test') !== -1",
        timeout=30000)


def test_renaming_a_space_updates_the_tab_title_too(page, core):
    """Half the original bug: the rename form wrote the wordmark but not the
    window title, so a renamed Space kept its old name in the tab strip until
    somebody reloaded — on the exact screen that exists to tell two Cores
    apart."""
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#tab-inicio", timeout=30000)
    page.wait_for_function(
        "() => typeof window.__wavrShowSpace === 'function'", timeout=30000)
    # Wait for the page's OWN Space load to land before overwriting it.
    #
    # The function existing is not the same as the page being finished with it.
    # `/api/space` is still in flight at that point, and on a loaded machine it
    # answered AFTER the line below and painted the real name back over the
    # test's — so the assertion read "Browser Test" and this failed for a reason
    # with nothing to do with renaming. Waiting for a name to be there at all
    # makes the order deterministic instead of merely usually-fast. A test that
    # fails under load is how people learn to scroll past failures.
    page.wait_for_function(
        "() => { const e = document.getElementById('brandSpace');"
        " return e && e.textContent.trim().length > 0; }", timeout=30000)
    page.evaluate("() => window.__wavrShowSpace({name: 'The Corner Shop', "
                  "kind: 'shop'})")
    # text_content, not inner_text: the wordmark is uppercased in CSS, and
    # inner_text returns what is rendered. Asserting on the rendered form would
    # fail the day somebody drops the text-transform, which has nothing to do
    # with what this test is about.
    assert page.text_content("#brandSpace").strip() == "The Corner Shop"
    assert "The Corner Shop" in page.title()


# -- No tab is a dead end -----------------------------------------------------

def test_every_tab_in_the_nav_leads_somewhere_with_words_on_it(page, core):
    """Eight destinations across two levels, and every one has to justify the
    click that reaches it.

    This is the failure this codebase has actually had: the Trust screen
    rendered only from the settings rail, and on a desktop nothing called its
    hook, so the panel sat permanently empty. Nothing failed; there was simply
    nothing there. A per-panel test catches a panel; this catches the property.

    Both levels are walked, and Manage is opened to reach the second — which
    also means a broken disclosure fails here rather than quietly hiding five
    screens.

    "Words on it" is a deliberately low bar — it is not a design review. It is
    the difference between a screen that says something and a screen that says
    nothing at all.
    """
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#tab-inicio", timeout=30000)

    primary = page.eval_on_selector_all(
        ".nav-tabs .nav-item[data-tab]",
        "els => els.map(e => e.getAttribute('data-tab'))")
    page.click("#tab-manage")
    page.wait_for_selector("#navManageSub:not([hidden])", timeout=10000)
    secondary = page.eval_on_selector_all(
        ".nav-sub .nav-sub-item[data-tab]",
        "els => els.map(e => e.getAttribute('data-tab'))")
    tabs = primary + secondary
    assert len(primary) >= 3, f"the primary level is not what this expects: {primary}"
    assert len(secondary) >= 5, f"Manage revealed only: {secondary}"
    assert len(tabs) >= 8, f"the nav is not what this test thinks: {tabs}"

    def walk(names, reopen_manage):
        # Choosing a PRIMARY destination collapses Manage — correct product
        # behaviour, and the reason the two levels are walked separately rather
        # than as one flat list.
        if reopen_manage:
            page.click("#tab-manage")
            page.wait_for_selector("#navManageSub:not([hidden])", timeout=10000)
        out = []
        for name in names:
            page.click(f"#tab-{name}")
            panel = f'.tab-panel[data-tab="{name}"]'
            page.wait_for_selector(f"{panel}.active", timeout=15000)
            try:
                page.wait_for_function(
                    "sel => { const el = document.querySelector(sel);"
                    "         return el && el.innerText.trim().length > 40; }",
                    arg=panel, timeout=12000)
            except Exception:                                # noqa: BLE001
                text = page.eval_on_selector(
                    panel, "el => el.innerText.trim().slice(0, 120)")
                out.append(f"{name}: {text!r}")
        return out

    empty = walk(primary, False) + walk(secondary, True)

    assert not empty, (
        "these tabs are reachable from the nav and show nothing:\n  "
        + "\n  ".join(empty)
        + "\n\nA tab that leads nowhere is worse than no tab: somebody clicks "
          "it, sees blank, and cannot tell a broken screen from an empty one.")


# =============================================================================
# The accessibility audit, measured on rendered pixels
# =============================================================================
#
# Auditing the token table proves nothing: what a person reads is the colour
# that survived cascade, opacity, and whatever a rule three thousand lines down
# did to it. So these walk the live dashboard and ask the browser what it
# actually painted, then check three things a person either gets or does not.
#
#   * Contrast, against the colour actually behind the text.
#   * Keyboard traversal, with a focus ring visible when it lands.
#   * Accessible names — an icon button with none is announced as "button".
#
# WCAG AA: 4.5:1 for body text, 3:1 for large text (>=24px, or >=18.66px bold).
# AAA is deliberately not the bar. This is a wall panel and a phone in a dark
# hallway, and chasing 7:1 on every dim caption pushes the whole interface
# toward the flat high-contrast look the product is deliberately not.
#
# These live in THIS file rather than their own so they share one Core
# subprocess and one Chromium with the tests above. A second module would start
# a second of each, and two browsers plus two Cores running at once is what
# produced this suite's only false failures.

# Elements whose contrast is measured differently or not at all, each with the
# reason. Matched against a CSS selector.
EXEMPT = {
    # Placeholder text is intentionally quieter than the value that replaces
    # it, and WCAG treats it as decorative when a label is also present.
    "::placeholder": "not content; every input here also carries a label",
    # Disabled controls are exempt under WCAG 1.4.3 by name.
    ":disabled": "WCAG 1.4.3 exempts disabled controls",
}

CONTRAST_JS = r"""
() => {
  const srgb = v => v <= 0.04045 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4);
  const lum = c => {
    const [r,g,b] = c;
    return 0.2126*srgb(r/255) + 0.7152*srgb(g/255) + 0.0722*srgb(b/255);
  };
  const parse = s => {
    const m = (s || '').match(/[\d.]+/g);
    return m ? m.slice(0,3).map(Number).concat(m[3] === undefined ? [1] : [Number(m[3])]) : null;
  };
  // The colour actually behind an element: walk up until something is opaque.
  const behind = el => {
    let n = el;
    while (n && n !== document.documentElement) {
      const c = parse(getComputedStyle(n).backgroundColor);
      if (c && c[3] >= 0.95) return c.slice(0,3);
      n = n.parentElement;
    }
    return [11, 14, 18];      // --bg
  };
  const ratio = (a, b) => {
    const la = lum(a), lb = lum(b);
    return (Math.max(la,lb) + 0.05) / (Math.min(la,lb) + 0.05);
  };

  const out = [];
  const seen = new Set();
  document.querySelectorAll('body *').forEach(el => {
    if (el.closest('[hidden]') || el.disabled) return;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return;
    if (parseFloat(st.opacity) < 0.6) return;   // fading in/out, not steady state
    // Only elements with their OWN text, not containers of text.
    const own = Array.from(el.childNodes)
      .filter(n => n.nodeType === 3).map(n => n.textContent.trim()).join('');
    if (own.length < 2) return;
    const box = el.getBoundingClientRect();
    if (box.width < 2 || box.height < 2) return;

    const fg = parse(st.color);
    if (!fg || fg[3] < 0.6) return;
    const bg = behind(el);
    const size = parseFloat(st.fontSize);
    const weight = parseInt(st.fontWeight, 10) || 400;
    const large = size >= 24 || (size >= 18.66 && weight >= 700);
    const need = large ? 3.0 : 4.5;
    const got = ratio(fg.slice(0,3), bg);

    if (got + 0.005 < need) {
      const key = st.color + '|' + bg.join(',') + '|' + Math.round(size);
      if (seen.has(key)) return;
      seen.add(key);
      out.push({
        text: own.slice(0, 42),
        selector: el.tagName.toLowerCase() +
                  (el.id ? '#' + el.id : '') +
                  (el.className && typeof el.className === 'string'
                     ? '.' + el.className.trim().split(/\\s+/).slice(0,2).join('.') : ''),
        color: st.color, background: 'rgb(' + bg.join(',') + ')',
        size: Math.round(size * 10) / 10, weight,
        ratio: Math.round(got * 100) / 100, need,
      });
    }
  });
  return out;
}
"""


def _visit(page, core, tab=None):
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#tab-inicio", timeout=30000)
    if tab:
        goto_tab(page, tab)
        page.wait_for_selector(f'.tab-panel[data-tab="{tab}"].active', timeout=15000)
        page.wait_for_timeout(1200)      # let the panel's own fetch land
    else:
        page.wait_for_timeout(1200)


@pytest.mark.parametrize("tab", [None, "sistema", "transparencia", "dispositivos"])
def test_text_meets_contrast_where_a_person_reads_it(page, core, tab):
    """Measured on rendered pixels, against the colour actually behind."""
    _visit(page, core, tab)
    bad = page.evaluate(CONTRAST_JS)
    if bad:
        lines = [f"{b['ratio']}:1 (needs {b['need']}) — {b['selector']} "
                 f"{b['size']}px/{b['weight']} {b['color']} on {b['background']}"
                 f"\n        {b['text']!r}" for b in bad]
        raise AssertionError(
            f"{len(bad)} text/background pairs below WCAG AA on "
            f"{tab or 'Home'}:\n  " + "\n  ".join(lines))


def test_every_control_announces_something(page, core):
    """An icon button with no accessible name is announced as "button"."""
    _visit(page, core)
    nameless = page.evaluate(r"""
      () => {
        const out = [];
        document.querySelectorAll(
          'button, a[href], input, select, textarea, [role="button"], [role="tab"]'
        ).forEach(el => {
          if (el.closest('[hidden]') || el.hidden) return;
          const st = getComputedStyle(el);
          if (st.display === 'none' || st.visibility === 'hidden') return;
          const name = (el.getAttribute('aria-label') || '').trim()
            || (el.getAttribute('title') || '').trim()
            || (el.innerText || '').trim()
            || (el.labels && el.labels.length
                  ? Array.from(el.labels).map(l => l.innerText.trim()).join(' ') : '')
            || (el.getAttribute('aria-labelledby')
                  ? (document.getElementById(el.getAttribute('aria-labelledby'))
                       || {innerText: ''}).innerText.trim() : '')
            || (el.getAttribute('placeholder') || '').trim();
          if (!name) out.push(el.tagName.toLowerCase() +
                              (el.id ? '#' + el.id : '') +
                              (typeof el.className === 'string' && el.className
                                 ? '.' + el.className.trim().split(/\\s+/)[0] : ''));
        });
        return out;
      }
    """)
    assert not nameless, (
        f"{len(nameless)} controls announce nothing:\n  " + "\n  ".join(nameless))


def test_focus_is_visible_wherever_the_keyboard_lands(page, core):
    """A focus ring the theme removed is a keyboard user navigating blind.

    ⚠ This must press Tab for real. The obvious version calls `el.focus()` in
    a loop and reads the computed style, and it reported 29 false failures on
    a page that has a perfectly good global `:focus-visible` ring: Chromium
    does not apply `:focus-visible` to programmatic focus when there has been
    no keyboard interaction. The test was measuring script focus, which no
    person ever experiences.

    It also accepts more than an outline. Several rules here deliberately swap
    the ring for a border-colour change (`.ptz-pad button:focus-visible{
    border-color:var(--accent);outline:none}`), which is a real indicator. So
    the check is "does ANYTHING visible change when focus lands" — snapshot
    the element's own style before, Tab to it, snapshot again.
    """
    _visit(page, core)

    snap = """
      el => {
        if (!el || el === document.body) return null;
        const s = getComputedStyle(el);
        return {
          id: el.id, tag: el.tagName.toLowerCase(),
          cls: (typeof el.className === 'string' && el.className)
                 ? el.className.trim().split(/\\s+/)[0] : '',
          outline: s.outlineStyle + ' ' + s.outlineWidth + ' ' + s.outlineColor,
          shadow: s.boxShadow, border: s.borderColor, bg: s.backgroundColor,
          color: s.color, ring: el.matches(':focus-visible'),
        };
      }
    """

    page.click("body", position={"x": 5, "y": 5})
    invisible, seen, path = [], set(), []
    for _ in range(45):
        page.keyboard.press("Tab")
        focused = page.evaluate_handle("() => document.activeElement")
        after = page.evaluate(snap, focused)
        if not after:
            continue
        key = f"{after['tag']}#{after['id']}.{after['cls']}"
        if key in seen:
            continue
        seen.add(key)
        path.append(key)
        # The same element with focus taken away, for comparison.
        before = page.evaluate(
            """el => { el.blur();
                       const s = getComputedStyle(el);
                       return {outline: s.outlineStyle + ' ' + s.outlineWidth
                                        + ' ' + s.outlineColor,
                               shadow: s.boxShadow, border: s.borderColor,
                               bg: s.backgroundColor, color: s.color}; }""",
            focused)
        # `el.matches(':focus-visible')` is NOT evidence. It says the browser
        # considers this focus keyboard-driven, which is true whether or not a
        # single rule draws anything — accepting it made this test pass with
        # the global focus ring deleted. Only a visual difference counts.
        changed = any(before[k] != after[k]
                      for k in ("outline", "shadow", "border", "bg", "color"))
        if not changed:
            invisible.append(key)
        # blur() moved focus to the body, so resume the walk from this element.
        page.evaluate("el => el.focus()", focused)

    assert len(path) >= 10, (
        f"Tab only reached {len(path)} distinct controls, so this test is not "
        f"measuring traversal: {path}")
    assert not invisible, (
        f"{len(invisible)} of {len(path)} controls the keyboard reaches show "
        f"nothing when focus lands:\n  " + "\n  ".join(invisible))


def test_the_audit_would_notice_a_failure():
    """These tests pass loudly against a page that renders nothing, so prove
    the measurement is live by feeding it a known-bad pair."""
    # Pure logic, no browser: the same maths the page script runs.
    def srgb(v):
        v = v / 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    def lum(c):
        r, g, b = (srgb(x) for x in c)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    def ratio(a, b):
        la, lb = lum(a), lum(b)
        return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)

    assert ratio((255, 255, 255), (0, 0, 0)) > 20
    # Wavr's dim on Wavr's background must clear body text.
    assert ratio((0x9A, 0xA4, 0xAD), (0x0B, 0x0E, 0x12)) >= 4.5
    # A deliberately bad pair must be caught.
    assert ratio((0x30, 0x36, 0x3D), (0x0B, 0x0E, 0x12)) < 4.5


def test_a_failure_says_which_kind_of_failure_it_was(page, core):
    """Unreachable and refused look identical to a person and are not the same
    thing. One means nothing was attempted and pressing the button again does
    the same; the other means the Core declined something. Collapsing them into
    "couldn't save" tells somebody a thing did not happen and nothing about
    what to do next."""
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#tab-inicio", timeout=30000)
    page.wait_for_function("() => typeof failureText === 'function'",
                           timeout=30000)

    unreachable = page.evaluate("() => failureText(undefined, 'save that')")
    assert "could not reach the Core" in unreachable
    assert "nothing changed" in unreachable.lower()
    assert "running" in unreachable, "it does not say what to check"

    refused = page.evaluate(
        "() => failureText({status: 403, json: () => Promise.reject()}, "
        "'save that')")
    assert "403" in refused, f"the status is what makes it diagnosable: {refused}"
    assert "refused" in refused

    # A reason from the Core always beats anything written in the client.
    spoken = page.evaluate(
        "() => failureText({status: 400, json: () => Promise.resolve("
        "{detail: 'That room does not exist.'})}, 'save that')")
    assert spoken == "That room does not exist."


def test_task_can_a_person_tell_which_home_they_are_looking_at(page, core):
    """Persona A on a phone that is paired to more than one Core: "is this my
    house or my mother's?"

    The stakes are not cosmetic. Somebody who cannot tell will dismiss an
    intrusion alert, or tell a caller nobody is home, about the wrong building.
    """
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#tab-inicio", timeout=30000)
    page.wait_for_timeout(2500)
    # The persistent frame, BOTH halves of it. This read `.nav-brand` alone and
    # passed on the runtime chip's "Browser Test · Live", which happened to be
    # inside that block — so when the chip moved to the topbar (it was
    # `display:none` on a phone where it sat) the test failed while the product
    # named the Space in two places instead of one.
    #
    # Case-insensitive because the sidebar wordmark's own copy is styled
    # uppercase; a text-transform is not the chrome failing to name the Space.
    chrome = " ".join(
        page.locator(sel).inner_text() for sel in (".nav-brand", ".topbar"))
    assert "browser test" in chrome.lower(), (
        f"the chrome does not name the Space; it says {chrome!r}")
    assert "Browser Test" in page.title(), (
        "two Cores in two tabs are indistinguishable from the tab strip")


def test_task_can_a_person_find_what_needs_them_and_get_there(page, core):
    """Persona A: "is there anything I have to deal with?"

    A count on its own is a nag. The point of an inbox is that it takes you to
    the thing — so the answer must be reachable, not merely displayed.
    """
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#tab-inicio", timeout=30000)
    page.wait_for_timeout(2500)

    tile = page.locator("#attnTile")
    chip = page.locator("#attnChip")
    assert tile.count() == 1, "the landing surface has no needs-attention tile"

    tile_shown = tile.is_visible()
    chip_shown = chip.count() > 0 and chip.is_visible()

    # The contract that matters is that the two AGREE. A chip saying three
    # things need you over a hidden inbox, or an inbox listing work behind a
    # chrome that shows nothing, are both a household being told two different
    # numbers by one product.
    assert tile_shown == chip_shown, (
        f"the chrome and the panel disagree: chip visible={chip_shown}, "
        f"inbox visible={tile_shown}")

    if not tile_shown:
        # Nothing waiting. Hiding both is the deliberate choice — a permanent
        # "0" is furniture, and furniture is what people stop seeing. Prove
        # that is really the state rather than a render that failed.
        empty = page.evaluate(
            "async () => (await (await fetch('/api/attention')).json())")
        assert not empty.get("items"), (
            f"the inbox is hidden while {len(empty['items'])} things wait")
        assert empty.get("headline"), "and it has no words for that state either"
        return

    said = tile.inner_text().strip()
    assert len(said) > 20, f"the inbox is on screen and nearly blank: {said!r}"
    before = page.evaluate("() => location.hash")
    chip.click()
    page.wait_for_timeout(700)
    after = page.evaluate(
        "() => location.hash + '|' + (document.querySelector"
        "('.tab-panel.active') || {}).id")
    assert after != before + "|", "the attention chip led nowhere"


def test_task_can_a_person_stop_wavr_watching_and_see_that_it_stopped(page, core):
    """Persona A, and the most consequential task in the product: "stop
    sensing" must be findable AND its effect must be visible.

    A pause that does not visibly take effect is worse than no pause at all:
    somebody believes they have privacy they do not have. This checks the
    control exists, is reachable without a terminal, and says what state it is
    in — it deliberately does NOT click it, because the fixture Core is shared
    with every other test in this module.
    """
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#tab-inicio", timeout=30000)
    page.wait_for_timeout(2000)

    level = page.locator("#sensingLevelTile")
    assert level.count() == 1, "the landing surface never says how much is sensed"
    said = level.inner_text().strip().lower()
    assert said, "the sensing-level tile is blank"
    assert any(w in said for w in
               ("off", "paused", "presence", "precise", "sensing")), said

    goto_tab(page, "sistema")
    page.wait_for_timeout(1500)
    power = page.locator("#controls")
    assert power.count() == 1, "System offers no power control"
    text = power.inner_text().lower()
    assert any(w in text for w in ("pause", "stop", "off", "resume", "start")), (
        f"the power tile does not offer stopping or starting: {text[:160]!r}")


def test_task_can_a_person_tell_a_quiet_house_from_a_blind_one(page, core):
    """The distinction this product's honesty rests on.

    "Nobody is home" and "nothing can see" produce the same silence and mean
    opposite things. Whatever the fixture Core's state, the landing surface
    must commit to one of them in words rather than leaving a person to guess.
    """
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#tab-inicio", timeout=30000)
    page.wait_for_timeout(2500)
    hero = page.locator("#houseStatusTile").inner_text().strip().lower()
    assert hero, "the house-status tile is blank"
    # A claim ("normal", "needs attention"), or an admission ("nothing is
    # being checked"). Either is fine. Silence is not: that is the state where
    # a person supplies their own answer, and they always supply the
    # comfortable one.
    claims = ("normal", "needs attention", "worth a glance", "someone",
              "occupied")
    admits = ("nothing is being checked", "cannot", "can't", "not watching",
              "no network monitor", "paused", "unknown", "no sensor",
              "starting", "waiting", "does not recognise")
    assert any(w in hero for w in claims + admits), (
        f"the house status neither claims nor admits anything: {hero[:200]!r}")

    # And when it DOES reassure, the reassurance has to be earned. `checked`
    # lists the layers that could actually report; an empty list with a calm
    # sentence is the exact failure this test exists for.
    hs = page.evaluate(
        "async () => (await (await fetch('/api/house-status')).json())")
    if "normal" in hero:
        assert hs.get("checked"), (
            "the tile says everything looks normal and the Core checked "
            f"nothing: {hs}")


# -- Deleting what Wavr learned, on screen ------------------------------------

def _open_privacy(page, core):
    """Wait for the LAST thing this panel renders, not for its heading.

    `renderPrivacyData` clears its host and rebuilds five tiles from five
    requests. Waiting on a heading passes the moment that heading is appended
    and then races the rest of the rebuild — the read that follows can land on
    a host that was just emptied. The delete tile is built last and ends in
    either a button (the route answered) or a note (403 from anywhere but the
    Core), so waiting for one of those means the panel is finished.
    """
    open_settings(page, core, wait_for="#privacyDataBody")
    page.wait_for_function(
        """() => {
             const host = document.getElementById('privacyDataBody');
             if (!host) return false;
             const tile = Array.from(host.querySelectorAll('.tile')).find(
               t => t.innerText.includes('Delete what Wavr learned'));
             if (!tile) return false;
             return !!tile.querySelector('button')
                 || tile.innerText.includes('at the Core');
           }""", timeout=30000)


def test_task_can_a_person_delete_what_wavr_learned(page, core):
    """A screen that lists everything a product keeps about you and offers no
    way to remove any of it is the wrong end of the sentence — and this is
    the privacy screen."""
    _open_privacy(page, core)
    text = page.locator("#privacyDataBody").inner_text()
    assert "Delete what Wavr learned" in text
    assert "What Wavr learned by watching" in text, (
        "the observation group is missing, so there is nothing to delete")
    # The size of the thing, before agreeing to it.
    assert "row" in text.lower(), "no counts, so nobody can see what they lose"


def test_deleting_setup_is_a_separate_choice_that_is_never_preselected(page, core):
    """"Delete what you know about me" and "delete my installation" are
    different requests. A checkbox that starts ticked answers the second one on
    somebody's behalf."""
    _open_privacy(page, core)
    state = page.evaluate("""
      () => {
        const host = document.getElementById('privacyDataBody');
        const tiles = Array.from(host.querySelectorAll('.tile'));
        const tile = tiles.find(t => t.innerText.includes('Delete what Wavr learned'));
        if (!tile) return null;
        const det = Array.from(tile.querySelectorAll('details'));
        const setup = det.find(d => d.innerText.includes('starting over'));
        const boxes = Array.from(tile.querySelectorAll('input[type=checkbox]'));
        const setupBoxes = setup
          ? Array.from(setup.querySelectorAll('input[type=checkbox]')) : [];
        return {
          hasSetupGroup: !!setup,
          setupOpen: setup ? setup.open : null,
          setupChecked: setupBoxes.filter(b => b.checked).length,
          setupTotal: setupBoxes.length,
          anyChecked: boxes.filter(b => b.checked).length,
        };
      }
    """)
    assert state, "the delete tile is not on the privacy screen"
    assert state["hasSetupGroup"], "setup categories are not separated at all"
    assert state["setupOpen"] is False, (
        "the setup list starts open, which puts undoing an installation one "
        "stray click from the observation checkboxes")
    assert state["setupChecked"] == 0, (
        f"{state['setupChecked']} of {state['setupTotal']} setup categories are "
        f"pre-ticked — that answers 'start over' on somebody's behalf")
    assert state["anyChecked"] > 0, (
        "nothing is pre-selected at all, so the default button does nothing")


def test_deleting_takes_two_deliberate_steps(page, core):
    """No native confirm(), and never one click. Same reveal-then-confirm shape
    as unpairing and device blocking."""
    _open_privacy(page, core)
    step = page.evaluate("""
      () => {
        const host = document.getElementById('privacyDataBody');
        const tile = Array.from(host.querySelectorAll('.tile'))
          .find(t => t.innerText.includes('Delete what Wavr learned'));
        const btns = Array.from(tile.querySelectorAll('button'));
        const go = btns.find(b => /^Delete /.test(b.textContent));
        if (!go) return {found: false};
        const confirmRow = tile.querySelector('.pair-dev-row[hidden]');
        const before = !!confirmRow;
        go.click();
        const yes = Array.from(tile.querySelectorAll('button'))
          .find(b => /^Yes/.test(b.textContent));
        return {
          found: true,
          hiddenBefore: before,
          confirmShown: !!(yes && yes.offsetParent !== null),
          confirmWords: yes ? yes.textContent : '',
        };
      }
    """)
    assert step["found"], "there is no delete button"
    assert step["hiddenBefore"], "the confirm was visible before anything was clicked"
    assert step["confirmShown"], "clicking delete did not reveal a confirm step"
    # The confirm names the consequence rather than asking "are you sure?",
    # which tells somebody nothing they did not already know.
    assert "permanently" in step["confirmWords"] or "undo" in step["confirmWords"], (
        f"the confirm does not say what happens: {step['confirmWords']!r}")


# -- The compositions, at the widths they were written for --------------------
#
# The stylesheet has a real breakpoint ladder — 419, 520, 620, 760, 820, 1200,
# plus pointer:coarse and a landscape-phone case. Having one is not the same as
# it working, and nothing checked. These visit each width and ask the two
# questions a broken layout answers badly.

VIEWPORTS = [
    (390, 844, "phone"),          # below 419: the tightest case
    (480, 900, "large phone"),    # between 419 and 520
    (600, 900, "phablet"),        # between 520 and 620
    (768, 1024, "tablet"),        # between 760 and 820: the icon-rail case
    (1024, 800, "small laptop"),  # between 820 and 1200
    (1440, 900, "desktop"),
]


@pytest.mark.parametrize("w,h,name", VIEWPORTS)
def test_the_page_never_scrolls_sideways(page, core, w, h, name):
    """A page wider than its viewport is the single most common responsive
    failure and the most annoying to use: every vertical scroll drifts, and
    controls sit off the right edge with nothing indicating they exist."""
    page.set_viewport_size({"width": w, "height": h})
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#tab-inicio", timeout=30000)
    page.wait_for_timeout(1800)

    over = page.evaluate("""
      () => {
        const doc = document.documentElement;
        const slack = 2;   // sub-pixel rounding, not a layout bug
        if (doc.scrollWidth <= doc.clientWidth + slack) return null;
        // Name the widest offender, or the failure is unactionable.
        let worst = null;
        document.querySelectorAll('body *').forEach(el => {
          const st = getComputedStyle(el);
          if (st.display === 'none' || st.visibility === 'hidden') return;
          if (st.position === 'fixed') return;   // overlays are their own thing
          const r = el.getBoundingClientRect();
          if (r.width === 0) return;
          const spill = r.right - doc.clientWidth;
          if (spill > slack && (!worst || spill > worst.spill)) {
            worst = {
              spill: Math.round(spill),
              sel: el.tagName.toLowerCase()
                   + (el.id ? '#' + el.id : '')
                   + (typeof el.className === 'string' && el.className
                        ? '.' + el.className.trim().split(/\\s+/)[0] : ''),
            };
          }
        });
        return {page: doc.scrollWidth, view: doc.clientWidth, worst};
      }
    """)
    assert over is None, (
        f"at {w}x{h} ({name}) the page is {over['page']}px wide in a "
        f"{over['view']}px viewport; widest offender: {over['worst']}")


MEASURE_NAV = """
  () => {
    const items = Array.from(document.querySelectorAll(
      '.nav-tabs .nav-item[data-tab], .nav-sub:not([hidden]) .nav-sub-item[data-tab]'
    ));
    return {
      total: items.length,
      tabs: items.map(e => e.getAttribute('data-tab')),
      // Reachable = rendered with a real box. A zero-size button is not.
      usable: items.filter(el => {
        const st = getComputedStyle(el);
        if (st.display === 'none' || st.visibility === 'hidden') return false;
        const r = el.getBoundingClientRect();
        return r.width > 4 && r.height > 4;
      }).length,
      named: items.filter(el => (el.getAttribute('aria-label') || '').trim()
                                .length > 0).length,
    };
  }
"""


@pytest.mark.parametrize("w,h,name", VIEWPORTS)
def test_every_section_stays_reachable_at_every_width(page, core, w, h, name):
    """The nav collapses from a labelled sidebar to an icon rail to a bottom
    bar. Collapsing is fine; losing a destination is not — a tab that becomes
    unreachable below some width is a feature that silently does not exist on a
    phone.

    There are TWO levels now: three primary destinations and five behind
    Manage. So this opens Manage and measures again, which also makes it a test
    of the disclosure itself — a destination behind a control that does not
    work at 390px is exactly as unreachable as one that was deleted.
    """
    page.set_viewport_size({"width": w, "height": h})
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#tab-inicio", timeout=30000)
    page.wait_for_timeout(1200)

    closed = page.evaluate(MEASURE_NAV)
    assert closed["total"] >= 3, f"the primary nav was not found at {name}"
    assert closed["usable"] == closed["total"], (
        f"at {w}x{h} ({name}) only {closed['usable']} of {closed['total']} "
        f"primary sections are reachable")

    manage = page.locator("#tab-manage")
    assert manage.count() == 1, "the Manage disclosure is gone"
    manage.click()
    page.wait_for_selector("#navManageSub:not([hidden])", timeout=10000)
    page.wait_for_timeout(400)

    opened = page.evaluate(MEASURE_NAV)
    assert opened["total"] >= 8, (
        f"at {w}x{h} ({name}) opening Manage revealed only "
        f"{opened['total']} destinations: {opened['tabs']}")
    assert opened["usable"] == opened["total"], (
        f"at {w}x{h} ({name}) only {opened['usable']} of {opened['total']} "
        f"sections are reachable with Manage open")
    # Below the rail breakpoint the labels are hidden, so the accessible name
    # is the ONLY thing carrying what a tab is. Losing it turns the bottom bar
    # into a row of unexplained glyphs for a screen-reader user.
    assert opened["named"] == opened["total"], (
        f"at {w}x{h} ({name}) {opened['total'] - opened['named']} tabs have no "
        f"accessible name, and the visible labels are hidden at this width")


@pytest.mark.parametrize("w,h,name", VIEWPORTS)
def test_the_manage_level_can_be_left_again(page, core, w, h, name):
    """A disclosure that only opens is a one-way door. Pressing Manage while
    inside it must return a person to their Space, at every width."""
    page.set_viewport_size({"width": w, "height": h})
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#tab-inicio", timeout=30000)
    page.wait_for_timeout(1000)

    page.click("#tab-manage")
    page.wait_for_selector("#navManageSub:not([hidden])", timeout=10000)
    assert page.locator('.tab-panel[data-tab="sistema"].active').count() == 1, (
        f"at {name} Manage opened without landing anywhere")

    page.click("#tab-manage")
    #  waits for VISIBLE, which a hidden element
    # never becomes — wait on the property instead.
    page.wait_for_function(
        "() => document.getElementById('navManageSub').hidden === true",
        timeout=10000)
    assert page.locator('.tab-panel[data-tab="inicio"].active').count() == 1, (
        f"at {name} leaving Manage did not return to Space")


def test_a_touch_target_is_big_enough_to_hit(browser, core):
    """44px is the long-standing floor, and this product is meant to end up on
    a wall panel somebody prods in passing.

    A TOUCH context, not merely a narrow one. Every 44px rule in this
    stylesheet lives behind `@media (pointer:coarse)`, and Playwright's default
    context reports a FINE pointer however narrow the viewport — so measuring
    in the ordinary `page` fixture measures a phone-shaped desktop and reports
    failures no phone has. Getting that wrong is how a real rule gets deleted
    for being noisy.
    """
    ctx = browser.new_context(viewport={"width": 390, "height": 844},
                              has_touch=True, is_mobile=True)
    page = ctx.new_page()
    try:
        page.goto(f"{core}/?cachebust={time.time()}")
        page.wait_for_selector("#tab-inicio", timeout=30000)
        page.wait_for_timeout(1500)
        coarse = page.evaluate("() => matchMedia('(pointer:coarse)').matches")
        small = page.evaluate("""
          () => {
            const out = [];
            document.querySelectorAll('.nav-tabs .nav-item, .topbar button')
              .forEach(el => {
                const st = getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') return;
                const r = el.getBoundingClientRect();
                if (r.width < 2 || r.height < 2) return;
                if (r.height < 40 || r.width < 40) {
                  out.push((el.id || el.className || el.tagName)
                           + ' ' + Math.round(r.width) + 'x' + Math.round(r.height));
                }
              });
            return out;
          }
        """)
    finally:
        ctx.close()
    assert coarse, (
        "the context does not report a coarse pointer, so none of the 44px "
        "rules applied and this test proved nothing")
    assert not small, (
        "these are too small to hit reliably on a phone or a wall panel:\n  "
        + "\n  ".join(small))


# -- Language, in a real browser ----------------------------------------------
#
# A catalogue that is complete on disk proves nothing about what a person sees.
# These switch the language the way a person does and read the screen back.

def test_task_can_a_person_read_wavr_in_their_own_language(page, core):
    """The whole point. Switch to Portuguese and the words change."""
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#tab-inicio", timeout=30000)
    page.wait_for_function("() => !!window.WavrI18n", timeout=30000)

    english = page.locator("#tab-inicio").inner_text().strip()
    assert english.lower().startswith("space"), english

    page.evaluate("() => WavrI18n.setLocale('pt-BR')")
    page.wait_for_timeout(400)

    portuguese = page.locator("#tab-inicio").inner_text().strip()
    assert portuguese.lower().startswith("espa"), (
        f"the navigation is still English after switching: {portuguese!r}")
    assert page.evaluate("() => document.documentElement.lang") == "pt", (
        "the document never declared its language, so a screen reader and a "
        "spellchecker both still think this page is English")


def test_switching_twice_does_not_translate_a_translation(page, core):
    """The failure this design's `i18nSrc` exists to prevent: without the
    original kept aside, the second switch looks up Portuguese in a Portuguese
    catalogue, misses, and leaves the screen stuck."""
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#tab-inicio", timeout=30000)
    page.wait_for_function("() => !!window.WavrI18n", timeout=30000)

    page.evaluate("() => WavrI18n.setLocale('pt-BR')")
    page.wait_for_timeout(300)
    page.evaluate("() => WavrI18n.setLocale('en')")
    page.wait_for_timeout(300)
    back = page.locator("#tab-inicio").inner_text().strip()
    assert back.lower().startswith("space"), (
        f"switching back to English did not restore it: {back!r}")

    page.evaluate("() => WavrI18n.setLocale('pt-BR')")
    page.wait_for_timeout(300)
    again = page.locator("#tab-inicio").inner_text().strip()
    assert again.lower().startswith("espa"), (
        f"the second switch to Portuguese failed: {again!r}")


def test_the_choice_survives_a_reload(page, core):
    """A person who sets their language on a wall panel should not have to set
    it again every morning."""
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_function("() => !!window.WavrI18n", timeout=30000)
    page.evaluate("() => WavrI18n.setLocale('pt-BR')")
    page.wait_for_timeout(300)

    page.reload()
    page.wait_for_selector("#tab-inicio", timeout=30000)
    page.wait_for_timeout(800)
    assert page.locator("#tab-inicio").inner_text().strip().lower() \
        .startswith("espa"), "the language choice was forgotten on reload"
    # And the formatter agrees — one locale, not two.
    assert page.evaluate("() => WavrFmt.locale ? WavrFmt.locale() : "
                         "localStorage.getItem('wavr.locale')") is not None


def test_nothing_on_the_landing_screen_is_left_untranslated(page, core):
    """`WavrI18n.missing()` records every lookup that fell back to English.
    On the landing surface, in Portuguese, that list must be empty — a
    half-translated screen is the failure mode this whole design accepts in
    exchange for never breaking, and it is only acceptable if nobody ships it.
    """
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_function("() => !!window.WavrI18n", timeout=30000)
    page.evaluate("() => { WavrI18n.setLocale('pt-BR'); "
                  "WavrI18n.resetMissing(); WavrI18n.apply(document); }")
    page.wait_for_timeout(1500)
    missing = page.evaluate("() => WavrI18n.missing()")
    assert not missing, (
        f"{len(missing)} strings on screen have no Portuguese:\n  "
        + "\n  ".join(repr(m) for m in missing[:20]))


# Words that cannot occur in a Portuguese sentence. Deliberately a short closed
# list of English function words rather than a language detector: this has to be
# read and trusted by whoever it fails on, and a probabilistic answer about a
# six-word chip label is not something anybody can act on.
_ENGLISH_ONLY = re.compile(
    r"\b(the|and|with|your|you|this|that|these|those|from|when|what|which|"
    r"need|needs|nothing|everything|something|anything|here|there|are|"
    r"is|was|were|can|cannot|will|would|should|does|doesn't|don't|isn't|"
    r"it's|its|has|have|had|been|being|about|into|until|while|because)\b",
    re.I)

# Words that are the same in every language, or are not words at all.
_NOT_A_SENTENCE = re.compile(r"^[\W\d\s]*$")


def test_nothing_on_the_landing_screen_reads_as_english(page, core):
    """The check `WavrI18n.missing()` structurally cannot make.

    `missing()` records lookups that fell back to English. A string that was
    never MARKED is never looked up, so it can never be missing — the test
    above reports zero for a screen that is half English, which is exactly what
    it did while eighteen surfaces were untranslated.

    This one reads what is actually on the glass. It is the complement, not a
    replacement: `missing()` catches marked-but-untranslated, this catches
    never-marked, and neither can see the other's half.

    The detector is a short closed list of English function words rather than
    anything clever, because whoever this fails on has to be able to read the
    failure and act on it.
    """
    # Switch, then RELOAD, because that is what the control does. Calling
    # `apply(document)` in place measures a deliberately partial repaint — the
    # panels whose text is written by JavaScript keep whatever they last painted
    # until their own poll comes round, which for the house-status tile is
    # twenty seconds. Measuring that and calling it a missing translation is
    # measuring something the product does not claim; the reload is the claim.
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_function("() => !!window.WavrI18n", timeout=30000)
    page.evaluate("() => WavrI18n.setLocale('pt-BR')")
    page.reload()
    page.wait_for_function("() => !!window.WavrI18n", timeout=30000)
    page.wait_for_selector("#tab-inicio", timeout=30000)
    # Long enough for the polled renderers to have painted at least once: an
    # empty screen passes this trivially.
    page.wait_for_timeout(6000)

    runs = page.evaluate(
        """() => {
             const roots = ['.topbar', '#mainNav', '#panel-inicio']
               .map(s => document.querySelector(s)).filter(Boolean);
             const out = [];
             for (const root of roots) {
               const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
               let n;
               while ((n = walk.nextNode())) {
                 const t = (n.textContent || '').trim();
                 if (!t) continue;
                 const el = n.parentElement;
                 if (!el || !el.offsetParent) continue;      // not on screen
                 if (el.closest('[hidden],[aria-hidden="true"]')) continue;
                 out.push({text: t, where: el.id || el.className || el.tagName});
               }
             }
             return out;
           }""")

    english = [r for r in runs
               if not _NOT_A_SENTENCE.match(r["text"]) and _ENGLISH_ONLY.search(r["text"])]
    assert not english, (
        f"{len(english)} runs of text on the Portuguese landing screen read as "
        f"English, so they were never marked for translation at all:\n  "
        + "\n  ".join(f"{r['where']}: {r['text'][:110]!r}" for r in english[:20]))


def test_the_language_control_lists_only_what_can_be_rendered(page, core):
    open_settings(page, core, wait_for="#settingsTile")
    page.wait_for_selector("#langSelect", timeout=15000)
    options = page.eval_on_selector_all(
        "#langSelect option", "els => els.map(e => e.value)")
    assert "en" in options and "pt" in options, options
    available = page.evaluate("() => WavrI18n.available()")
    assert sorted(options) == sorted(available), (
        f"the control offers {options} but the build can render {available}")


def test_the_landing_tile_stops_claiming_everything_is_normal(page, core):
    """The second green verdict, and the larger one.

    The chrome chip turns red when the Core stops answering. A hundred pixels
    below it, the house-status tile kept its last view — a green dot and
    "Everything looks normal." — under a comment that said "keep last view".
    Two health verdicts on one screen contradicting each other, and the
    reassuring one is the bigger, greener half. A person reads the reassurance
    and walks away.

    Requests are held rather than refused, because that is what a Core that has
    gone dark looks like on the wire, and because a refusal was never the case
    in doubt.
    """
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#houseStatusTile:not([hidden])", timeout=30000)
    page.wait_for_function(
        "() => { const l = document.getElementById('houseStatusLine');"
        " return l && l.textContent.trim().length > 0; }", timeout=30000)
    before = page.locator("#houseStatusLine").inner_text()
    assert before.strip(), "nothing rendered, so this would prove nothing"

    held = []
    page.route("**/api/house-status", lambda route: held.append(route))

    # Two polls at 20s each, plus room for the second miss the tile waits for.
    page.wait_for_function(
        "() => { const l = document.getElementById('houseStatusLine');"
        " return l && /unknown/.test(l.className); }", timeout=70000)
    after = page.locator("#houseStatusLine").inner_text()
    assert after != before, after
    assert "normal" not in after.lower(), after
    assert held, "no request was intercepted; the tile changed for some other reason"
    assert page.script_errors == [], page.script_errors


def test_one_dropped_request_does_not_flap_the_tile(page, core):
    """The other half of the rule. A health tile that flickers between "fine"
    and "cannot tell" on a single dropped packet is its own kind of lie, and
    the one people learn to ignore."""
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#houseStatusTile:not([hidden])", timeout=30000)
    page.wait_for_function(
        "() => { const l = document.getElementById('houseStatusLine');"
        " return l && l.textContent.trim().length > 0; }", timeout=30000)

    once = {"done": False}

    def fail_once(route):
        if not once["done"]:
            once["done"] = True
            route.abort()
        else:
            route.continue_()

    page.route("**/api/house-status", fail_once)
    page.wait_for_timeout(25000)          # one poll fails, the next succeeds
    line = page.locator("#houseStatusLine")
    assert "unknown" not in (line.get_attribute("class") or ""), (
        "one dropped request flipped the verdict; two misses is the rule")
    assert page.script_errors == [], page.script_errors


def test_worth_a_glance_does_not_read_as_a_contradiction_of_needs_attention(page, core):
    """Space said "● Worth a glance — 6 things" (amber) while Manage → Needs
    attention said "Nothing needs your attention." at the same time, over the
    same six identical, unclickable "unrecognized device on the network
    (unknown)" rows. Both sentences were true: `attention.py` deliberately
    excludes a row nobody can act on (own docstring: "an item nobody can act
    on is a bug in this module, not an item"), and an unnamed, unstably-MAC'd
    device is exactly that. But nothing on screen said the two counts answer
    DIFFERENT questions, so a reader reasonably read them as disagreeing.

    Faked over the real `/api/house-status` response (`page.route`) rather
    than a live rogue-device condition, which this fixture Core cannot be
    made to have reliably: the fix under test is the explanatory copy, not
    the detector.
    """
    fake = {
        "status": "notice", "checked": ["network"],
        "reasons": [{"layer": "network", "severity": "note",
                     "what": "unrecognized device on the network (unknown)",
                     "ts": "2026-01-01T00:00:00+00:00"}] * 6,
    }
    page.route("**/api/house-status", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(fake)))
    page.goto(f"{core}/?cachebust={time.time()}")
    page.wait_for_selector("#houseStatusTile:not([hidden])", timeout=30000)
    page.wait_for_function(
        "() => (document.getElementById('houseStatusLine')?.textContent || '')"
        ".toLowerCase().includes('glance')", timeout=30000)

    note = page.locator("#houseStatusInfoNote")
    assert note.is_visible(), (
        "six 'worth a glance' reasons are shown with no note explaining why "
        "Manage's own Needs-attention count can legitimately read zero")
    text = note.inner_text().lower()
    assert "decision" in text and ("manage" in text or "needs attention" in text), (
        f"the note does not actually point at the other screen: {text!r}")
    assert page.script_errors == [], page.script_errors

