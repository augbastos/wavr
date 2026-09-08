"""Somebody who wants to use Wavr on their phone can find how, by looking.

## What happened

"Devices & sensors" is the only item in the whole navigation whose name
suggests connecting a phone. What it offered was this, at the bottom of a card
about sensor onboarding, below a divider, in 0.78rem dim text:

    Pairing a phone or tablet instead of a sensor?  Go to pairing

The link worked. It opened the settings overlay and scrolled to the pairing
panel. It had existed for weeks. The first user opened the screen, looked, and
wrote: *"eu nem sei aonde fica essa tela e de novo, se eu nao consigo achar eh
pq nao esta adequadamente apresentavel pro usuario medio."*

He is right, and this is the second time the same navigation lost him on the
same subject — the first was the LAN switch, filed under "Advanced".

## Why this is measured in a browser and not in the markup

The defect was not absence. Every static check would have passed: the words
were on the page, the handler was wired, the flow it led to worked. What was
wrong was **size and position** — properties that only exist once something is
laid out. So this opens the screen, finds the control by what it says, and asks
where it is and how big.

The Core, browser and page fixtures are copied from `test_browser_ui` for the
reason stated there.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
from playwright.sync_api import sync_playwright  # noqa: E402

from pathlib import Path  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def core(tmp_path_factory):
    port = _free_port()
    db = tmp_path_factory.mktemp("achavel") / "wavr.db"
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
            data=json.dumps({"name": "Findable", "kind": "home",
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
def devices_screen(core):
    """The Devices & sensors screen, open, as somebody who wants their phone on it."""
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:      # noqa: BLE001
            pytest.skip(f"no chromium available: {exc}")
        ctx = b.new_context(viewport={"width": 1400, "height": 950})
        pg = ctx.new_page()
        erros: list[str] = []
        pg.on("pageerror", lambda e: erros.append(str(e)))
        pg.goto(core, wait_until="networkidle", timeout=60000)
        pg.wait_for_timeout(3000)
        pg.evaluate("() => { if (window.switchTab) window.switchTab('dispositivos'); }")
        pg.wait_for_timeout(2500)
        pg.script_errors = erros
        yield pg
        b.close()


def _o_cartao(page):
    return page.evaluate("""() => {
        const s = document.getElementById('panel-dispositivos');
        if (!s) return null;
        const t = document.getElementById('phoneTile');
        if (!t || t.offsetParent === null) return {existe: false};
        const cartoes = [...s.querySelectorAll('.tile')].filter(x => x.offsetParent !== null);
        const botao = document.getElementById('phoneTilePair');
        const rb = botao ? botao.getBoundingClientRect() : null;
        const cs = botao ? getComputedStyle(botao) : null;
        return {
            existe: true,
            posicao: cartoes.indexOf(t),
            quantosCartoes: cartoes.length,
            titulo: (t.querySelector('h2') || {}).innerText || '',
            temBotao: !!botao,
            botaoEhBotao: botao ? botao.tagName === 'BUTTON' : false,
            botaoAltura: rb ? Math.round(rb.height) : 0,
            botaoTopo: rb ? Math.round(rb.top) : -1,
            corpoFonte: cs ? cs.fontSize : '',
        };
    }""")


def test_the_devices_screen_offers_the_phone_first(devices_screen):
    """First card, not a footnote at the bottom of another one."""
    c = _o_cartao(devices_screen)
    assert c is not None, "the Devices & sensors panel is not on the page"
    assert c["existe"], (
        "Devices & sensors has no card about putting Wavr on a phone. It is "
        "the only screen in the navigation whose name suggests it, and the "
        "first user could not find the pairing screen from anywhere.")
    assert c["posicao"] == 0, (
        f"the phone card is card number {c['posicao'] + 1} of "
        f"{c['quantosCartoes']}; somebody arriving with this question should "
        f"not have to scroll past other subjects to find it")
    assert "phone" in c["titulo"].lower(), (
        f"the card's heading does not use the word somebody is looking for: "
        f"{c['titulo']!r}")


def test_the_call_to_action_is_a_real_button_above_the_fold(devices_screen):
    """The old one was 0.78rem dim text below a divider. Size was the defect."""
    c = _o_cartao(devices_screen)
    assert c["temBotao"] and c["botaoEhBotao"], (
        "there is no button — a person looking for something to press finds "
        "prose")
    assert c["botaoAltura"] >= 30, (
        f"the button is {c['botaoAltura']}px tall; the footnote it replaces "
        f"was small text and small text is what nobody saw")
    assert 0 < c["botaoTopo"] < 900, (
        f"the button sits at y={c['botaoTopo']} in a 950px window, so it is "
        f"below the fold on the screen a person opens with this question")
    tamanho = float(c["corpoFonte"].replace("px", "") or 0)
    assert tamanho >= 13, (
        f"the button's text is {tamanho}px; the footnote was 0.78rem (~12.5px) "
        f"and went unread")


def test_pressing_it_lands_on_something_visible(devices_screen):
    """With LAN access off the pairing panel is hidden and `#pairingOff` stands
    in for it. Scrolling to a hidden element does nothing at all, which would
    leave a person one screen deeper than they started with nothing to see."""
    devices_screen.click("#phoneTilePair")
    devices_screen.wait_for_timeout(1200)
    onde = devices_screen.evaluate("""() => {
        const ov = document.getElementById('gearOverlay');
        const p = document.getElementById('pairing');
        const off = document.getElementById('pairingOff');
        const naTela = (e) => {
            if (!e || e.offsetParent === null) return false;
            const r = e.getBoundingClientRect();
            return r.bottom > 0 && r.top < innerHeight;
        };
        return {overlayAberto: ov ? !ov.hidden : false,
                pairingNaTela: naTela(p), standInNaTela: naTela(off)};
    }""")
    assert onde["overlayAberto"], "the button did not open Settings"
    assert onde["pairingNaTela"] or onde["standInNaTela"], (
        "Settings opened but neither the pairing panel nor the stand-in that "
        "replaces it is on screen, so the button delivered somebody to a "
        "screen and left them to search it")


def test_no_script_errors(devices_screen):
    assert devices_screen.script_errors == [], devices_screen.script_errors
