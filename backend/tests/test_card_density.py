"""No surface may become a wall of identical cards.

§42: cards only where they improve grouping — tables where comparison matters,
lists where scanning matters, side sheets for detail.

## Two measurements that were wrong before this one

``\\btile\\b`` against the raw class attribute also matches ``tile-head`` and
``tile-body``, because a hyphen is a word boundary — it counts a card's own
header as another card, and roughly doubles every number. And counting the
Transparency tab's markup says thirty-three, which is meaningless: that tab
contains the whole settings overlay, and the overlay shows ONE section at a
time.

Measured as a person actually sees it — class LIST tokens, per visible
surface — the densest tab carries eight and nothing else passes six.

The ceilings below are therefore set AT what exists, not above it. Growing past
one is not forbidden; it is a decision somebody makes deliberately by raising
the number here, with the question "should this be a table or a list instead?"
attached to it.
"""
from __future__ import annotations

import re
from pathlib import Path

SHELL = Path(__file__).resolve().parents[2] / "frontend" / "index.html"

DIV = re.compile(r'<div\b[^>]*\bclass="([^"]*)"[^>]*>')

# Surface -> the most simultaneously-visible cards it may carry.
CEILINGS = {
    # Six now, and the sixth was earned by a reorganisation rather than added:
    # "Who's home" moved here from its own primary tab, because it answers WHO
    # and the room column beside it answers WHERE. Its companion "Rooms" tile
    # came with it and was DROPPED -- that tile listed every room as occupied
    # or empty, which is the room column's whole job.
    "panel-inicio": 6,
    # The outlier, and known: four of these eight answer "is it working?"
    # (healthCheck, doctorCheck, statusPanel, diagPanel). Consolidating them
    # is a product decision, not a refactor — the ceiling holds the line
    # meanwhile so it cannot quietly become nine.
    "panel-sistema": 8,
    # Eight. The eighth is "Use Wavr on your phone", and it was earned by a
    # person failing to find what it says.
    #
    # This is the only screen in the navigation whose name suggests connecting
    # a phone, and what it offered was a footnote: 0.78rem dim text at the
    # bottom of a card about sensor onboarding, below a divider. The first user
    # scanned it and wrote "eu nem sei aonde fica essa tela". Asked of this
    # one, the question the ceiling exists to force -- should it be a table or
    # a list instead? -- answers itself: it is a single call to action with
    # nothing to compare it against.
    #
    # Seven was: "New devices" moved here from its own primary tab. "Is there
    # something on my Wi-Fi I do not recognise?" is a device-management
    # question, and its actionable half already reaches a person through
    # Needs Attention.
    "panel-dispositivos": 8,
    "panel-rede": 5,
    "panel-historico": 3,
    "panel-transparencia": 3,
    "panel-rotinas": 1,
    "panel-discoveries": 1,
}
SECTION_CEILING = 5      # any one settings section


def _cards(body: str) -> list[re.Match[str]]:
    return [m for m in DIV.finditer(body) if "tile" in m.group(1).split()]


# Cards that are one card in two states: at most one of each pair is ever on
# screen, so counting both overstates what a person is looking at.
#
# There is exactly one pair, and it earns the exception by being mutually
# exclusive in code rather than by convention: `renderPairing()` shows
# `#pairingOff` when other devices cannot connect and hides it the moment they
# can. `test_only_one_of_a_two_state_card_can_be_shown` below holds that, so
# this list cannot become a place to park a card that really is a sixth one.
DUAS_FACES = [{"pairing", "pairingOff"}]

ID = re.compile(r'\bid="([^"]+)"')


def _visible(body: str) -> int:
    ids: list[str] = []
    soltos = 0
    for m in _cards(body):
        if "hidden" in m.group(1).split():
            continue
        nome = ID.search(m.group(0))
        if nome:
            ids.append(nome.group(1))
        else:
            soltos += 1
    total = soltos + len(ids)
    for par in DUAS_FACES:
        presentes = par & set(ids)
        if len(presentes) > 1:
            total -= len(presentes) - 1
    return total


def _own_body(body: str) -> str:
    """A tab's own body stops where the settings overlay begins."""
    cut = body.find('<div class="settings-section')
    return body[:cut] if cut != -1 else body


def _nav_tabs() -> list[str]:
    """Every tab a person can reach, from BOTH navigation levels.

    Reading only `.nav-tabs` would have quietly stopped covering the five
    screens that moved down into Manage. A density guard that silently narrows
    to the three surfaces least likely to sprawl is worse than no guard,
    because the report still claims everything is measured.
    """
    text = SHELL.read_text(encoding="utf-8")
    found: list[str] = []
    for cls in ("nav-tabs", "nav-sub"):
        bar = re.search(r'<div class="' + cls + r'"[^>]*>.*?\n    </div>',
                        text, re.S)
        assert bar, f"the {cls} level moved — this file measures nothing"
        found += re.findall(r'<button[^>]*data-tab="([a-z]+)"', bar.group(0))
    assert len(found) >= 8, f"only found {found} across both levels"
    return found


