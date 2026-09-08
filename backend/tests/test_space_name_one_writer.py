"""The Space name has exactly one writer, and it is reachable in every mode.

## The bug this file is built around

The writer started life inside the first-run wizard, which is the natural place
for it — the wizard is where a Space gets named. But that module opens with:

    if (mode !== "live") return;

so everything below it is unreachable on a paired phone and on the Core's own
kiosk face. Those are precisely the two surfaces that need the answer: a phone
is the device most likely to be attached to more than one Core, and the kiosk
is an unattended screen somebody walks up to. Both showed a room map with no
statement of which home it belongs to, which is how a person dismisses an
intrusion alert for the wrong house.

Exporting the function from the bottom of that IIFE looks like it fixes it and
does not: the export is below the early return too.

## The rules

1. The writer lives in a module with no early mode-return, and is published as
   `window.__wavrShowSpace`.
2. Nobody else assigns `#brandSpace`'s text or the Space part of the title.
   The rename form used to write the slot without the title, so a renamed Space
   kept its old name in the browser tab until a reload.
"""
from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
RUNTIME = FRONTEND / "js" / "runtime.js"
WIZARD = FRONTEND / "js" / "wizard.js"
SHELL = FRONTEND / "index.html"

def _js(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# The bail-outs that make a module's tail unreachable. These are the exact
# shapes the shell uses to say "this surface is not the loopback dashboard".
#
# Why these and not a general "is this `return` at top level" analysis: I wrote
# that first, by counting braces over a comment/string-stripped copy, and it
# reported three false positives in two files. Regex literals containing quotes
# or braces throw the count off, and getting it right needs a JS parser. A
# structural check that cries wolf is deleted by the next person to see it fail
# wrongly, so this one is narrow and exact instead: it asks the question that
# actually matters — does this module opt out of non-live modes at all?
MODE_GUARD_PATTERNS = (
    re.compile(r'mode\s*!==\s*"live"'),
    re.compile(r'\bWAVR_CORE\b'),
    re.compile(r'has\("core"\)'),
)


def _mode_guards(src: str, before: int | None = None) -> list[str]:
    """Which "not this surface" bail-outs a module carries, before `before`."""
    window = src[:before] if before is not None else src
    return [m.group(0) for p in MODE_GUARD_PATTERNS for m in p.finditer(window)]


def test_the_writer_is_published_from_the_runtime_module():
    src = _js(RUNTIME)
    assert "window.__wavrShowSpace" in src, (
        "the Space-name writer is not published from runtime.js")
    assert "function showSpaceName(" in src


def test_the_publishing_module_does_not_opt_out_of_any_mode():
    """The whole point. An export below an early return is not an export, and
    the wizard's early return is a mode guard."""
    src = _js(RUNTIME)
    bad = _mode_guards(src, src.index("window.__wavrShowSpace"))
    assert not bad, (
        f"runtime.js checks the surface mode above the export: {bad}. If it "
        f"bails out on any of these, a paired phone or the kiosk loses the "
        f"Space name — which is the bug that moved the writer here.")


def test_the_wizard_no_longer_owns_the_writer():
    src = _js(WIZARD)
    assert "function showSpaceName(" not in src, (
        "the writer is back inside the wizard, which returns early for every "
        "mode but 'live' — a paired phone and the kiosk would lose the name.")
    assert "__wavrShowSpace" in src, "the wizard must call the shared writer"


def test_the_wizard_still_bails_out_early_for_non_live_modes():
    """Not a style check: it is WHY the writer had to move. If this guard ever
    goes away the reasoning above stops holding, and somebody should notice
    here rather than by moving the writer back."""
    assert re.search(r"if \(mode !== \"live\"\) return;", _js(WIZARD)), (
        "the wizard's mode guard changed — re-read whether the Space-name "
        "writer still needs to live outside it.")


def test_nobody_else_writes_the_wordmark_slot():
    offenders = []
    for p in (SHELL, WIZARD):
        for m in re.finditer(r'.*brandSpace.*', _js(p)):
            line = m.group(0)
            if "//" in line.split("brandSpace")[0]:
                continue                      # a comment mentioning it
            if "textContent" in line or "innerHTML" in line or "innerText" in line:
                offenders.append(f"{p.name}: {line.strip()[:100]}")
    assert not offenders, (
        "these write the Space name directly instead of calling "
        f"window.__wavrShowSpace:\n  " + "\n  ".join(offenders))


def test_the_writer_sets_the_title_as_well_as_the_slot():
    """Half the bug was a caller that set one and not the other."""
    src = _js(RUNTIME)
    body = src[src.index("function showSpaceName("):]
    body = body[:body.index("\n  }")]
    assert "brandSpace" in body
    assert "document.title" in body, (
        "renaming a Space must change the browser tab too, or a second tab on "
        "a second Core stays indistinguishable.")


def test_the_status_poll_feeds_the_writer():
    """/api/setup/status is loopback-root only, so it cannot be the answer for
    a companion. /api/status carries presence:read and every companion holds
    it — if this call goes away, phones silently lose the name again.

    Searched across the shell AND its modules: the `/api/status` poll now lives
    in `js/status-panel.js`, and reading index.html alone would report that a
    call which still runs on every companion had been deleted.
    """
    from tests.frontend_source import ALL, module_holding
    assert "__wavrShowSpace(s.space)" in ALL, (
        "the /api/status poll no longer feeds the Space name, so a paired "
        "phone has no way to learn which Space it is looking at.")
    # One producer, still. Two pollers writing the name is how the two answers
    # start to differ, and this module exists because they once did.
    feeders = [n for n, t in
               __import__("tests.frontend_source",
                          fromlist=["MODULES"]).MODULES.items()
               if "__wavrShowSpace(s.space)" in t]
    assert feeders == ["status-panel.js", "wizard.js"], (
        f"the /api/status Space-name feed moved or multiplied: {feeders}. "
        f"status-panel.js is the poll; wizard.js writes it once at the end of "
        f"first-run setup, before any poll has happened.")
    assert module_holding("__wavrShowSpace(s.space)") == "status-panel.js"


def test_the_detector_actually_detects_the_shape_it_is_looking_for():
    """A structural check that can never fire is decoration. The wizard is a
    live example of the shape being guarded against — both of them, in fact:
    the non-live bail-out and the kiosk one. Point the detector at it and
    require hits, or the clean bill of health above means nothing."""
    hits = _mode_guards(_js(WIZARD))
    assert hits, (
        "the detector found no mode guard in wizard.js, which has two — so it "
        "would not catch one in runtime.js either.")
    assert any('live' in h for h in hits), f"missed the live guard: {hits}"
    assert any('core' in h.lower() for h in hits), f"missed the kiosk: {hits}"
