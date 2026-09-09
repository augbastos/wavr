"""`project.json` is written to be read cold. So it has to still be true.

It exists so a language model — or a person — can open this repository with no
context and get an accurate map: what the surfaces are, where they live, what
the invariants are, what is genuinely still missing. A map like that is worth
more than prose, and worth less than nothing when it is stale: a reader who
trusts it stops looking.

It has already been wrong once, on the day it was written. It described
`mobile/` as a surface of this repository while a clone of master contained no
`mobile/` at all — a claim with no code behind it, in the file whose own
`conventions` section says the most expensive defect here is exactly that.

So the paths it names are checked mechanically. Not the prose: the paths, the
entry points, the version, and the one claim that must never quietly become
false — that this project has no users and no customers.
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
ARQUIVO = RAIZ / "project.json"


@pytest.fixture(scope="module")
def mapa() -> dict:
    if not ARQUIVO.is_file():
        pytest.fail("project.json is gone — it is referenced from the README")
    return json.loads(io.open(ARQUIVO, encoding="utf-8").read())


def test_every_surface_it_names_exists(mapa):
    """A surface is a directory somebody can open. If it is named here and a
    clone does not have it, the file is advertising something that is not
    there — which is how this test came to exist."""
    faltando = []
    for s in mapa["surfaces"]:
        caminho = s["path"].rstrip("/").split(" ")[0]
        if not (RAIZ / caminho).exists():
            faltando.append(f"{s['name']} -> {s['path']}")
    assert not faltando, (
        "project.json names surfaces this repository does not contain:\n  "
        + "\n  ".join(faltando))


def test_every_file_it_points_at_exists(mapa):
    """`where_to_look` is the section a reader uses first. A dead path there
    sends them looking for something that moved.

    A key starting with `$` is an annotation, not a path -- the convention this
    document already uses for `$comment` in three other sections. It was not
    honoured here, so the first annotation added to `where_to_look` was read as
    a filename and reported as missing. A validator that enforces a convention
    in three places and not the fourth trains people to work around it."""
    faltando = [f"{k} -> {v}" for k, v in mapa["where_to_look"].items()
                if not k.startswith("$") and not (RAIZ / v).exists()]
    assert not faltando, (
        "project.json points at paths that do not exist:\n  "
        + "\n  ".join(faltando))


def test_the_version_it_states_is_the_version(mapa):
    versao = (RAIZ / "VERSION").read_text(encoding="utf-8").strip()
    assert mapa["project"].get("version") == versao, (
        f"project.json says {mapa['project'].get('version')}, "
        f"VERSION says {versao}")


def test_it_still_says_there_are_no_users(mapa):
    """The single most important sentence in the file, and the one most likely
    to be quietly dropped as the project grows. Nothing here has users,
    customers or paying installations, and a model reading this repository
    would otherwise assume the usual."""
    bloco = mapa.get("not_true_of_this_project", {})
    texto = " ".join(str(v) for v in bloco.values()).lower()
    assert "no users" in texto or "no customers" in texto, (
        "the 'not true of this project' block no longer says there are no "
        "users or customers — if that changed, it changed in reality first "
        "and this test should be updated deliberately")


def test_the_gaps_it_lists_are_still_open(mapa):
    """A closed gap left in the list is the same lie as an open one omitted.
    Checked for the two that are mechanically decidable."""
    texto = " ".join(mapa["known_gaps"]).lower()

    if "website" in texto and "not deployed" in texto:
        assert (RAIZ / "site").is_dir(), (
            "the gap list talks about the website; site/ is not here")

    # The companion's first-run screens: the gap says they are English-only.
    # If the shim ever starts routing them through the dashboard's translator,
    # this stops being true and the list has to change with it.
    shim = RAIZ / "mobile" / "src" / "wavr-mobile-shim.js"
    if shim.is_file() and "first-run screens are hardcoded english" in texto:
        fonte = io.open(shim, encoding="utf-8", errors="replace").read()
        sem_comentario = re.sub(r"^\s*//.*$", "", fonte, flags=re.M)
        assert "WavrT(" not in sem_comentario, (
            "the companion now calls the dashboard's translator, so "
            "'first-run screens are hardcoded English' is no longer a gap — "
            "update project.json")
