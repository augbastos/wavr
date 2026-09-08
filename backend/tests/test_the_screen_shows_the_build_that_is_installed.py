"""What the person in front of the machine can actually see.

Three defects on one afternoon, all of them the same shape: the code was right,
was served, and never reached the screen.

1. **The shell was cache-first.** `sw.js` precached `index.html` and every module
   under `/js/`, called them "content-stable", and served them from Cache Storage
   until somebody remembered to edit a version constant by hand. They are not
   content-stable; they are the product. The first real user reinstalled Wavr,
   opened Settings, and could not find a box that had been in the served HTML for
   hours — his WebView2 was answering out of a cache written before it existed,
   and reinstalling an app does not clear that. Every test in this suite passed
   the whole time, because every one of them asks the Core what it serves, and
   the Core was serving the right thing.

2. **The search box was exiled.** It was pushed to the far right of a 1911px bar
   by `margin-left:auto`: present, visible, measurable, and a hand's width from
   the word it belongs to, alone in an empty corner. Measuring it returned
   `visibility: visible, opacity: 1` and told me nothing.

3. **The filter resurrected panels the product had hidden.** `wireSettingsFilter`
   ran once on load with an empty query and wrote `hidden = false` onto every
   tile in the overlay, including the pairing panel, which is hidden precisely
   when its endpoints are not mounted. A Core with LAN access off therefore
   offered a complete pairing flow — access levels, "New code now", instructions
   — that could not have worked. Nothing typed it; opening the screen was enough.

So this file measures the screen, not the response body, and it measures it in
the window size he actually uses. The two static checks at the end exist because
the browser cannot see a release-process defect: a shell that goes stale only
does so on the *second* install, which no test run ever performs.

The Core, browser and page fixtures come from `test_browser_ui`, copied rather
than imported for the reason stated there.
"""
from __future__ import annotations

import io
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
from playwright.sync_api import sync_playwright  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
FRONTEND = BACKEND.parent / "frontend"

# His window, from the screenshots: a maximised Wavr Desktop on this laptop.
# The width matters — the defect was a control placed at x=1575 of 1911, which
# is inside the viewport at every size and findable at none of them.
VIEWPORT = {"width": 1911, "height": 1010}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _sem_comentarios(fonte: str) -> str:
    """The code, without the prose.

    Every one of these files carries a comment explaining the mistake it used to
    make, quoting the line that made it. A check that reads the comments finds
    the bug it is looking for in the warning against it.
    """
    fonte = re.sub(r"/\*.*?\*/", " ", fonte, flags=re.S)
    return re.sub(r"^\s*//.*$", "", fonte, flags=re.M)