def _tab_bodies() -> dict[str, str]:
    text = SHELL.read_text(encoding="utf-8")
    # `data-tab` sits between class and id on at least one panel, so the id
    # cannot be assumed to follow the class attribute directly. Getting this
    # wrong once already hid the Discoveries tab from every check below.
    parts = re.split(r'<section class="tab-panel[^"]*"[^>]*?id="(panel-[a-z]+)"',
                     text)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def _section_bodies() -> dict[str, str]:
    text = SHELL.read_text(encoding="utf-8")
    parts = re.split(r'<div class="settings-section[^"]*" id="(gearSec[A-Za-z]+)"',
                     text)
    return {parts[i]: _own_body(parts[i + 1]) for i in range(1, len(parts), 2)}


def test_no_tab_exceeds_its_card_ceiling():
    over = []
    for name, body in _tab_bodies().items():
        count = _visible(_own_body(body))
        ceiling = CEILINGS.get(name)
        if ceiling is not None and count > ceiling:
            over.append(f"{name}: {count} > {ceiling}")
    assert not over, (
        "these surfaces grew past their card ceiling:\n  " + "\n  ".join(over)
        + "\n\nBefore raising a ceiling, ask whether the new information wants "
          "to be a card at all — a table compares, a list scans, a card groups.")


def test_every_clickable_tab_has_a_ceiling():
    """A new tab with no entry here would be exempt from the rule for as long
    as nobody noticed — which is how a density rule dies. Driven off the nav
    bar rather than off the panels, so a panel the body-regex fails to find
    still fails this test instead of silently going unmeasured."""
    clickable = {"panel-" + t for t in _nav_tabs()}
    assert clickable, "found no tabs in the nav bar"
    missing = sorted(clickable - set(CEILINGS))
    assert not missing, (
        f"these tabs have no card ceiling: {missing}. Measure what each "
        f"carries and add it to CEILINGS.")


def test_every_clickable_tab_has_a_body_the_measurement_can_find():
    clickable = {"panel-" + t for t in _nav_tabs()}
    unfound = sorted(clickable - set(_tab_bodies()))
    assert not unfound, (
        f"the nav offers {unfound} but the body regex cannot find them, so "
        f"their density is never measured.")


def test_no_settings_section_becomes_a_wall():
    over = [f"{n}: {c}" for n, b in _section_bodies().items()
            if (c := _visible(b)) > SECTION_CEILING]
    assert not over, f"settings sections past {SECTION_CEILING} cards: {over}"


def test_only_one_of_a_two_state_card_can_be_shown():
    """`DUAS_FACES` is an exemption, so it has to be earned in code.

    Discounting a card because "it never appears at the same time as that other
    one" is exactly the sort of claim that is true when written and false a
    month later — at which point the ceiling has quietly been raised by one and
    nothing says so. The pairing panel earns it: the function that reveals
    either one hides the other, and this reads that function.
    """
    fonte = (SHELL.parent / "js" / "pairing.js").read_text(encoding="utf-8")
    # Comments in this file quote both ids while explaining the arrangement, so
    # the prose has to go before the code is read — otherwise the explanation
    # satisfies the check.
    codigo = re.sub(r"^\s*//.*$", "", re.sub(r"/\*.*?\*/", " ", fonte, flags=re.S),
                    flags=re.M)
    assert '_off.hidden = true' in codigo or '_off.hidden=true' in codigo, (
        "nothing hides #pairingOff when pairing becomes possible, so both "
        "halves of the pair can be on screen at once and the density "
        "exemption in DUAS_FACES is false")
    assert "_pairingUnavailable" in codigo, (
        "nothing reveals #pairingOff, so the Devices screen is silent again "
        "when other devices cannot connect")


def test_the_counter_does_not_count_a_card_header_as_a_card():
    """The bug this file was written around, pinned so it cannot come back."""
    assert _visible('<div class="tile-head">x</div>') == 0
    assert _visible('<div class="tile">x</div>') == 1
    assert _visible('<div class="tile hidden">x</div>') == 0
    assert _visible('<div class="tile rooms-col">x</div>') == 1
    # The two-state pair counts once; two unrelated cards still count twice.
    par = ('<div class="tile" id="pairing">a</div>'
           '<div class="tile" id="pairingOff">b</div>')
    assert _visible(par) == 1
    assert _visible('<div class="tile" id="a">x</div>'
                    '<div class="tile" id="b">y</div>') == 2


def test_the_measurement_still_finds_the_surfaces_it_measures():
    """A regex that silently stops matching makes every test above pass
    forever, which is the failure mode of every structural check."""
    tabs, sections = _tab_bodies(), _section_bodies()
    assert len(tabs) >= 8, f"only found tabs: {sorted(tabs)}"
    assert len(sections) >= 8, f"only found sections: {sorted(sections)}"
    assert sum(_visible(_own_body(b)) for b in tabs.values()) >= 20
