"""Surfaces that must stop asserting health when the Core goes dark.

## Why holding a request, and not killing the Core

Killing the process is the easy case and it already worked everywhere: the OS
refuses the connection, `fetch` rejects in milliseconds, and every one of these
screens already had a catch. What none of them had was an answer for a host that
goes DARK — a power cut, a suspended laptop, a dropped Wi-Fi link. A dark host
does not refuse anything. It says nothing at all, the TCP connection sits there,
`await fetch(...)` never settles, the poll never reaches its render, and the last
good verdict stands for ever.

So every test here reproduces the wire, not the process: `page.route` with a
handler that is entered and never answers, and — for the live stream —
`page.route_web_socket` with a handler that leaves the socket OPEN and SILENT,
which is what a dying hub's socket actually looks like to a browser.

The bar each test sets is the same, and it is the product rule: within a bounded
time, the surface stops claiming things are fine. Not "goes red" — several of
these correctly resolve to "cannot tell", which is the honest verdict when
nothing is being measured. What none of them may do is keep the reassuring one.

## Skipped rather than failed when the browser is absent

Same contract as `test_browser_ui.py`, whose fixtures this file copies: a
developer without a downloaded browser should see the rest of the suite pass,
and CI installs the browser explicitly, so a skip there is a CI configuration
problem visible as a skip count rather than as a false green.
"""
from __future__ import annotations

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
    browser, and a browser needs a socket. Copied from `test_browser_ui.py`
    rather than imported, so that file stays owned by whoever owns it.
    """
    port = _free_port()
    db = tmp_path_factory.mktemp("darkcore") / "wavr.db"
    env = {
        **os.environ,
        "WAVR_DB": str(db),
        # CWD-relative by default, and this subprocess runs with cwd=backend/.
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

        # A Space, so the first-run wizard is not covering everything.
        import urllib.request
        req = urllib.request.Request(
            base + "/api/setup/create-space",
            data=json.dumps({"name": "Dark Core Test", "kind": "home",
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
    # A DESKTOP viewport, the same width `test_browser_ui.py` uses: the settings
    # rail is panel-width-only, so this is the width at which a section that
    # renders only from a rail click sits empty.
    ctx = browser.new_context(viewport={"width": 1400, "height": 1100})
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(f"PAGEERROR {e}"))
    pg.script_errors = errors
    yield pg
    ctx.close()


MANAGE_TABS = ("sistema", "dispositivos", "rede", "discoveries",
               "transparencia")


def goto_tab(page, name):
    """Reach a destination on EITHER navigation level (five screens live one
    disclosure down, behind Manage)."""
    if name in MANAGE_TABS:
        sub = page.locator("#navManageSub")
        if sub.count() and sub.is_hidden():
            page.click("#tab-manage")
            page.wait_for_selector("#navManageSub:not([hidden])", timeout=10000)
    page.click(f"#tab-{name}")
    page.wait_for_selector(f'.tab-panel[data-tab="{name}"].active', timeout=15000)


def fresh(core, *, query=""):
    """A cache-busted URL, so a service worker never serves yesterday's shell."""
    sep = "&" if query else ""
    return f"{core}/?{query}{sep}cachebust={time.time()}"


# -- The live stream: open, and silent -----------------------------------------

