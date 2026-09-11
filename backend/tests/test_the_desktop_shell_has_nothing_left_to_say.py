"""Deleting the desktop crash banner is only safe while the dashboard speaks.

## What was deleted

`report_backend_crashed` in the Tauri shell used to eval

    window.wavrShowStartupError && window.wavrShowStartupError(msg)

into the main window. That function is defined in exactly one place —
`desktop/dist/index.html`, the placeholder shown while the Core is starting —
and never in the live dashboard the Core serves. The crash monitor that calls
it only runs after the window has navigated to that dashboard, so the guard was
false on every crash there has ever been. The banner was not rare. It was
impossible.

## Why the deletion is safe, and what makes it stay safe

Because the dashboard already answers for a dead Core, and answers better than
a Rust-injected string could: the runtime chip goes red and reads "Not
responding", the headline gains a reconnecting suffix, and each tile that
depends on a reading dims and says it may be out of date.

That is now load-bearing. This file holds the three pieces of it, so that if
somebody makes the dashboard quieter about a dead Core, a test fails here
rather than a person discovering it in front of a stopped Core.

It deliberately does NOT test the Rust: the point is not that the eval is gone,
it is that nothing needs it.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHELL = ROOT / "frontend" / "index.html"
RUNTIME = ROOT / "frontend" / "js" / "runtime.js"
CONN = ROOT / "frontend" / "js" / "core-connection.js"
MAIN_RS = ROOT / "desktop" / "src-tauri" / "src" / "main.rs"


def test_the_dashboard_still_says_the_core_is_not_responding():
    """The chip's own word for it. Same producer as the tray and the CLI."""
    src = RUNTIME.read_text(encoding="utf-8")
    assert 'unavailable: function () { return WavrT("Not responding"); }' in src, (
        "the runtime chip no longer has a word for an unreachable Core — the "
        "desktop shell was relying on it having one")


def test_the_dashboard_still_says_it_is_reconnecting():
    src = CONN.read_text(encoding="utf-8")
    assert "reconnecting" in src.lower(), (
        "nothing in the connection layer says it is reconnecting any more")


def test_the_dashboard_still_marks_its_readings_stale():
    """The half that matters most: a Core that stopped an hour ago must not
    leave confident-looking room cards on screen."""
    src = SHELL.read_text(encoding="utf-8")
    assert "rooms-stale" in src, "the stale-reading treatment is gone"
    assert re.search(r"No reading has arrived for a while", src), (
        "the sentence that admits the cards may be out of date is gone")


def test_the_shell_no_longer_pretends_to_show_a_banner():
    """The control for the three above.

    If the eval came back, these tests would still pass and the shell would
    still be silent — so this one asserts the deletion itself, and names the
    reason so a future reader does not restore it as a fix.
    """
    if not MAIN_RS.exists():                       # pragma: no cover
        return
    rs = MAIN_RS.read_text(encoding="utf-8")
    fn = rs[rs.index("fn report_backend_crashed"):]
    fn = fn[:fn.index("\nfn ", 1)]
    # Comments out first. The deletion is explained in a comment that names the
    # function it removed — so a check over the raw text finds the word it is
    # looking for in the very sentence saying the call is gone, and reports a
    # regression that is actually documentation.
    fn = re.sub(r"//[^\n]*", "", fn)
    assert "wavrShowStartupError" not in fn, (
        "report_backend_crashed is calling wavrShowStartupError again. That "
        "function exists only in desktop/dist/index.html, and this path only "
        "runs after the window has left that page — it cannot fire.")
    assert "notification" in fn, (
        "the OS notification went with it; that one is the part a web page "
        "genuinely cannot do")
