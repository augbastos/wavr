"""Python and JavaScript must say the same duration about the same reading.

A runtime finding ships a duration as SECONDS, because "3 minutes" composed in
Python is an English fragment that would land in the middle of a Portuguese
sentence. So there are two humanisers by design: `runtime_status._human` for
the tray, the CLI and the Android notification, and `runtime.js humanAge` for
the browser.

Two implementations of one answer is exactly the shape this module's own
docstring opens by forbidding — "two implementations of 'is it healthy'
eventually disagree in front of somebody with no way to tell which is right".
They cannot be collapsed into one, because one of them has to run where the
catalogue is; what they CAN be is held to the same arithmetic.

They were not. `_human` floors (`int(seconds)`, then `//`); `humanAge` rounded.
At 90 seconds the tray said "1 minute" and the browser said "2 minutes" about
the same reading.

## Why this test is structural

The honest test would render both and compare, and the two do not meet
anywhere a test can stand: one is composed server-side into `text` for
consumers that never reach a browser, the other is composed in a closure inside
an IIFE. So this reads the two functions and requires the arithmetic to match —
weaker than a behavioural check, and much better than the comment that was
there instead. It fails the moment somebody changes one side.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
RUNTIME_JS = FRONTEND / "js" / "runtime.js"

# The boundaries both sides switch on, in seconds.
BOUNDARIES = (60, 3600, 86400)


def _js_human_age() -> str:
    src = RUNTIME_JS.read_text(encoding="utf-8")
    m = re.search(r"function humanAge\(seconds\)\s*\{(.*?)\n  \}", src, re.S)
    assert m, "humanAge moved or changed shape in frontend/js/runtime.js"
    return m.group(1)


def _py_human() -> str:
    from wavr import runtime_status
    return inspect.getsource(runtime_status._human)


def test_both_humanisers_floor_rather_than_round():
    """`Math.round` on one side and `//` on the other is a one-unit
    disagreement at every boundary, on every surface, for ever."""
    js = _js_human_age()
    assert "Math.round" not in js, (
        "humanAge rounds while `runtime_status._human` floors, so the browser "
        "and the tray state different durations for the same reading:\n" + js)
    assert "Math.ceil" not in js, js
    assert js.count("Math.floor") >= 4, (
        "humanAge should floor the seconds and each of the three larger "
        "units, the way `_human` does:\n" + js)

    py = _py_human()
    assert "round(" not in py, py
    assert "int(seconds)" in py and "//" in py, py


def test_both_humanisers_switch_at_the_same_boundaries():
    """A minute that starts at 59 on one side is the same defect in a
    different place."""
    js = _js_human_age()
    py = _py_human()
    for boundary in BOUNDARIES:
        assert str(boundary) in js, f"{boundary} is not a boundary in humanAge"
        assert str(boundary) in py, f"{boundary} is not a boundary in _human"


def test_the_python_side_still_produces_words_for_the_tray():
    """The reason two humanisers exist at all: `text` is read by consumers that
    translate nothing, and handing them a bare number would be the same bug
    pointing the other way."""
    from wavr.runtime_status import _human

    assert _human(1) == "1 second"
    assert _human(59) == "59 seconds"
    assert _human(60) == "1 minute"
    assert _human(90) == "1 minute", "floors, so 90s is one minute and not two"
    assert _human(7200) == "2 hours"
    assert _human(172800) == "2 days"
