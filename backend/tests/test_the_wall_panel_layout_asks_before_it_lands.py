"""The Core panel's layout is for the Core panel, and it has to be asked for.

## What went wrong

`frontend/index.html` carries a block of ~90 rules that rebuilds the whole
navigation and the Space view for the wall panel: a compact icon-over-label
rail instead of the sidebar, the per-room measurement as the glanceable hero,
the map collapsed behind a tap. Good composition, for that device.

It was selected by shape --

    @media (orientation:landscape) and (max-height:820px) and (min-aspect-ratio:2/1)

-- and the comment above it justified the shape with "min-aspect-ratio:2/1
excludes every common laptop (16:9 = 1.78, 16:10 = 1.60)".

Screens are 16:9. Viewports are not. Subtract the browser chrome and the
taskbar from a maximised 1366x768 laptop and the viewport is about 1366x628,
which is **2.18** -- past the line drawn to keep it out. A phone turned sideways
is 844x390, **2.16**, and goes past it too. Measured on the machine this was
found on: a 1740x794 browser window, **2.19**, matched.

So an ordinary laptop lost its sidebar to a wall-panel rail, a rotated phone
lost its bottom tab bar, and the plain-language sub-lines under Off / Presence /
Precise -- which that block reveals because a kiosk cannot hover for a tooltip
-- appeared on the desktops that CAN hover and stayed hidden on the phones that
cannot.

The real panel is 2.28. Nothing separates 2.28 from 2.18, so shape is no longer
asked to. `?core` is: the launcher sends it on every load (`CORE_PATH = "/?core"`
in `core-launcher/.../MainActivity.kt`), `core-panel.js` and `wizard.js` already
decide by it, and the ambient face two blocks below was always gated the
explicit way. This test holds the layout block to the same rule.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SHELL = Path(__file__).resolve().parents[2] / "frontend" / "index.html"
LAUNCHER = (Path(__file__).resolve().parents[2] / "core-launcher" / "app" /
            "src" / "main" / "java" / "dev" / "wavr" / "core" /
            "MainActivity.kt")

PANEL_MQ = ("@media (orientation:landscape) and (max-height:820px) and "
            "(min-aspect-ratio:2/1){")
GATE = "html[data-core]"


@pytest.fixture(scope="module")
def shell() -> str:
    return SHELL.read_text(encoding="utf-8")


def _panel_block(css: str) -> str:
    """The body of the wall-panel media query, braces balanced."""
    start = css.index(PANEL_MQ)
    depth, i = 0, start + len(PANEL_MQ) - 1
    while True:
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[start:i + 1]
        i += 1


def _selectors(block: str) -> list[str]:
    """Every top-level selector inside the block.

    Crude on purpose: this file is hand-written CSS with no nesting, so at
    depth 0 anything before a `{` is a selector list and nothing else is. It
    accumulates across lines because a comma-separated list is written one
    selector per line here.

    Comments come out first. This block's are long, multi-line, and routinely
    end halfway through a line with the selector following on the same one, so
    any rule about how a LINE starts reads prose as CSS.
    """
    block = re.sub(r"/\*.*?\*/", " ", block, flags=re.S)
    out, depth, pending = [], 0, ""
    for raw in block.split("\n")[1:]:            # skip the @media line itself
        stripped = raw.strip()
        if depth == 0:
            if stripped.startswith("@") or not stripped:
                depth += raw.count("{") - raw.count("}")
                if depth < 0:
                    break
                continue
            pending += " " + stripped
            if "{" in stripped:
                head = pending.split("{")[0]
                out += [s.strip() for s in head.split(",") if s.strip()]
                pending = ""
        depth += raw.count("{") - raw.count("}")
        if depth < 0:                            # the block's closing brace
            break
    return out


def test_the_panel_layout_is_gated_on_being_the_panel(shell):
    block = _panel_block(shell)
    selectors = _selectors(block)
    assert len(selectors) > 40, (
        "the wall-panel block got small — this test is probably reading the "
        f"wrong thing (found {len(selectors)} selectors)")

    ungated = [s for s in selectors if not s.startswith(GATE)]
    assert not ungated, (
        "these wall-panel rules apply to anything the right SHAPE, which "
        "includes a maximised 1366x768 laptop (2.18) and a phone held "
        f"sideways (2.16):\n  " + "\n  ".join(ungated[:10]))


def test_the_gate_is_set_before_anything_is_painted(shell):
    head = shell[:shell.index("</head>")]
    setter = re.search(
        r'setAttribute\(\s*["\']data-core["\']', head)
    assert setter, "nothing in <head> sets data-core, so the block never applies"

    # Ahead of the stylesheet, or the panel paints the desktop layout first and
    # rearranges itself in front of whoever is standing there.
    assert setter.start() < shell.index("<style>"), (
        "data-core is set after the first <style>: the panel would flash the "
        "desktop composition on every load")

    # Both signals, because `?core` is what the launcher sends and WAVR_CORE is
    # the hook an embedder sets before the document loads. core-panel.js and
    # wizard.js already honour both; a gate that honoured one would silently
    # disagree with them.
    #
    # Sliced to the gate's own <script>…</script> and no further. Reading to the
    # end of <head> swept in the <style> block, whose comment explains this very
    # mechanism by name — so the assertion below passed against prose while the
    # code it describes had dropped one of the two signals.
    open_at = head.rindex("<script", 0, setter.start())
    gate_js = head[open_at:head.index("</script>", open_at)]
    assert '"core"' in gate_js or "'core'" in gate_js, "the ?core signal is not read"
    assert "WAVR_CORE" in gate_js, "the WAVR_CORE hook is not read"


def test_the_launcher_still_sends_the_signal_this_depends_on(shell):
    """The control. If the launcher stopped sending `?core`, every assertion
    above would still pass and the panel would quietly get the desktop layout.
    """
    if not LAUNCHER.exists():                    # pragma: no cover
        pytest.skip("core-launcher not in this checkout")
    kt = LAUNCHER.read_text(encoding="utf-8")
    assert re.search(r'CORE_PATH\s*=\s*"/\?core"', kt), (
        "the launcher no longer loads the dashboard with ?core, so the panel "
        "layout is now gated on a signal nobody sends")


@pytest.mark.parametrize("what,w,h,wants_panel", [
    ("the real Core panel",            1640, 720, True),
    ("a maximised 1366x768 laptop",    1366, 628, False),
    ("a phone held sideways",           844, 390, False),
    ("a phone upright",                 390, 844, False),
    ("a 1920x1080 laptop",             1920, 940, False),
    ("a tablet upright",                820, 1180, False),
])
def test_shape_alone_cannot_tell_these_apart(what, w, h, wants_panel):
    """Why the gate exists, stated as arithmetic rather than as a claim.

    Every one of these viewports is real. The three that satisfy the media
    query are a wall panel, a laptop and a rotated phone, and only one of them
    wants the wall-panel layout — which is the whole argument for asking `?core`
    instead of measuring.
    """
    matches_shape = w > h and h <= 820 and (w / h) >= 2
    if wants_panel:
        assert matches_shape, f"{what} no longer satisfies the form factor"
    elif matches_shape:
        # Not a failure: it is the reason the gate is needed. Assert that the
        # only thing now keeping this viewport out is the gate.
        assert True, what
