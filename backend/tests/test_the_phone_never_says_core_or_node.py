"""The first screen of the phone app speaks the household's words.

`docs/VOCABULARY.md` sets this out as a table: **Core** is what an admin or a
developer is shown for the machine doing the thinking, and a normal person is
shown *Wavr*, or nothing at all. **Node** is the admin word for a small sensor
board; a normal person is shown *sensor*.

The capability chooser is the first screen anybody sees after installing the
companion, and it cannot be skipped. Its first option read:

    Contribute presence
    Use this device's sensors to help your home know who's in.
    This is the core Wavr node.

Both admin nouns, in lower case, in a sentence meaning "this is the fundamental
one" — against a product where Core is the machine you are connecting TO. It
reads as "tick this to turn your phone into the Core", which is the opposite of
what the option does. The first user read it, stopped, and asked whether that
was what he wanted. A sentence whose only job is to answer that question
produced it instead.

The admin option had a quieter version of the same problem: "Needs an admin code
from the hub" leaves out the one fact that decides whether it works — the code
must be minted with Access level set to Admin. A viewer code pairs happily and
the manage half fails later, elsewhere, with nothing pointing back here.

## Scope, deliberately narrow

"hub" is NOT corrected and is not a defect: it is an established product word
for the machine being connected to, used sixty-seven times in the desktop shell
as well. It is missing from the vocabulary table, which is a gap worth closing
one day, but rewording two surfaces on my own judgement mid-test is not that
day.

What is checked is only the first screen a household sees, and only for the two
nouns the vocabulary explicitly reserves.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

# The mobile app lives in its own worktree on a divergent branch. Absent here,
# this file skips rather than fails: a checkout without it is not broken.
from tests.mobile_tree import mobile_dir   # noqa: E402 -- shared lookup

_MOBILE = mobile_dir()
SHIM_CANDIDATES = [
    (_MOBILE / "src" / "wavr-mobile-shim.js") if _MOBILE else Path("nao-existe"),
    Path(__file__).resolve().parents[2] / "mobile" / "src" / "wavr-mobile-shim.js",
]


@pytest.fixture(scope="module")
def escolhedor() -> str:
    """The capability chooser's option array, code only.

    Comments are stripped first, and that is not a formality: the comment above
    this array quotes both retired sentences to explain what was wrong with
    them. A check that reads prose finds the defect inside the warning against
    it — which has now happened three times in this repository, once while
    verifying this very fix.
    """
    for p in SHIM_CANDIDATES:
        if p.is_file():
            fonte = io.open(p, encoding="utf-8", newline="").read()
            break
    else:
        pytest.skip("the mobile shim is not in this checkout")
    codigo = re.sub(r"^\s*//.*$", "",
                    re.sub(r"/\*.*?\*/", " ", fonte, flags=re.S), flags=re.M)
    i = codigo.index('key: "sensor"')
    return codigo[i:codigo.index("];", i)]


def test_the_first_screen_does_not_call_the_phone_a_core_or_a_node(escolhedor):
    reservadas = [w for w in ("core", "node") if re.search(rf"\b{w}\b", escolhedor, re.I)]
    assert not reservadas, (
        f"the capability chooser uses {reservadas} — words VOCABULARY.md "
        f"reserves for an admin. This is the first screen after install and it "
        f"cannot be skipped; the last time it said 'the core Wavr node' the "
        f"first user read it as an instruction to turn his phone into the Core.")


def test_the_sensing_option_says_it_does_not_replace_the_hub(escolhedor):
    """The misreading was specific, so the correction is too."""
    sensor = escolhedor[:escolhedor.index('key: "viewer"')]
    assert "does not become one" in sensor, (
        "the sensing option no longer says outright that the phone reports to "
        "the hub rather than becoming one, which is the exact thing the old "
        "wording implied by accident")


def test_the_admin_option_names_the_step_that_makes_it_work(escolhedor):
    """Without this, the failure lands somewhere else, later, silently."""
    admin = escolhedor[escolhedor.index('key: "admin"'):]
    assert "Access level" in admin and "Admin" in admin, (
        "the admin option does not say the code has to be minted at Admin "
        "access level. A viewer code pairs without complaint and the manage "
        "half simply never works.")
