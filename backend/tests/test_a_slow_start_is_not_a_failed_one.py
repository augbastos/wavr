"""The shell keeps watching after it says the Core has not answered.

## What happened

Measured on the first user's machine, from `desktop.log` and the process table:

    21:01:51  the shell starts
    21:02:01  the Core's bootloader is spawned
    21:02:25  the 20s wait gives up and paints "Wavr didn't start"
    21:02:28  the Core's worker begins answering on port 8000

Three seconds. The Core then served for a minute and a half — 200 on
`/healthz`, 137MB resident — while the window sat on the failure screen and
nothing in the shell was looking any more.

Nothing was broken. A first start after an install self-extracts a 40MB binary
that Windows is scanning for the first time, and that costs more than twenty
seconds on a real laptop.

## Why the fix is not a bigger number

The number was raised, because 20s was measurably wrong. But a longer wait only
moves the cliff: whatever it is, some machine is slower, and on that machine the
same screen appears over the same working Core.

What was actually wrong is that the FIRST answer was treated as the LAST one.
The wait now reports honestly and a thread keeps probing behind the message; if
the Core comes up the window goes where it should have gone. The message says
so, rather than reading as a verdict, and the notification no longer says
"never" about something still being watched.

Read from the Rust source: none of this can be exercised from pytest, and a
timeout defect is one nobody reproduces on purpose. Comments are stripped first
— this file's own explanation quotes the words it checks for.
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


def _segundos(codigo: str, nome: str) -> int:
    m = re.search(rf"const {nome}: Duration = Duration::from_secs\((\d+)\)", codigo)
    assert m, f"{nome} is gone or no longer a from_secs constant"
    return int(m.group(1))


def test_the_startup_wait_allows_for_a_cold_first_start(codigo):
    """20s was measurably short: the Core answered at 27."""
    assert _segundos(codigo, "HEALTH_TIMEOUT") >= 40, (
        "the startup wait is back under 40s. A first launch after installing "
        "self-extracts a 40MB binary while Windows scans it; 20s produced a "
        "failure screen over a Core that answered three seconds later.")


def test_giving_up_is_not_the_end_of_watching(codigo):
    """The defect was the verdict, not the number."""
    assert "LATE_START_GRACE" in codigo, (
        "there is no grace period after the startup wait, so the first answer "
        "is the last one again — which is what left a working Core behind a "
        "failure screen for ninety seconds")
    assert _segundos(codigo, "LATE_START_GRACE") >= 60, (
        "the grace period is under a minute; the case it exists for is a "
        "machine slower than the one the timeout was measured on")
    # Anchored on the failure message itself, because "the failure branch" has
    # no other landmark a regex can trust — the first attempt matched the first
    # `} else {` in the file, a thousand lines away, and reported a real defect
    # about code that has nothing to do with starting the Core.
    inicio = codigo.index("Wavr hasn't answered on port")
    depois = codigo[inicio:inicio + 3000]
    assert "LATE_START_GRACE" in depois, (
        "the grace period is declared but the path that shows the failure "
        "message does not use it")
    pos_grace = depois.index("LATE_START_GRACE")
    assert "navigate" in depois[pos_grace:], (
        "nothing navigates the window when the Core finally answers, so the "
        "failure screen stays up over a working Core")


def test_the_message_does_not_read_as_a_verdict(codigo):
    """It is shown while a thread is still watching. It must say so."""
    m = re.search(r'"Wavr hasn\'t answered on port \{\}[^"]*"', codigo)
    assert m, (
        "the startup failure message was reworded; check it still says the "
        "watching continues, because it is displayed while it does")
    texto = m.group(0).lower()
    assert "still watching" in texto, (
        "the message no longer says the shell is still trying, while it is")
    assert "did not answer" not in texto and "never" not in texto, (
        "the message reads as a final verdict again")


def test_the_notification_does_not_say_never(codigo):
    """It fired saying the backend "never became healthy" three seconds before
    the backend became healthy."""
    assert "never became healthy" not in codigo, (
        'the startup notification says the backend "never became healthy" — a '
        "claim about the future, sent while the shell was still watching")
