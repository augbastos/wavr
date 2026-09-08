"""One concept, one word, on every surface a person reads.

A documented vocabulary that nothing checks is a style guide, and style guides
lose to whoever is typing. What CAN be checked mechanically is checked here.

The rule underneath all of it: **the producer names a state once, and every
surface renders that name.** A surface that derives its own word has invented a
second vocabulary, and the two will eventually disagree in front of somebody who
has no way to tell which is right.
"""
from __future__ import annotations

import re
from pathlib import Path

from wavr import runtime_status as rs

REPO = Path(__file__).resolve().parents[2]
TRAY = REPO / "desktop" / "src-tauri" / "src" / "main.rs"
CHIP = REPO / "frontend" / "js" / "runtime.js"
CLI = REPO / "backend" / "wavr" / "status.py"
DOC = REPO / "docs" / "VOCABULARY.md"

# Every state the one producer can emit. A surface that does not handle one of
# these renders it as a raw identifier, or as nothing.
STATES = set(rs.SEVERITY) | {rs.STARTING}


def test_the_producer_emits_exactly_the_states_the_vocabulary_documents():
    """The doc is the contract for humans; this keeps it honest about the code.

    A state added without a word is a state whose first appearance is a raw
    identifier in front of a household.
    """
    documented = set(re.findall(r"^\| `([a-z]+)` \|", DOC.read_text(encoding="utf-8"),
                                re.M))
    assert documented == STATES, (
        f"only in the doc: {sorted(documented - STATES)}; "
        f"only in the code: {sorted(STATES - documented)}")


def test_the_shell_chip_has_a_word_for_every_state():
    """`LABEL` in `runtime.js`. A missing entry falls through to the raw state
    name, so `unavailable` would appear in the chrome as the word
    "unavailable" rather than "Not responding"."""
    text = CHIP.read_text(encoding="utf-8")
    block = text[text.index("var LABEL = {"):]
    block = block[:block.index("};")]
    labelled = set(re.findall(r"^\s*([a-z]+):", block, re.M))
    assert STATES <= labelled, f"the chip has no word for: {sorted(STATES - labelled)}"


def test_the_cli_has_a_mark_for_every_state():
    """`_MARK` in `wavr/status.py`. A state with no mark falls back to a
    bullet, which reads as healthy.

    Named for the file it reads. This was called
    `test_the_tray_has_a_mark_for_every_state`, and said "the CLI and the
    tray's own rendering both key on these" — the tray is Rust and cannot see
    this dictionary at all. The tray's own table is checked below; until this
    was split, it was checked nowhere.
    """
    cli_text = CLI.read_text(encoding="utf-8")
    block = cli_text[cli_text.index("_MARK = {"):]
    block = block[:block.index("}")]
    marked = set(re.findall(r'"([a-z]+)":', block))
    assert STATES <= marked, f"the CLI has no mark for: {sorted(STATES - marked)}"


def _tray_words() -> set[str]:
    """The states the tray menu has a sentence of its own for.

    `tray_view()` in `desktop/src-tauri/src/main.rs` renders the menu's status
    line with `let summary = match state { … }`. Only the arms naming a state
    count: the arm below them is `other => other.to_string()`, which is the
    wire value, not a word.
    """
    text = TRAY.read_text(encoding="utf-8")
    block = text[text.index("let summary = match state {"):]
    block = block[:block.index("\n    };")]
    return set(re.findall(r'^\s*"([a-z]+)"\s*=>', block, re.M))


def test_the_trays_own_table_is_still_where_this_reads_it():
    """A guard on the guard below.

    `xfail` swallows an exception as neatly as it swallows a failed assertion,
    so the day `tray_view` is restructured and this parse stops finding the
    match block, the check under it would go quiet rather than red.
    """
    assert "healthy" in _tray_words(), (
        "the tray's `match state` block is not where this reads it — the "
        "check below is parsing nothing and cannot fail")


def test_the_tray_has_a_word_for_every_state():
    """The tray must not put a wire value on a menu line.

    The tray's summary is `match state`: `healthy` has a sentence, anything
    else takes the worst finding's text if there is one, and otherwise renders
    the state identifier itself. Two of those states reached a household with
    no finding to borrow:

      * `starting` — every launch, before the Core has assessed anything. The
        menu read "starting", which is not a sentence and not this product's
        vocabulary. It was the first thing this product said, every time.
      * `unavailable` from a Core that answers but has nothing to report.

    "offline" is already refused on the sensor path by
    `test_a_person_is_never_shown_the_raw_wire_value` below; the tray is the
    same rule on the surface a person sees without opening anything.

    This carried a strict `xfail` describing the fix. The fix is now in
    `tray_view`: an arm per state, in the words `docs/VOCABULARY.md` already
    fixes, in this surface's idiom — not a lookup that decides the state, which
    would make it a second producer (see
    `test_no_surface_invents_its_own_health_verdict`). The Core's own finding
    text still wins whenever there is one; these arms are only the fallback
    that used to be an identifier.
    """
    worded = _tray_words()
    assert STATES <= worded, (
        f"the tray has no word for: {sorted(STATES - worded)}. Each of those "
        f"reaches the menu as the raw wire value.")


