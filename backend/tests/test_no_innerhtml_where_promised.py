"""A file that says it never reaches for innerHTML must never reach for it.

`trust.js` opens with:

    No innerHTML anywhere in this file. The values it renders — sensor ids,
    room names — are operator input, and a file that never reaches for
    innerHTML cannot grow an unsafe one in a later edit.

That is a good rule and it was a promise with nothing enforcing it, which is
the shape this codebase keeps finding: a docstring making an absolute
statement, and a suite that would not notice the day it stopped being true.
The privacy screen's delete controls were added to that file this week; the
next thing added to it will be added by somebody who has not read line 34.

## Why only some files

`runtime.js` DOES use innerHTML, deliberately, and carries its own `escape()`
for the purpose. The rule is not "innerHTML is forbidden" — it is "a file that
claims not to use it, does not". So the list below is derived from the claim
itself: any frontend module whose header says it does not use innerHTML is
checked, and a file that stops making the claim stops being checked, which is
honest as long as dropping the claim is a visible edit.
"""
from __future__ import annotations

import re
from pathlib import Path

JS = Path(__file__).resolve().parents[2] / "frontend" / "js"

CLAIM = re.compile(r"No innerHTML anywhere in this file", re.I)
USE = re.compile(r"\.innerHTML\s*=|\.innerHTML\b")


def _claimants() -> dict[str, str]:
    out = {}
    for f in sorted(JS.glob("*.js")):
        src = f.read_text(encoding="utf-8")
        if CLAIM.search(src):
            out[f.name] = src
    return out


def test_at_least_one_file_still_makes_the_claim():
    """If every file quietly dropped the sentence, this whole check would pass
    by having nothing to check."""
    claimants = _claimants()
    assert claimants, (
        "no frontend module claims 'No innerHTML anywhere in this file' any "
        "more. Either the rule was abandoned — which is a decision worth "
        "seeing — or the sentence was lost in an edit.")
    assert "trust.js" in claimants, (
        "trust.js dropped the claim. It renders operator input on the privacy "
        "screen; if the rule genuinely changed, say why here.")


def test_no_claiming_file_uses_innerhtml():
    offenders = []
    for name, src in _claimants().items():
        for m in USE.finditer(src):
            line = src[:m.start()].count("\n") + 1
            # The claim itself mentions the word; a comment is not a use.
            start = src.rfind("\n", 0, m.start()) + 1
            text = src[start:src.find("\n", m.start())]
            if text.lstrip().startswith(("//", "*", "/*")):
                continue
            offenders.append(f"{name}:{line}  {text.strip()[:80]}")
    assert not offenders, (
        "these files promise not to use innerHTML and do:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse createElement/textContent, or drop the claim from the "
          "header — but not silently.")


def test_the_detector_recognises_a_real_use():
    """A regex that stops matching makes the check above pass forever.
    `runtime.js` uses innerHTML on purpose and makes no claim, so it is the
    honest control sample."""
    src = (JS / "runtime.js").read_text(encoding="utf-8")
    assert USE.search(src), (
        "the detector finds no innerHTML in runtime.js, which definitely has "
        "some — so it would not find one in trust.js either")
    assert not CLAIM.search(src), (
        "runtime.js now claims not to use innerHTML while using it; one of "
        "the two has to change")
