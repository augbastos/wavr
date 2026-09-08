"""An empty screen must say WHICH nothing it is looking at.

There are at least three, and a household reads them very differently:

  * **Nothing happened.** The house was quiet. Wavr was watching.
  * **Nothing is set up yet.** Wavr cannot answer, and there is a step to take.
  * **Nothing is watching.** Wavr is paused or blind, and any answer would be
    a guess dressed as a fact.

Collapsing the second or third into the first is the failure this project
already fixed once, in the map's screen-reader summary: a house with every
camera offline announced "Empty home — no presence detected" at the exact
moment nothing could see.

## The two this file was written for

`Who's home` had the same bug in a different place. Its honesty gate read

    else if (!kp || !Array.isArray(kp.corroborators))

so an EMPTY corroborator array — nobody has assigned a device to a person yet,
which is every fresh install — sailed past it and landed on "No one detected
right now." An absence of people, asserted from an absence of assignments. The
other consumer of that same payload, `renderKnownPresence()`, had always
treated `[]` as "nothing honest to show" and hidden itself: two readers of one
payload disagreeing about what empty meant.

`History` said "no events match these filters" with no hint that the filter
searches only the sixty events already loaded — so an empty result reads as an
empty history.
"""
from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
SHELL = FRONTEND / "index.html"
# Who's home moved out of the shell into its own module. These assertions have
# to follow it, or they pass by finding nothing — which is how a guard quietly
# stops guarding after a refactor that had nothing to do with it.
WHOSHOME = FRONTEND / "js" / "whoshome.js"


def _src() -> str:
    """The shell AND the modules lifted out of it. Reading only index.html
    means a rule silently stops being checked the day its code is extracted."""
    parts = [SHELL.read_text(encoding="utf-8")]
    for m in sorted((FRONTEND / "js").glob("*.js")):
        parts.append(m.read_text(encoding="utf-8"))
    return "\n".join(parts)


def test_an_empty_corroborator_list_is_treated_as_not_set_up():
    """The guard must reject an empty array, not just a missing one."""
    src = _src()
    guard = re.search(
        r"else if\(!kp \|\| !Array\.isArray\(kp\.corroborators\)([^)]*)\)", src)
    assert guard, "the Who's home honesty gate moved or changed shape"
    assert "kp.corroborators.length" in guard.group(0), (
        "an empty corroborator list falls through this gate again. On a fresh "
        "install that asserts nobody is home, from the fact that nobody has "
        "assigned a device to a person — two different nothings.")


def test_the_fallback_sentence_names_what_it_is_based_on():
    """Reachable only when devices ARE registered and none is present, so it
    can say so. "No one detected" alone is a claim about the house; Wavr can
    only make a claim about the devices."""
    src = _src()
    # "here", not "home": the sentence has to be true in an office or a clinic,
    # and "home" was one of the ninety-eight strings that assumed a house.
    assert "None of the registered devices is here right now." in src
    # Look for it being ASSIGNED, not merely mentioned: the comment above the
    # fix quotes the old wording, and a substring search over the whole file
    # cannot tell an explanation from a regression.
    revived = re.search(
        r'(?:textContent|innerText)\s*=\s*["\']No one detected right now\.', src)
    assert not revived, (
        "the unqualified sentence is being rendered again — it reads as a "
        "statement about who is in the building, which is not what the data "
        "supports.")


def test_the_two_readers_of_known_presence_agree_about_empty():
    """`renderKnownPresence()` hides itself on `[]`; Who's home must not
    contradict it by answering."""
    src = _src()
    assert "if(!corroborators.length){ tile.hidden = true; return; }" in src, (
        "renderKnownPresence's own empty guard changed — check that Who's "
        "home still agrees with it.")


def test_a_filtered_history_says_the_filter_is_why():
    src = _src()
    m = re.search(r'<div id="tlNone"[^>]*>(.*?)</div>', src, re.S)
    assert m, "the History empty state moved"
    text = re.sub(r"<[^>]+>", " ", m.group(1))
    text = re.sub(r"\s+", " ", text).strip().lower()
    assert "filter" in text, "it no longer says the filters are why"
    assert "clear" in text or "widen" in text, (
        f"it does not say what to do about it: {text!r}")
    assert "60" in text or "already loaded" in text, (
        "it does not say the search covers only the events already loaded, so "
        "an empty result still reads as an empty history.")


def test_the_pause_state_still_refuses_to_imply_an_empty_home():
    """The oldest of these gates, and the reason the others exist."""
    src = _src()
    assert "Sensing is off" in src
    assert "System paused — nothing is sensing." in src, (
        "the paused copy changed; make sure it still refuses to report "
        "occupancy from before the pause.")
