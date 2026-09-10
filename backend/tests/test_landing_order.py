"""The landing surface answers the question the arrival is actually asking.

It used to answer them in a fixed sequence, cards first and the Space last:

  1. **Is there anything I have to deal with?**  `attnTile`
  2. **Is everything okay?**                     `houseStatusTile`
  3. **How much is being sensed right now?**     `sensingLevelTile`
  4. **Who is home?**                            `qcTile`
  5. **Where are they?**                         the room cards
  6. **Show me.**                                the map

That reading is right about one thing and wrong about the rest of it. It is
right that what needs a person must never sit below the fold — the original
version of this file warned, in as many words, about "a tile moved up because
it looks better in a screenshot, and six months later the first thing a
household sees is a 3D house and the thing that needs them is below the fold".

It is wrong that the sequence is fixed, because the questions are not equally
likely. On the overwhelming majority of days nothing needs a decision, and on
those days the fixed order made a household scroll past a status card, a
configuration panel and a roster to reach the representation of their own home.
On a maximised laptop the Space opened below the fold, under sensor settings,
in a product whose entire claim is that it IS the Space.

So the order is conditional now, which answers both concerns instead of picking
one:

  * `attnTile` is still FIRST, and still above the Space. It renders `hidden`
    unless something is actually waiting, so on the day it matters it is the
    first thing on the screen — exactly what the old first test protected.
  * On every other day it is not in the document at all, and the Space is what
    opens: the map, the room strip under it, and the column beside it that
    qualifies what the map is showing.

`test_what_needs_a_person_is_first` is unchanged and still passing, because
that part was never the problem.
"""
from __future__ import annotations

import re
from pathlib import Path

SHELL = Path(__file__).resolve().parents[2] / "frontend" / "index.html"

# In order. The Space leads; the things that qualify it follow. `None` marks a
# card with no id — the rooms column — matched by position rather than by name.
EXPECTED = ["attnTile", "radarWrap", None, "qcTile", "houseStatusTile",
            "sensingLevelTile"]

DIV = re.compile(r'<div\b[^>]*\bclass="([^"]*)"[^>]*>')


def _panel() -> str:
    """Only the Space panel. Every other tab has tiles of its own, and a scan
    over the whole shell answers a different question than the one asked here."""
    text = SHELL.read_text(encoding="utf-8")
    parts = re.split(r'<section class="tab-panel[^"]*"[^>]*?id="(panel-[a-z]+)"',
                     text)
    body = {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}["panel-inicio"]
    cut = body.find('<div class="settings-section')
    return body[:cut] if cut != -1 else body


def _cards():
    out = []
    for m in DIV.finditer(_panel()):
        classes = m.group(1).split()
        if "tile" not in classes or "hidden" in classes:
            continue
        got = re.search(r'id="([^"]+)"', m.group(0))
        out.append(got.group(1) if got else None)
    return out


def test_the_landing_surface_leads_with_the_space():
    got = _cards()
    assert got == EXPECTED, (
        f"the landing order changed.\n  expected {EXPECTED}\n  found    {got}\n"
        f"\nThe order is the design: anything that needs a person, then the "
        f"Space itself, then the rooms in it, then what qualifies them. "
        f"Changing it is allowed; changing it silently is how the thing that "
        f"needs somebody ends up below the fold — or how the product goes back "
        f"to opening on its own settings.")


def test_what_needs_a_person_is_first():
    """Stated separately from the full order because it is the one that
    matters most, and because a reshuffle will be tempted to justify itself by
    keeping 'roughly' the same order. It survived the Space moving up: the
    attention tile is still the first card in the document, and still renders
    above the stage."""
    got = _cards()
    assert got and got[0] == "attnTile", (
        f"the first thing on the landing surface is {got[0]!r}, not the inbox")


def test_the_space_is_not_below_the_configuration():
    """The defect this order exists to prevent, stated as the thing itself.

    Whatever else moves, the map may not end up underneath the sensing-level
    control again: that is the arrangement that had a laptop open on sensor
    configuration with the household's own home somewhere further down.
    """
    got = _cards()
    assert "radarWrap" in got and "sensingLevelTile" in got, got
    assert got.index("radarWrap") < got.index("sensingLevelTile"), (
        "the Space is below the sensing control again")


def test_the_scan_still_finds_the_cards():
    assert len(_cards()) >= 4, "the card scan found almost nothing"
