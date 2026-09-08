"""The shell may not go deaf while it waits, and may not stop learning.

Three defects, all introduced by tonight's own fixes to the desktop shell, all
found by reading the diff rather than by running it.

## 1. "Restart Core" froze the whole app for 45 seconds

Making the restart WAIT for health was the right fix — the button used to write
`Wavr: Core restarted from the tray.` to the log without having restarted
anything. But the tray menu handler runs on the event loop, so the wait ran
there too. The window stops repainting, Windows paints it "Not Responding", and
the tray menu cannot be opened again. The worst case is precisely the case the
button exists for: a Core that is not coming back, where the wait runs its full
length.

A fix that makes an honest report by hanging the product is not an improvement
a person can tell apart from a crash.

## 2. The scheme was learned once and could never be re-learned

The shell used to PREDICT whether the Core serves http or https; tonight it
started discovering it. But it stored the answer in a `OnceLock`, and the second
write was discarded by `let _ =`. The scheme is not a constant of the process —
it is a property of the running Core, and the Core is replaced during a normal
session by this very Restart button and by the crash watchdog.

Turn "Let other devices connect" on, press Restart Core, and the probe finds
https, writes a log line saying it found https, and then sends the window to the
http it had already committed to. Every request on that page fails, the service
worker answers the navigation out of its offline cache, and a fully drawn
dashboard appears showing a house nobody lives in.

## 3. The tray poller froze its address from the guess

`spawn_runtime_presence` built its URL once, and it starts BEFORE the health
probe — so the URL kept the guess for the life of the process while
`fetch_runtime` re-read the discovered scheme for its own branch. The two
disagreed, every tick failed, and the tray read "Wavr is not answering on this
machine" beside a window that was working perfectly. The tray is the surface a
person believes, because it is the one they see without opening anything.

## Why these are read from the source

There is no way to run the shell from here. Comments are stripped first: this
file names every symbol it checks for, and a check that reads prose finds the
defect inside the warning against it. That has happened three times in this
repository.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

MAIN = (Path(__file__).resolve().parents[2] / "desktop" / "src-tauri"
        / "src" / "main.rs")


@pytest.fixture(scope="module")
def codigo() -> str:
    if not MAIN.is_file():
        pytest.skip(f"{MAIN} is not in this checkout")
    fonte = io.open(MAIN, encoding="utf-8", newline="").read()
    fonte = re.sub(r"/\*.*?\*/", " ", fonte, flags=re.S)
    return re.sub(r"^\s*//.*$", "", fonte, flags=re.M)


def _corpo(codigo: str, assinatura: str) -> str:
    """The body of one function, by brace balance."""
    inicio = codigo.index(assinatura)
    abre = codigo.index("{", inicio)
    nivel = 0
    for i in range(abre, len(codigo)):
        if codigo[i] == "{":
            nivel += 1
        elif codigo[i] == "}":
            nivel -= 1
            if nivel == 0:
                return codigo[abre:i + 1]
    raise AssertionError(f"unbalanced braces after {assinatura}")


# --------------------------------------------------------------------------- #
# 1. The wait happens somewhere the window can keep drawing.
# --------------------------------------------------------------------------- #
def test_the_restart_does_not_wait_on_the_event_loop(codigo):
    """The menu handler may schedule the restart; it may not perform it.

    Pinned as "the call is inside a spawned thread" rather than "the handler is
    short", because the thing that must never come back is a blocking wait on
    the thread that paints."""
    chamadas = [m for m in re.finditer(r"restart_backend\s*\(", codigo)]
    assert chamadas, "nothing restarts the Core any more"
    for m in chamadas:
        if codigo[:m.start()].rstrip().endswith("fn"):
            continue                                    # the definition itself
        antes = codigo[max(0, m.start() - 220):m.start()]
        assert "thread::spawn" in antes, (
            "restart_backend is called without a thread around it; if that call "
            "site is the tray menu handler, the window freezes for the whole "
            "health timeout")


def test_the_restart_still_waits_for_health_somewhere(codigo):
    """Moving the wait off the main thread must not quietly delete it. The
    button used to report success without restarting anything."""
    corpo = _corpo(codigo, "fn restart_backend(")
    assert "wait_healthy" in corpo, (
        "restart_backend no longer waits for the Core to answer — it is back to "
        "reporting a success it did not earn")


# --------------------------------------------------------------------------- #
# 2. The discovered scheme can be discovered again.
# --------------------------------------------------------------------------- #
def test_the_live_scheme_can_be_relearned(codigo):
    achado = re.search(r"static\s+LIVE_SCHEME\s*:\s*([^=]+)=", codigo)
    assert achado, "LIVE_SCHEME is gone — has scheme discovery been removed?"
    tipo = achado.group(1)
    assert "OnceLock" not in tipo, (
        "LIVE_SCHEME is write-once again. The Core is replaced during a normal "
        "session (Restart Core, the crash watchdog) and can come back on the "
        "other scheme; a value that cannot be rewritten sends the window to the "
        "dead one and the offline cache draws a house nobody lives in")


def test_every_success_records_what_answered(codigo):
    """`wait_healthy` must write the scheme on every success, not only the
    first — that is the half that makes the type above matter."""
    corpo = _corpo(codigo, "fn wait_healthy(")
    assert "remember_scheme" in corpo, (
        "wait_healthy no longer records which scheme answered")
    assert "LIVE_SCHEME.set" not in corpo, (
        "the write-once setter is back inside wait_healthy")


# --------------------------------------------------------------------------- #
# 3. The tray asks for the current address, every time.
# --------------------------------------------------------------------------- #
def test_the_tray_poller_rebuilds_its_address_each_tick(codigo):
    """This thread starts before the probe runs, so anything it computes once
    is computed from the guess."""
    corpo = _corpo(codigo, "fn spawn_runtime_presence(")
    laco = corpo[corpo.index("loop {"):]
    assert "backend_url()" in laco, (
        "the tray poller's URL is built outside its loop, so it keeps the "
        "guessed scheme for the life of the process and reports a working "
        "Core as not answering")
