"""What the dashboard must not do on a phone or a tablet.

The first user paired his S25, looked at it, and said the presentation was ugly.
"Ugly" is not actionable, so it was measured at 412x915 and 800x1280, and what
came back was specific:

* The status pills wrapped onto **four rows**. The first line of content began
  550px down a 915px screen — sixty per cent of the phone was chrome before the
  answer the product exists to give. Wrapping is what the desktop rule does and
  it is right there, where the bar has room; here the row grows with how much
  the product has to say, on the narrowest screen it has to say it.
* Three elements were **wider than the viewport** — the uncovered-room card, its
  heading and its note. A room name is text a household typed, and that card is
  built out of nothing else, so it is the first thing to push a phone sideways.
* **Eleven controls were under 40px.** Eight of those were a measurement
  artefact: `@media (pointer:coarse)` already covers them, and a plain headless
  context does not report a coarse pointer. Measuring as a narrow desktop
  instead of as a phone would have "fixed" eight things that were never broken.
  Three were real — Off / Presence / Precise, the buttons that decide how much
  of somebody's home is sensed, at 31px.
* On the tablet, the navigation was **five unlabelled icons**. The 760–1199px
  rule is written for a laptop window dragged narrow, where space is scarce and
  a pointer can hover for a tooltip. A tablet is the same width and nothing else
  the same.

## The axis these are checked on

Two of the fixes are filed under `pointer:coarse` rather than a width, and the
tests run with touch emulation for the same reason: a finger and a narrow window
are different problems that happen to share a number. Filed by width, the fix
for the segmented buttons left the 800px tablet — every bit as touched — with
the 31px version.
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

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
from playwright.sync_api import sync_playwright  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]

TELAS = [
    pytest.param(412, 915, True, id="phone-412x915"),
    pytest.param(800, 1280, True, id="tablet-800x1280"),
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def core(tmp_path_factory):
    port = _free_port()
    db = tmp_path_factory.mktemp("movel") / "wavr.db"
    env = {**os.environ, "WAVR_DB": str(db),
           "WAVR_HOUSE_MAP": str(db.parent / "house.json"),
           "WAVR_LOCAL_TOKEN": "", "WAVR_MULTIDEVICE": "0"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "wavr.app:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(BACKEND), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        fim = time.monotonic() + 60
        while time.monotonic() < fim:
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
            data=json.dumps({"name": "Layout", "kind": "home",
                             "owner_name": "Tester",
                             "room": "a room with a deliberately long name"}).encode(),
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
def navegador():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:      # noqa: BLE001
            pytest.skip(f"no chromium available: {exc}")
        yield b
        b.close()


def _medir(navegador, core, larg, alt, toque):
    ctx = navegador.new_context(viewport={"width": larg, "height": alt},
                                has_touch=toque, is_mobile=(larg < 600))
    pg = ctx.new_page()
    pg.goto(core, wait_until="networkidle", timeout=60000)
    pg.wait_for_timeout(4000)
    d = pg.evaluate("""() => {
        const vis = (e) => e && e.offsetParent !== null;
        const bar = document.querySelector('.topbar');
        const primeiro = [...document.querySelectorAll('.tile')].find(vis);
        const largos = [...document.querySelectorAll('body *')].filter(e =>
            vis(e) && e.getBoundingClientRect().width > innerWidth + 2)
            .slice(0, 8).map(e => (e.tagName + '.' + (e.className || '')).slice(0, 46));
        const pequenos = [...document.querySelectorAll('button, a[href], input, select')]
            .filter(vis).map(e => ({el: (e.tagName + '.' + (e.className||'')).slice(0,40),
                                    h: Math.round(e.getBoundingClientRect().height)}))
            .filter(x => x.h > 0 && x.h < 40);
        const rotulos = [...document.querySelectorAll('.nav-item .nav-label')]
            .filter(vis).length;
        return {
            cabecalho: bar ? Math.round(bar.getBoundingClientRect().height) : null,
            conteudoComeca: primeiro
                ? Math.round(primeiro.getBoundingClientRect().top) : null,
            maisLargosQueATela: largos,
            alvosPequenos: pequenos,
            rotulosDeNavegacao: rotulos,
            rolaDeLado: document.documentElement.scrollWidth > innerWidth + 2,
        };
    }""")
    ctx.close()
    return d


@pytest.mark.parametrize("larg,alt,toque", TELAS)
def test_nothing_is_wider_than_the_screen(navegador, core, larg, alt, toque):
    d = _medir(navegador, core, larg, alt, toque)
    assert not d["maisLargosQueATela"], (
        f"elements wider than the {larg}px viewport: {d['maisLargosQueATela']}")
    assert not d["rolaDeLado"], (
        "the page scrolls sideways, which reads as broken before anybody has "
        "read a word of it")


@pytest.mark.parametrize("larg,alt,toque", TELAS)
def test_every_control_a_finger_can_reach_is_big_enough(navegador, core, larg, alt, toque):
    d = _medir(navegador, core, larg, alt, toque)
    assert not d["alvosPequenos"], (
        f"controls under 40px on a touch screen: {d['alvosPequenos'][:6]}")


def test_the_chrome_does_not_eat_the_phone(navegador, core):
    """Measured, not judged: how far down the first card starts."""
    d = _medir(navegador, core, 412, 915, True)
    assert d["cabecalho"] <= 90, (
        f"the top bar is {d['cabecalho']}px tall on a phone. It wrapped to four "
        f"rows once; the pills scroll on one line now, and if that has been "
        f"undone this is what says so.")
    assert d["conteudoComeca"] <= 260, (
        f"the first card starts {d['conteudoComeca']}px down a 915px screen. "
        f"It was 550px when the first user called the presentation ugly.")


def test_the_tablet_navigation_says_where_it_goes(navegador, core):
    """Five unlabelled icons is a laptop-window layout on a touched screen."""
    d = _medir(navegador, core, 800, 1280, True)
    assert d["rotulosDeNavegacao"] >= 4, (
        f"only {d['rotulosDeNavegacao']} navigation labels are visible on a "
        f"tablet. The icon-only rail is for a narrow laptop WINDOW, where there "
        f"is a pointer and a tooltip; a tablet has neither.")
