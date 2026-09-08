"""What the dashboard is allowed to draw when it cannot reach the Core.

## What the first user was shown

His Space is called "My Home" and has one room, "living room/ bedroom".

His screen said **Command Center** and listed three rooms — living room,
bedroom, backyard — each with "No sensor covers this room. Add a camera or a
sensor node to cover it." underneath, which reads as advice about a real room.
The only sign that any of it was wrong was a small chip in the corner reading
"reconnecting".

Two defects stacked, and the second one is the reason the first was invisible:

1. **The window was navigated to the wrong scheme.** `backend_url()` was read
   one line ABOVE the `wait_healthy()` call that discovers which scheme the Core
   actually answers on, so the window opened `http://` against a Core serving
   `https://`. Every request from that page failed, permanently — not until a
   retry succeeded, but forever, because the address itself was wrong.

2. **A dashboard drew anyway.** The service worker answers a failed navigation
   from its offline cache, which is correct and is what offline launch is for.
   So the shell rendered, found no map, and `loadHouse()` substituted the
   built-in sample house — the one the public demo uses, which never calls a
   backend at all.

The morning's fix had already taught the PROBE to try both schemes and record
the winner. It did not teach the navigation to read it. One address, two
sources, for the second time in one day.

## What is pinned

Read from source, because neither half can be exercised from pytest: one is
Rust in a GUI shell, the other only happens when a fetch fails. Comments are
stripped first — this file names every symbol it looks for, and a check that
reads prose finds the defect inside the warning against it.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
MAIN = RAIZ / "desktop" / "src-tauri" / "src" / "main.rs"
HOUSEMAP = RAIZ / "frontend" / "js" / "housemap.js"


def _sem_comentarios(fonte: str) -> str:
    fonte = re.sub(r"/\*.*?\*/", " ", fonte, flags=re.S)
    return re.sub(r"^\s*//.*$", "", fonte, flags=re.M)


@pytest.fixture(scope="module")
def rust() -> str:
    if not MAIN.is_file():
        pytest.skip(f"{MAIN} is not in this checkout")
    return _sem_comentarios(io.open(MAIN, encoding="utf-8", newline="").read())


@pytest.fixture(scope="module")
def mapa() -> str:
    return _sem_comentarios(io.open(HOUSEMAP, encoding="utf-8", newline="").read())


# -- the address ------------------------------------------------------------

def test_every_navigation_reads_the_address_after_the_probe(rust):
    """`backend_url()` must never be captured before `wait_healthy()` runs.

    The probe is the only thing that knows the scheme. Reading the address
    first and probing second produces a URL built from a guess, and the guess
    was wrong on the first machine this shipped to.
    """
    culpados = []
    for m in re.finditer(r"let\s+url\s*=\s*backend_url\(\)\s*;", rust):
        # What follows this binding, up to the end of its block, decides whether
        # the value was read before the thing that computes it.
        depois = rust[m.end():m.end() + 400]
        if "wait_healthy" in depois:
            antes = rust[max(0, m.start() - 200):m.start()]
            culpados.append(antes.strip().splitlines()[-1:] or ["?"])
    assert not culpados, (
        "a URL is captured before wait_healthy() runs, so the window is sent to "
        f"the guessed scheme instead of the one the socket answered: {culpados}")


def test_the_tray_restart_also_points_the_window_at_the_core(rust):
    """Two paths bring a Core back; both must tell the window.

    The crash watchdog navigated and the tray restart did not, so a restart from
    the menu left the page on whatever it had — and if the Core had come back on
    a different scheme, left it there permanently.
    """
    inicio = rust.index("fn restart_backend")
    corpo = rust[inicio:rust.index("\nfn ", inicio + 10)]
    assert "navigate" in corpo, (
        "restart_backend does not navigate the window, so the page is left "
        "showing whatever it had when the Core went away")
    assert corpo.index("wait_healthy") < corpo.index("navigate"), (
        "the window is navigated before the Core is known to answer")


def test_the_certificate_pin_is_not_gated_on_a_guess(rust):
    """The pin must be armed on every Windows launch, not "if multidevice()".

    `multidevice()` reads WAVR_MULTIDEVICE from the environment or a `.env`
    file. An installed Core has neither — the switch a person turns on in
    Settings lives in the Core's own database. So on the first machine where
    somebody actually flipped it, the pin was never installed, and the window,
    by then correctly navigated to https, was met by "Your connection isn't
    private" and a Continue (unsafe) link.

    The gate bought nothing. The handler allows only a certificate byte-
    identical to the one on disk at exactly `127.0.0.1:<port>` and cancels
    everything else, including "there is no certificate", which is what a
    plain-HTTP Core looks like. Not installing it does not make the browser
    stricter — it hands the person a button that clicks through a real
    interception.
    """
    m = re.search(r"install_cert_pinning\s*\(\s*app\.handle\(\)\s*\)", rust)
    assert m, "the cert pin is no longer installed at startup at all"
    antes = rust[max(0, m.start() - 300):m.start()]
    assert "multidevice()" not in antes, (
        "the cert pin is armed behind a condition that reads the environment. "
        "The shell cannot see the switch a person flipped in Settings, so this "
        "condition is a guess, and it was wrong on the first machine that "
        "mattered.")
    # And the scoping that makes unconditional installation safe must still be there.
    corpo = rust[rust.index("fn install_cert_pinning"):]
    corpo = corpo[:corpo.index("\nfn ", 10)]
    assert "ALWAYS_ALLOW" in corpo and "CANCEL" in corpo, (
        "the handler no longer has both outcomes")
    assert "expected_authority" in corpo and "pinned_cert_der" in corpo, (
        "the handler stopped checking the authority or the pinned bytes, which "
        "is what made installing it unconditionally safe")


# -- the house --------------------------------------------------------------

def test_a_live_dashboard_never_falls_back_to_the_sample_house(mapa):
    """`DEMO_HOUSE` belongs to the demo, which calls no backend at all.

    In live mode it is a fabrication: three rooms with names and polygons,
    drawn as somebody's home, under headings that invite them to go and buy a
    sensor for a room that does not exist.
    """
    inicio = mapa.index("async function loadHouse")
    corpo = mapa[inicio:mapa.index("\n}", mapa.index("currentFloor", inicio))]
    vivo = corpo[corpo.index('MODE === "live"'):]
    # The live branch must return before ever reaching the substitution line.
    assert "return;" in vivo.split("companion")[0], (
        "the live branch falls through to the DEMO_HOUSE substitution, so a "
        "Core that cannot be reached is drawn as a house with a living room, a "
        "bedroom and a backyard")


def test_the_substitution_is_gated_on_having_asked_not_on_being_empty(mapa):
    """An empty map and an unanswered Core are different facts.

    The old condition — "if the map has no floors, use the sample" — cannot tell
    them apart, which is exactly why it fired on a live Core that was simply
    unreachable.
    """
    assert "HOUSE_FROM_CORE" in mapa, (
        "nothing records whether the map came from a Core, so the sample can "
        "still stand in for an answer that never arrived")


def test_an_unreachable_core_leaves_the_map_empty(mapa):
    """Empty is the honest state, and the shell already has words for it.

    `index.html` ships the rooms panel holding "Waiting for the Core's first
    reading…", and nothing replaces it until a real card is drawn. That sentence
    is true when the Core is silent. Three invented rooms are not.
    """
    assert re.search(r"floors:\s*\[\]", mapa), (
        "the live failure path no longer resets the map to an empty one")
    espera = (RAIZ / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "Waiting for the Core" in espera, (
        "the rooms panel lost its empty state, so an unreachable Core now "
        "renders a blank area with no explanation")