def test_a_silent_live_socket_stops_the_kiosk_saying_the_hub_is_up(page, core):
    """`Hub ✓` over a hub that stopped talking, for ever.

    The reconnection indicator hung entirely off `ws.onclose`, and a dark host
    never closes the socket — it just goes quiet with the connection still open.
    So no close event, no `setReconnecting`, no repaint: the presence verdicts
    froze at their last values and the kiosk went on painting a green `Hub ✓`
    beside them, indefinitely.

    A mocked WebSocket route with a handler that never speaks is exactly that
    wire: the socket opens, and nothing ever arrives on it.

    The wait is long because the watchdog's silence window is a minute — short
    enough to matter, long enough that an idle hub with nothing to report is not
    called dead. A minute plus one check interval is the worst case, and this
    allows twice that before failing.
    """
    seen = []
    page.route_web_socket(re.compile(r"/ws/live"), lambda ws: seen.append(ws))
    page.goto(fresh(core, query="core"))
    page.wait_for_selector("#coreHealthCore", timeout=30000)
    # The socket is up. Nothing on this screen may say the hub is unreachable
    # yet — that is the OTHER failure, and it would make the assertion below
    # meaningless.
    assert "core-health-warn" not in (
        page.locator("#coreHealthCore").get_attribute("class") or "")

    page.wait_for_function(
        "() => (document.getElementById('coreHealthCore')?.textContent || '')"
        ".includes('\\u26a0')", timeout=150000)
    assert seen, (
        "no WebSocket was intercepted, so this proved nothing — the pill may "
        "have changed for an unrelated reason")
    # And the dashboard's own presence pill is dimmed by the same call, so the
    # ambient face and the dashboard cannot disagree about one hub.
    assert page.evaluate(
        "() => !!document.getElementById('house')?.classList.contains('stale')")


def test_the_room_cards_stop_reading_as_live_over_a_silent_socket(page, core):
    """The cards are the screen, and they were the part that said nothing.

    The test above proves the kiosk's hub pill drops its tick, and that the
    dashboard's house pill dims with it. Neither of those is what somebody is
    reading. Underneath them sits a grid of room cards, each with a confidence
    ring and a word, and they kept the last frame's verdict with no mark at all
    — "Occupied · 92%", indefinitely, from a reading that arrived before the
    hub went quiet. A small dimmed pill above a page of confident cards is not
    a warning; it is a footnote.

    Nothing here changes a verdict. A client that decided a room was empty
    because the stream went quiet would be inventing one, which is the failure
    on the other side of this. What the screen owes is to say that what it is
    showing is the last thing that arrived.

    Same wire and same wait as the test above: a socket that opens and never
    speaks, and the watchdog's one-minute silence window plus a check interval.
    """
    seen = []
    page.route_web_socket(re.compile(r"/ws/live"), lambda ws: seen.append(ws))
    page.goto(fresh(core))
    page.wait_for_selector("#rooms", timeout=30000)
    # Not already marked: otherwise the assertion below proves nothing.
    assert page.evaluate(
        "() => !document.getElementById('rooms').classList.contains('stale')")
    assert page.locator("#roomsStale").is_hidden()

    page.wait_for_function(
        "() => document.getElementById('rooms')"
        ".classList.contains('stale')", timeout=150000)
    assert seen, (
        "no WebSocket was intercepted, so this proved nothing — the class may "
        "have appeared for an unrelated reason")
    # And in words, not only in opacity. A dimmed card is a hint; somebody
    # reading the screen at a glance needs to be told which of the two things
    # dimming can mean this is.
    note = page.locator("#roomsStale")
    assert note.is_visible(), (
        "the cards dimmed and nothing said why, so the screen is ambiguous "
        "between 'nobody is home' and 'nothing has been heard'")
    # The map beside them paints presence from the same frames, so it goes with
    # them. One half of a screen admitting the problem while the other half
    # does not is worse than neither admitting it.
    assert page.evaluate(
        "() => !!document.getElementById('radarWrap')"
        "?.classList.contains('stale')"), (
        "the map kept its presence colours while the cards beside it dimmed")
    assert note.inner_text().strip(), "the note is on screen and empty"
    assert page.script_errors == [], page.script_errors


def test_the_kiosk_starts_the_hub_pill_unknown_not_healthy(page, core):
    """`Hub ✓` was painted at parse time, before the socket existed.

    `renderCoreHealth()` runs while the script is still executing, from a flag
    that started at `false` meaning "not down" — so the first thing a kiosk ever
    drew was a green tick asserting a connection that had not been attempted. On
    a Core that never comes up, it is the only thing it ever draws. The Internet
    pill beside it has always started at `…` for precisely this reason.

    Nothing arrives here: the socket is mocked silent and the history read is
    answered empty, so the page genuinely knows nothing about the hub.
    """
    page.route_web_socket(re.compile(r"/ws/live"), lambda ws: None)
    page.route(re.compile(r"/api/history"),
               lambda route: route.fulfill(status=200,
                                           content_type="application/json",
                                           body="[]"))
    page.goto(fresh(core, query="core"))
    page.wait_for_selector("#coreHealthCore", timeout=30000)
    page.wait_for_timeout(4000)          # well past parse and the boot paint
    text = page.locator("#coreHealthCore").inner_text()
    assert "✓" not in text, (
        f"the kiosk claimed the hub was up before hearing from it: {text!r}")
    assert "…" in text, (
        f"expected the unknown state the Internet pill already uses: {text!r}")


