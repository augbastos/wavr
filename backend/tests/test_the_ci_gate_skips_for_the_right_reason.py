"""The browser job is skipped by a rule. The rule is worth testing.

`tests.yml` grew a `what-changed` job so the eleven-minute browser job runs only
when a commit touches something a real Chromium could observe. That saves
minutes this repository now actually pays for — it is private, so its runner
time comes out of the account's monthly allowance instead of being discounted
away — and it introduces a new way to be wrong that is completely silent: a
skipped job and a passing job are the same colour in a summary. A gate that
skips too eagerly deletes coverage and reports success while doing it.

So the rule is not trusted, it is exercised. The pattern is READ OUT OF THE
WORKFLOW rather than restated here, because a second copy would drift and the
drift would be invisible — the same reason `test_egress_is_visible` executes the
frontend's own rule instead of quoting it.

What this cannot check: the shell around the pattern — the base-SHA resolution
and the fail-open branches. Those are asserted structurally at the bottom, and
the honest statement is that their behaviour is only observed on a runner.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
FLUXO = RAIZ / ".github" / "workflows" / "tests.yml"


def _padrao() -> str:
    """The gate's own regex, lifted from the workflow.

    Fails loudly if it cannot be found. A gate whose pattern this file can no
    longer locate is a gate this file has stopped testing, and that must not
    look like a pass.
    """
    fonte = FLUXO.read_text(encoding="utf-8")
    m = re.search(r"grep -qE \\\s*\n\s*'(\^\([^']+)'", fonte)
    assert m, (
        "could not find the what-changed gate's pattern in tests.yml. Either "
        "the gate was removed — in which case delete this file — or it was "
        "rewritten and nothing here is testing it any more.")
    return m.group(1)


# (should the browser job run?, what changed)
CASOS = [
    # -- it must run: the browser can observe all of these -------------------
    (True, "frontend/index.html"),
    (True, "frontend/sw.js"),
    (True, "frontend/js/runtime.js"),
    (True, "experiences/anchor-demo/index.html"),
    (True, "backend/wavr/app.py"),
    (True, "backend/tests/test_browser_ui.py"),
    (True, "backend/tests/test_offline_launch.py"),
    (True, "backend/tests/test_three_d_view.py"),
    (True, "backend/tests/test_deep_links.py"),
    (True, ".github/workflows/tests.yml"),
    # One relevant file among irrelevant ones still runs it.
    (True, "README.md\nbackend/wavr/fusion.py"),

    # -- it may skip: nothing the browser renders is involved ----------------
    (False, "README.md"),
    (False, "CONTRIBUTING.md\nSECURITY.md\nREADME.md"),
    (False, "backend/tests/test_fusion_per_sensor.py"),
    (False, "core-launcher/app/src/main/java/dev/wavr/core/MainActivity.kt"),
    (False, "mobile/src/wavr-mobile-shim.js"),
    (False, ".github/workflows/docker.yml"),
    (False, "site/public/index.html"),

    # -- the anchor: a path that merely CONTAINS one of these names ----------
    # Without `^` these would match and the gate would run the job needlessly;
    # without `$` on the test names, a neighbouring file would too. Neither is
    # dangerous on its own, and both mean the pattern is not saying what it
    # looks like it says.
    (False, "docs/frontend/notes.md"),
    (False, "backend/tests/test_browser_ui_helpers.py"),
]


@pytest.mark.parametrize("esperado,mudou", CASOS)
def test_the_gate_decides_the_same_way_a_person_reading_it_would(esperado, mudou):
    """Run the workflow's own grep, exactly as the workflow runs it."""
    padrao = _padrao().replace("\\\\", "\\")
    r = subprocess.run(["grep", "-qE", padrao], input=mudou + "\n",
                       text=True, capture_output=True)
    correu = (r.returncode == 0)
    assert correu is esperado, (
        f"changed:\n  " + mudou.replace("\n", "\n  ")
        + f"\n-> the gate says {'run' if correu else 'SKIP'} the browser job, "
        f"and it should say {'run' if esperado else 'skip'}")


def test_the_gate_fails_open():
    """Every branch that cannot answer the question must answer "run it".

    This is the property that makes the gate safe, and it is the one a future
    edit is most likely to lose — the tempting simplification is to drop a case
    and let it fall through to the default, and the default a person writes
    when they are simplifying is usually `false`.

    Checked structurally: the shell is not executed here, so this asserts that
    each escape hatch exists and sets the output to true, not that a runner
    behaves that way. That part is only observable on a runner.
    """
    fonte = FLUXO.read_text(encoding="utf-8")
    inicio = fonte.index("what-changed:")
    corpo = fonte[inicio:fonte.index("\n  test:", inicio)]

    for situacao in ("0000000000000000000000000000000000000000",   # first push
                     "git cat-file -e",                            # base absent
                     "git diff failed"):                           # diff refused
        assert situacao in corpo, (
            f"the gate no longer handles {situacao!r}; a case it cannot answer "
            f"must resolve to running the browser tests, not to skipping them")

    # Every early exit sets it true. If one of them ever sets false, the gate
    # has a silent hole.
    saidas = re.findall(r'echo "browser=(\w+)" >> "\$GITHUB_OUTPUT"', corpo)
    assert saidas.count("true") >= 3, (
        f"expected at least three fail-open exits setting browser=true, found "
        f"{saidas}")
    assert saidas.count("false") == 1, (
        f"there should be exactly ONE path that skips the browser job — the "
        f"one that positively established nothing relevant changed. Found "
        f"{saidas.count('false')}: {saidas}")


def test_the_job_is_actually_wired_to_the_gate():
    """The gate is worth nothing if the job does not consult it.

    A `what-changed` job that computes a correct answer nobody reads is the
    same defect this repository keeps finding from the other direction: a
    producer with no consumer.
    """
    fonte = FLUXO.read_text(encoding="utf-8")
    bloco = fonte[fonte.index("\n  browser:"):]
    bloco = bloco[:bloco.index("\n  guarantees:")]
    assert "needs: what-changed" in bloco, (
        "the browser job no longer depends on the gate")
    assert "needs.what-changed.outputs.browser == 'true'" in bloco, (
        "the browser job no longer reads the gate's answer")