@pytest.fixture(scope="module")
def core(tmp_path_factory):
    """A real Core with LAN access OFF — the state he was in.

    Off is the default and the state the whole afternoon was spent in: the
    pairing panel cannot work, so what Devices shows instead is the thing under
    test.
    """
    port = _free_port()
    db = tmp_path_factory.mktemp("visivel") / "wavr.db"
    env = {
        **os.environ,
        "WAVR_DB": str(db),
        "WAVR_HOUSE_MAP": str(db.parent / "house.json"),
        "WAVR_MULTIDEVICE": "0",
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

        import urllib.request
        req = urllib.request.Request(
            base + "/api/setup/create-space",
            data=json.dumps({"name": "Visible Test", "kind": "home",
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
def settings(browser, core):
    """The Settings overlay, open, at his window size."""
    ctx = browser.new_context(viewport=VIEWPORT)
    pg = ctx.new_page()
    erros: list[str] = []
    pg.on("pageerror", lambda e: erros.append(str(e)))
    pg.goto(core, wait_until="networkidle", timeout=60000)
    pg.wait_for_timeout(2500)
    for sel in ("#gearNavBtn", "#gearTopBtn"):
        btn = pg.query_selector(sel)
        if btn and btn.is_visible():
            btn.click()
            break
    pg.wait_for_selector("#gearOverlay:not([hidden])", timeout=15000)
    pg.wait_for_timeout(3000)
    pg.script_errors = erros
    yield pg
    ctx.close()


# --------------------------------------------------------------------------
# What is on the screen
# --------------------------------------------------------------------------

def test_the_search_box_is_next_to_the_word_settings(settings):
    """Beside the heading, not marooned at the far edge of the bar.

    "Visible" was never the property in question: the exiled box measured
    visible, opaque and inside the viewport while he was telling me, correctly,
    that there was no search box. The property is proximity to the thing it
    belongs to — a person looks for a search field where the surface is named.
    """
    caixa = settings.evaluate("""() => {
        const inp = document.getElementById('settingsFilter');
        const h = document.getElementById('gear-h');
        if (!inp || !h) return null;
        const a = inp.getBoundingClientRect(), b = h.getBoundingClientRect();
        const s = getComputedStyle(inp);
        return {vazio: a.width < 40 || a.height < 20,
                oculto: s.visibility === 'hidden' || s.opacity === '0',
                folga: Math.round(a.left - b.right)};
    }""")
    assert caixa is not None, "no search box in the Settings bar at all"
    assert not caixa["vazio"], f"the search box has no size: {caixa}"
    assert not caixa["oculto"], f"the search box is not painted: {caixa}"
    assert caixa["folga"] < 240, (
        "the search box is {}px from the end of the word 'Settings' — that is "
        "where the first user looked straight past it".format(caixa["folga"]))


def test_typing_narrows_settings_to_what_was_typed(settings):
    """The box does the job it is there for."""
    settings.fill("#settingsFilter", "other devices")
    settings.wait_for_timeout(600)
    resultado = settings.evaluate("""() => {
        const vivo = [...document.querySelectorAll('#gearOverlay .setting-row')]
            .filter(r => !r.hidden && !r.hasAttribute('data-filtered'));
        return {linhas: vivo.length,
                textos: vivo.map(r => (r.innerText || '').split('\\n')[0].slice(0, 60))};
    }""")
    assert resultado["linhas"] >= 1, "searching for a setting that exists found nothing"
    assert any("other devices" in t.lower() for t in resultado["textos"]), (
        f"the switch that was searched for is not among the results: {resultado}")


def test_the_filter_never_reveals_what_the_product_hid(settings):
    """The pairing panel stays hidden — before typing, while typing, and after.

    This is the bug the filter shipped with. `apply()` runs once on wiring, and
    its first version set `hidden = false` on every tile it could reach, so the
    screen offered a pairing flow against a Core with no pairing endpoints. No
    keystroke was needed; opening Settings did it.
    """
    def estado():
        return settings.evaluate("""() => {
            const p = document.getElementById('pairing');
            return {escondido: p.hidden, naTela: p.offsetParent !== null};
        }""")

    inicial = estado()
    assert inicial["escondido"] and not inicial["naTela"], (
        f"PAIR DEVICE is on screen with LAN access off: {inicial}")

    settings.fill("#settingsFilter", "pair")
    settings.wait_for_timeout(500)
    durante = estado()
    assert durante["escondido"] and not durante["naTela"], (
        f"searching brought back a panel the product had hidden: {durante}")

    settings.fill("#settingsFilter", "")
    settings.wait_for_timeout(500)
    depois = estado()
    assert depois["escondido"] and not depois["naTela"], (
        f"clearing the search brought back a hidden panel: {depois}")


def test_devices_says_why_nothing_can_pair_and_points_at_the_switch(settings):
    """The screen a person opens to connect a phone is not silent.

    Hiding the pairing panel is right. Hiding it and saying nothing left the
    website, the download page and the rescue script all pointing at an empty
    space, and the first user wrote "estou perdido".
    """
    settings.fill("#settingsFilter", "")
    settings.wait_for_timeout(400)
    aviso = settings.evaluate("""() => {
        const t = document.getElementById('pairingOff');
        if (!t) return null;
        return {naTela: t.offsetParent !== null,
                texto: (t.innerText || '').replace(/\\s+/g, ' ').toLowerCase()};
    }""")
    assert aviso is not None, "nothing stands in for the pairing panel"
    assert aviso["naTela"], "Devices is silent about why nothing can pair"
    assert "let other devices connect" in aviso["texto"], (
        "the stand-in does not name the switch: " + aviso["texto"][:200])

    settings.click("#pairingOffGo")
    settings.wait_for_timeout(1200)
    apontado = settings.evaluate("""() => {
        const r = document.querySelector('.setting-row[data-key="lan_access"]');
        if (!r) return null;
        const b = r.getBoundingClientRect();
        return {marcada: r.classList.contains('setting-row-pointed'),
                naJanela: b.top > -1 && b.bottom < innerHeight + 1};
    }""")
    assert apontado is not None, "the lan_access row carries no key to point at"
    assert apontado["marcada"], "the row was scrolled to but not marked"
    assert apontado["naJanela"], f"the row is not on screen after being pointed at: {apontado}"


def test_the_overlay_raised_no_script_errors(settings):
    assert settings.script_errors == [], settings.script_errors


# --------------------------------------------------------------------------
# What a browser cannot see: the second install
# --------------------------------------------------------------------------

def test_the_service_worker_asks_the_core_before_the_cache():
    """The shell is network-first, so an upgrade is never masked by a cache.

    A browser test cannot catch this. Going stale requires a *second* install
    over a Cache Storage the first one wrote, and a test run only ever performs
    the first. What can be checked is the order of the two calls in the branch
    that answers for `index.html` and every module the shell loads.
    """
    fonte = _sem_comentarios(io.open(FRONTEND / "sw.js", encoding="utf-8").read())
    inicio = fonte.index("if (isShell)")
    ramo = fonte[inicio:fonte.index("isNavigation", inicio)]
    assert "fetch(req)" in ramo, "the shell branch never asks the network"
    assert ramo.index("fetch(req)") < ramo.index("caches.match"), (
        "the shell is answered from cache before the network — this is the "
        "defect that made a shipped fix invisible to an installed Core")
    assert "caches.match" in ramo, (
        "the offline fallback is gone; the app would no longer cold-launch "
        "without a Core")


def test_the_settings_filter_owns_an_attribute_and_not_hidden():
    """`hidden` in this overlay belongs to the product, not to the search box.

    The filter has one attribute of its own. Anything it writes to `hidden` is a
    claim about whether a feature can work, which it has no way of knowing.
    """
    fonte = _sem_comentarios(
        io.open(FRONTEND / "js" / "core-settings.js", encoding="utf-8").read())
    corpo = fonte[fonte.index("function wireSettingsFilter"):]
    corpo = corpo[:corpo.index("\nrenderCoreSettings()")]
    ofensas = re.findall(r"^.*\.hidden\s*=(?!=).*$", corpo, flags=re.M)
    # The "nothing matched" note is the filter's own element, created for this
    # and shown by nothing else, so it is the one thing it may still hide.
    ofensas = [l for l in ofensas if "empty.hidden" not in l]
    assert ofensas == [], (
        "the settings filter writes `hidden`, which is how it once put a dead "
        "pairing flow on screen: " + " | ".join(x.strip() for x in ofensas))