# -- The Privacy & security screen ---------------------------------------------

def test_the_privacy_screen_stops_claiming_sensing_is_on(page, core):
    """The worst instance of the class, because of what it is a claim ABOUT.

    "Sensing is on · Wavr is actively watching for presence." is the sentence
    somebody opens this screen to read. It sat behind a catch that returned with
    the comment "keep last view", so a Core that stopped answering left the
    reassurance standing — and a dark Core never reached the catch at all,
    because the request never settled.
    """
    page.goto(fresh(core))
    goto_tab(page, "transparencia")
    page.wait_for_function(
        "() => (document.getElementById('transSensing')?.textContent || '')"
        ".trim().length > 0", timeout=30000)
    before = page.locator("#transSensing").inner_text()
    assert "sensing" in before.lower(), before

    held = []
    page.route(re.compile(r"/api/transparency"), lambda route: held.append(route))

    # An 8s deadline on a 20s poll: the second miss lands around half a minute.
    page.wait_for_function(
        "() => { const b = document.getElementById('transBody');"
        "        const n = document.getElementById('transNote');"
        "        return b && n && b.hidden && !n.hidden"
        "               && n.textContent.trim().length > 0; }", timeout=90000)
    note = page.locator("#transNote").inner_text()
    assert "not answering" in note.lower(), note
    assert page.locator("#transSensing").is_hidden(), (
        "the sensing claim is still on screen over a Core that is not answering")
    assert held, "no request was intercepted; the screen changed for another reason"


# -- The Trust screen: every section, or an empty page -------------------------

def test_the_trust_screen_says_it_cannot_check_rather_than_showing_nothing(
        page, core):
    """Seven reads, seven `catch (e) { return; }`, and no deadline on any.

    So a Core that REFUSED the connection made every section vanish without a
    word, and a Core that goes DARK never settled the requests at all, so even
    those `return`s were unreachable and the page stayed empty for ever.

    Blank is the worst answer this particular screen can give. It is the one
    that answers "what does Wavr know about me": coverage, what each sensor has
    earned, what is kept, what can be connected to, what applications can read.
    A section that draws nothing reads as "nothing to declare", which is the
    reassuring answer, produced by not asking.

    The deadline is what makes this testable at all: `WavrAPI.json` dropped
    `timeoutMs` on the floor, so before the fix this held request would simply
    never end.
    """
    held = []
    for path in (r"/api/coverage", r"/api/reliability", r"/api/topology",
                 r"/api/privacy/", r"/api/experience/grants"):
        page.route(re.compile(path), lambda route: held.append(route))

    page.goto(fresh(core))
    page.wait_for_selector("#tab-inicio", timeout=30000)
    page.click("#gearNavBtn")
    page.wait_for_selector("#gearOverlay:not([hidden])", timeout=15000)
    # No rail click. The rail is panel-width-only, and at this viewport it is
    # `display:none` — `openGear` calls `__wavrRenderTrust()` itself for exactly
    # that reason (see shell-nav.js), because a screen that works on a phone and
    # not on a laptop is the failure that comment was written about.

    # Wait for the SENTENCE, not for content: the section headings are written
    # before the first read is awaited, so "the panel has text" is satisfied
    # immediately by a heading and proves nothing. 8s deadline per read, seven
    # of them, awaited one after another.
    page.wait_for_function(
        "() => (document.getElementById('trustBody')?.innerText || '')"
        ".toLowerCase().includes('cannot be read right now')", timeout=120000)
    said = page.locator("#trustBody").inner_text()
    assert held, "no request was intercepted; the page filled for another reason"
    assert "cannot be read right now" in said.lower(), (
        f"the Trust screen rendered without saying it could not ask: {said!r}")
    # And it must name the Core, not leave somebody guessing which of the many
    # things on this screen failed.
    assert "not answering" in said.lower(), said
    assert page.script_errors == [], page.script_errors


