"""The landing surface answers questions in the order a person asks them.

Somebody opening Wavr arrives with a sequence, and the screen either matches it
or makes them hunt:

  1. **Is there anything I have to deal with?**  `attnTile`
  2. **Is everything okay?**                     `houseStatusTile`
  3. **How much is being sensed right now?**     `sensingLevelTile`
  4. **Who is home?**                            `qcTile`
  5. **Where are they?**                         the room cards
  6. **Show me.**                                the map

Step 4 arrived when "Who's home" stopped being its own primary tab. It sits
immediately above the room column on purpose: WHO and WHERE are one question
asked twice, and a person should not have to hold one screen in their head to
read the other.

The redesign put them in that order. Nothing kept them there, and order is
exactly the property that erodes: a tile gets added at the bottom because that
is where the markup ends, or moved up because it looks better in a screenshot,
and six months later the first thing a household sees is a 3D house and the
thing that needs them is below the fold.

This is a ceiling-style guard, not a design opinion — changing the order is
allowed, and requires changing it here, which is where the question "does this
still match how somebody actually arrives?" gets asked.
"""
from __future__ import annotations

import re
from pathlib import Path

SHELL = Path(__file__).resolve().parents[2] / "frontend" / "index.html"

# In order. `None` marks a card with no id — the rooms column — matched by
# position rather than by name.
EXPECTED = ["attnTile", "houseStatusTile", "sensingLevelTile", "qcTile",
            None, "radarWrap"]

DIV = re.compile(r'<div\b[^>]*\bclass="([^"]*)"[^>]*>')


def _panel() -> str:
    text = SHELL.read_text(encoding="utf-8")
    parts = re.split(r'<section class="tab-panel[^"]*"[^>]*?id="(panel-[a-z]+)"',
                     text)
    body = {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}["panel-inicio"]
    cut = body.find('<div class="settings-section')
    return body[:cut] if cut != -1 else body


def _cards() -> list[str | None]:
    out = []
    for m in DIV.finditer(_panel()):
        classes = m.group(1).split()
        if "tile" not in classes or "hidden" in classes:
            continue
        got = re.search(r'id="([^"]+)"', m.group(0))
        out.append(got.group(1) if got else None)
    return out


def test_the_landing_surface_asks_in_the_order_people_arrive():
    got = _cards()
    assert got == EXPECTED, (
        f"the landing order changed.\n  expected {EXPECTED}\n  found    {got}\n"
        f"\nThe order is the design: what needs a person, then whether "
        f"everything is okay, then how much is being sensed, then who is "
        f"where, then the map. Changing it is allowed; changing it silently "
        f"is how the thing that needs somebody ends up below the fold.")


def test_what_needs_a_person_is_first():
    """Stated separately from the full order because it is the one that
    matters most, and because a future reshuffle will be tempted to justify
    itself by keeping 'roughly' the same order."""
    got = _cards()
    assert got and got[0] == "attnTile", (
        f"the first thing on the landing surface is {got[0]!r}, not the inbox")


def test_the_map_is_last_not_first():
    """It is the most striking thing on the screen and the least useful answer
    to 'is anything wrong'. It earns its place by being the thing you look at
    once the questions above are answered."""
    got = _cards()
    assert got and got[-1] == "radarWrap", (
        f"the map is no longer last; the surface ends with {got[-1]!r}")


def test_the_scan_still_finds_the_cards():
    assert len(_cards()) >= 4, "the card scan found almost nothing"