def test_both_languages_name_the_pseudo_room_the_same_thing():
    """`casa` is the whole-building scope, and both halves of the product have
    to agree on the string, because both filter on it.

    The JavaScript learned this the hard way: four modules wrote the literal by
    hand and the fifth forgot, which put a card named after a place nobody has
    on the Space tab. The constant moved into `shared.js`. Python kept writing
    the literal in five places, and `sensor_coverage` was the reader that
    forgot — see `test_the_house_is_not_a_room.py`.
    """
    from wavr.events import HOUSE_ROOM

    shared = (REPO / "frontend" / "js" / "shared.js").read_text(encoding="utf-8")
    found = re.search(r'const HOUSE_ROOM = "([^"]+)"', shared)
    assert found, "shared.js no longer declares HOUSE_ROOM where this reads it"
    assert found.group(1) == HOUSE_ROOM, (
        f"the browser filters {found.group(1)!r} and the Core reports "
        f"{HOUSE_ROOM!r}, so one of them is showing the other's pseudo-room")


def test_no_surface_invents_its_own_health_verdict():
    """Each of these renders `runtime_status`'s answer. If one of them starts
    computing health from raw parts, it has become a second producer.

    Checked by absence of the two things a second producer needs: a staleness
    threshold of its own, and its own idea of what "healthy" means.
    """
    for path, label in ((TRAY, "the tray"), (CHIP, "the shell chip")):
        text = path.read_text(encoding="utf-8")
        # A surface deciding freshness for itself would need a threshold.
        assert "STALE_AFTER" not in text, (
            f"{label} has its own staleness threshold — the Core decides that")
        # And it must not conclude "healthy" from anything but the field.
        assert not re.search(r'(state|status)\s*=\s*["\']healthy["\']', text), (
            f"{label} assigns `healthy` itself rather than rendering it")


def test_the_three_surfaces_say_the_same_thing_about_silence():
    """`unreachable()` says it exists "so a tray, a menu bar and a browser tab
    cannot disagree about what 'I got no answer' means".

    Nothing enforced that. The tray is Rust and the chip is JavaScript; neither
    can call a Python function, so the only thing that can hold the claim is a
    check that reads all three. Until this existed, the tray said "Wavr is not
    answering. It may have stopped." and the chip said "...on this machine...",
    which is the drift that docstring promised was impossible — four words
    apart, on two surfaces the same person has open at the same time.
    """
    tray = TRAY.read_text(encoding="utf-8")
    chip = CHIP.read_text(encoding="utf-8")

    assert rs.HEADLINE_SILENT in tray, (
        f"the tray's offline tooltip is not {rs.HEADLINE_SILENT!r}")
    for surface, text in (("the tray", tray), ("the shell chip", chip)):
        assert rs.CORE_SILENT in text, (
            f"{surface} does not say {rs.CORE_SILENT!r} when the Core is "
            f"silent, so it has a second sentence for one state")


def test_not_responding_and_not_reporting_are_kept_apart():
    """Two different failures with two different errands: the Core is not
    answering at all, versus one sensor that should be producing and is not.

    They were kept apart by hand until now, which works right up until somebody
    reasonably decides the two phrasings are inconsistent and unifies them.
    """
    core_words = rs.unreachable("X").headline.lower()
    assert "not responding" in core_words
    assert "not reporting" not in core_words

    sensor = rs.assess(
        uptime_s=86400,
        last_state_at=None,
        coverage_rows=[{"sensor_id": "a", "health": "ok"},
                       {"sensor_id": "b", "health": "offline"}])
    said = " ".join(f.text for f in sensor.findings if f.key == "sensors").lower()
    assert "not reporting" in said
    assert "not responding" not in said


def test_a_person_is_never_shown_the_raw_wire_value():
    """`offline` is the value on the wire. A laptop is "offline" when it is
    asleep, so a household reads it as "switched off" rather than "broken" —
    which is the opposite of what it means here."""
    sensor = rs.assess(
        uptime_s=86400, last_state_at=None,
        coverage_rows=[{"sensor_id": "a", "health": "offline"}])
    said = " ".join(f.text for f in sensor.findings)
    assert "offline" not in said.lower(), said