# -- The Status tile, and the pill it feeds ------------------------------------

_HEALTHY_STATUS = json.dumps({
    "version": "9.9.9",
    "space": {"name": "Dark Core Test"},
    "internet": {"ok": True, "since": None},
    "features": {"internet_monitor": True, "multidevice": False},
    "sources": [{"name": "network", "active": True}],
    "house": {"floors": 1, "rooms": 1},
})


def test_the_status_tile_stops_showing_internet_ok_over_a_dark_core(page, core):
    """`NO_ANSWER` reached every hook consumer and never the tile's own DOM.

    The payload was handed to `window.__wavrStatus` and the function returned,
    so the Core Panel's internet pill correctly fell back to "…" while the tile
    those element ids belong to kept its last paint — a green "Internet: OK", a
    live sensor row and a version. One Core, two screens, two verdicts, and the
    reassuring one was the one with the detail on it.

    A healthy answer is stubbed first because this test Core has no internet
    monitor running, so the green state a real home shows has to be created
    before it can be left standing.
    """
    state = {"dark": False}
    held = []

    def status(route):
        if state["dark"]:
            held.append(route)          # entered, never answered: a dark host
            return
        route.fulfill(status=200, content_type="application/json",
                      body=_HEALTHY_STATUS)

    page.route(re.compile(r"/api/status(\?|$)"), status)
    page.goto(fresh(core))
    goto_tab(page, "sistema")
    page.wait_for_function(
        "() => (document.getElementById('statusInternet')?.textContent || '')"
        ".includes('Internet: OK')", timeout=30000)

    state["dark"] = True
    # 3s poll, 8s deadline.
    page.wait_for_function(
        "() => !(document.getElementById('statusInternet')?.textContent || '')"
        ".includes('Internet: OK')", timeout=60000)
    assert held, "no request was intercepted; the tile changed for another reason"
    tile = page.locator("#statusPanel").inner_text()
    assert "not answering" in tile.lower(), tile
    assert "9.9.9" not in tile, (
        "the tile is still reporting a version it can no longer read: " + tile)


def test_the_mobile_companions_status_read_carries_a_deadline_too(page, core):
    """The phone took the branch the deadline was not on.

    The web path reads `/api/status` through `WavrAPI.fetch` with an 8s
    deadline. The mobile companion reads it through the native pinned fetch,
    which is a different function, so the deadline stopped at the branch and the
    phone kept the pre-fix behaviour in full. A bridge that never settles
    freezes this poll exactly the way a dark host freezes the browser one — and
    this poll is the producer for the pill on somebody's wall.

    The shim here is the smallest thing that makes the page take that branch:
    the members the shell actually reads, and a `netFetch` that can be told to
    stop settling for one route.
    """
    page.add_init_script("""
      window.__wavrDarkStatus = false;
      window.WAVR_MOBILE = {
        mode: "live",
        base: location.origin,
        ready: Promise.resolve(),
        role: null,
        tokenGet: function(){ return null; },
        tokenSet: function(){},
        netFetch: function(url, opt){
          if (window.__wavrDarkStatus && String(url).indexOf("/api/status") !== -1) {
            return new Promise(function(){});   // a bridge that never settles
          }
          return fetch(url, opt);
        },
        netWebSocket: function(url){ return new WebSocket(url); }
      };
    """)
    page.goto(fresh(core))
    goto_tab(page, "sistema")
    page.wait_for_function(
        "() => /version/i.test(document.getElementById('statusVersion')"
        "?.textContent || '')", timeout=30000)

    page.evaluate("() => { window.__wavrDarkStatus = true; }")
    page.wait_for_function(
        "() => /not answering/i.test(document.getElementById('statusVersion')"
        "?.textContent || '')", timeout=45000)


# -- The kiosk wake gate --------------------------------------------------------

def test_the_wake_gate_does_not_deadlock_when_pin_status_never_answers(page, core):
    """Every tap doing nothing at all, silently, for the life of the page.

    `wake()` set `gatingInProgress` before asking `/api/core/pin/status` whether
    a PIN is configured, and cleared it when the answer came. Over a dark Core
    the answer never came, so the flag stayed set and its own dedupe guard —
    there to swallow the pointerdown/touchstart/keydown triplet of one physical
    tap — turned into a permanent lock-out. A kiosk that has stopped responding
    to touch is indistinguishable from broken hardware.

    A refusal was never the case in doubt: the file already fails SAFE on a 401
    and had a documented verdict for a refused connection. A hang simply reaches
    neither.
    """
    # Acknowledge What's New first, or its takeover swallows the tap before the
    # gate ever sees it — a different early return, and not the one under test.
    page.goto(fresh(core, query="core"))
    page.wait_for_selector("#corePanel:not([hidden])", timeout=30000)
    page.evaluate(
        "() => { try { localStorage.setItem('wavr_whatsnew_seen',"
        " window.WAVR_APP_VERSION); } catch (e) {} }")

    held = []
    page.route(re.compile(r"/api/core/pin/status"), lambda route: held.append(route))
    page.goto(fresh(core, query="core"))
    page.wait_for_selector("#corePanel:not([hidden])", timeout=30000)
    assert page.evaluate(
        "() => !!document.getElementById('coreWhatsNew')?.hidden"), (
        "the release-notes takeover is up, and it swallows the tap on a "
        "different early return than the one under test")
    assert "core-dissolved" not in (
        page.locator("#corePanel").get_attribute("class") or ""), (
        "the panel was already open, so a tap would prove nothing")

    page.keyboard.press("a")             # a wake, on the same listener a tap uses
    page.wait_for_function(
        "() => !!document.getElementById('corePanel')"
        "?.classList.contains('core-dissolved')", timeout=45000)
    assert len(held) >= 2, (
        "the gate asked once at boot and never again, so the tap did not "
        f"reach it: {len(held)} request(s) held")


# -- Who's likely here ----------------------------------------------------------

_KNOWN_PRESENCE = json.dumps({
    "scope": "house", "modality": "network", "likely_home": True,
    "confidence": 0.7, "confidence_label": "coarse",
    "corroborators": [{"person": "Ana", "mac_prefix": "aa:bb:cc",
                       "present": True, "details": None}],
})


def test_who_is_likely_here_retracts_its_names_over_a_dark_core(page, core):
    """The most specific claim on the screen, and so the worst one to freeze.

    This tile names a person and puts "here" next to them. Left standing over a
    Core that stopped answering, that is not a stale reading — it is an
    assertion about who is in the building right now, made by something that
    stopped measuring.

    The answer is stubbed because a real corroborator needs a device assigned to
    a person AND a fresh sighting of it, and neither is what is under test here;
    what is under test is what the tile does when the answers stop.
    """
    state = {"dark": False}
    held = []

    def known(route):
        if state["dark"]:
            held.append(route)
            return
        route.fulfill(status=200, content_type="application/json",
                      body=_KNOWN_PRESENCE)

    page.route(re.compile(r"/api/identity/known-presence"), known)
    page.goto(fresh(core))
    goto_tab(page, "rede")
    page.wait_for_selector("#whoHome:not([hidden])", timeout=30000)
    page.wait_for_function(
        "() => /\\bhere\\b/.test(document.getElementById('whoHomeList')"
        "?.innerText || '')", timeout=30000)
    assert "Ana" in page.locator("#whoHomeList").inner_text()

    state["dark"] = True
    # 15s poll, 8s deadline, two misses before it speaks.
    page.wait_for_function(
        "() => /not answering/i.test(document.getElementById('whoHomeList')"
        "?.innerText || '')", timeout=90000)
    after = page.locator("#whoHomeList").inner_text()
    assert "Ana" not in after, after
    assert held, "no request was intercepted; the tile changed for another reason"
    # The sibling Who's-home tab reads this hook, and must stop asserting too.
    assert page.evaluate("() => window.__wavrKnownPresence") is None
