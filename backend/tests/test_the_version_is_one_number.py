"""One product, one version — and something that notices when that stops.

Before this file the tree carried four answers to "what version is Wavr":

    backend/pyproject.toml       0.3.0
    desktop/package.json         0.1.0
    desktop tauri.conf.json      0.1.0
    core-launcher                1.0
    mobile                       0.3.0

and the newest GitHub release said v0.3.0 while master carried the Space model,
the spatial layer, MCP-over-HTTP, the Android Core and per-device credentials.
Every one of those numbers was written by hand at a different moment, and
nothing compared them, so they drifted exactly as far apart as you would expect.

`VERSION` at the repository root is the answer. This test is the only reason it
stays the answer: a version file nothing checks is a fifth opinion.

Android's `versionCode` is deliberately NOT the product version — it is a
monotonic integer the platform requires — so it is checked for shape and for
agreeing with the product version's ordering, not for being equal to it.

## Where the file list itself used to drift

This file and `scripts/set_version.py` each used to keep their own copy of
"every file that carries the version". The copies drifted too:
`project.json` was in `set_version.py`'s list and missing from this one, so a
bump could move ten files, leave the eleventh silently behind, and this file
would still go green. `scripts/version_surfaces.py` is now the one list both
read — see it for what a "surface" is and why each file on it counts as one.
"""
from __future__ import annotations

import importlib.util
import io
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
VERSAO = (RAIZ / "VERSION").read_text(encoding="utf-8").strip()


def _load(name: str, rel: str):
    """Load a `scripts/` module by path — it is not a package on sys.path."""
    path = RAIZ / rel
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


version_surfaces = _load("version_surfaces", "scripts/version_surfaces.py")
set_version = _load("set_version", "scripts/set_version.py")

SURFACES = version_surfaces.SURFACES
NON_SURFACES = version_surfaces.NON_SURFACES
ANDROID_MANIFESTS = version_surfaces.ANDROID_MANIFESTS


def ler(rel: str) -> str:
    p = RAIZ / rel
    if not p.is_file():
        pytest.skip(f"{rel} is not in this checkout")
    return io.open(p, encoding="utf-8", newline="").read()


def test_the_version_file_is_a_version():
    assert re.fullmatch(r"\d+\.\d+\.\d+", VERSAO), (
        f"VERSION reads {VERSAO!r}; it must be a bare semantic version")


# -- every surface set_version.py moves, checked against VERSION ---------------

@pytest.mark.parametrize(
    "surface", SURFACES,
    ids=[f"{i}-{s.path}" for i, s in enumerate(SURFACES)])
def test_every_surface_agrees_with_version(surface):
    """The desktop shell, the companion, the Core launcher, the desktop
    client, project.json, the lockfiles — one test walking the shared list
    instead of one hand-written assertion per file.
    """
    if surface.optional and not surface.exists(RAIZ):
        pytest.skip(f"{surface.path} is not in this checkout")
    found = surface.current(RAIZ)
    assert found == VERSAO, f"{surface.path} says {found!r}, VERSION says {VERSAO!r}"


# -- the running product, not just its files ------------------------------------

def test_the_running_package_agrees():
    """The number a person is actually shown, asked of the package itself.

    Every surface check above reads a FILE. `backend/wavr/__init__.py` is one
    of those files, but `wavr.__version__` was once caught sitting at the
    previous release while `VERSION` and four manifests moved — and
    `wavr.__version__` is what `config_export` stamps into the diagnostic
    bundle somebody sends to a person helping them. The file check covered
    the declaration and missed the value.

    Imported rather than parsed, deliberately: parsing the file would test the
    same string a second time, and what matters is what the running product
    answers when asked.
    """
    import wavr
    assert wavr.__version__ == VERSAO, (
        f"the backend package reports {wavr.__version__} at runtime and "
        f"VERSION says {VERSAO}. This is the number in the diagnostic bundle, "
        f"so a person sending one for help is telling a supporter the wrong "
        f"release.")


def test_the_diagnostic_bundle_carries_that_same_number():
    """One step further, because the bundle is the surface that was wrong.

    `config_export.diagnostic_bundle` takes the version as an argument, so agreeing
    with the package is not enough on its own — the caller has to pass the
    right thing. Asked of the composed bundle rather than of the function.
    """
    import wavr
    from wavr.config_export import diagnostic_bundle
    pacote = diagnostic_bundle(version=wavr.__version__, platform="test")
    assert pacote["wavr_version"] == VERSAO, (
        f"the diagnostic bundle says {pacote['wavr_version']!r}; VERSION says "
        f"{VERSAO}")


def test_the_native_runtime_reads_the_version_file():
    """`wavr version` printed 0.1.0 inside an archive named 0.4.0: the native
    CMake project carried its own number. It now reads VERSION; this keeps it
    from growing a literal again."""
    fonte = ler("native/CMakeLists.txt")
    assert re.search(r'file\(STRINGS "\$\{CMAKE_CURRENT_SOURCE_DIR\}/\.\./VERSION"', fonte)
    projeto = re.search(r"^project\(wavr_native VERSION (\S+)", fonte, re.M)
    assert projeto and projeto.group(1) == "${_wavr_version}", (
        f"native/CMakeLists.txt pins version {projeto and projeto.group(1)}")


@pytest.mark.parametrize("rel", ANDROID_MANIFESTS)
def test_the_android_version_code_orders_with_the_product_version(rel):
    """`versionCode` is the platform's monotonic integer, not the product
    version. It only has to be a plausible encoding of it, so a release cannot
    ship a lower code than the version it claims."""
    fonte = ler(rel)
    achado = re.search(r"versionCode\s+(\d+)", fonte)
    assert achado, f"{rel} has no versionCode"
    codigo = int(achado.group(1))
    maior, menor, remendo = (int(x) for x in VERSAO.split("."))
    minimo = maior * 10000 + menor * 100 + remendo
    assert codigo >= minimo, (
        f"{rel} has versionCode {codigo}, which is below the {minimo} implied "
        f"by version {VERSAO} — a store would refuse the upgrade")


# -- the list itself cannot silently lose a surface ------------------------------

def _tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=RAIZ, capture_output=True,
                          text=True, check=True)
    return [line for line in out.stdout.splitlines() if line]


def test_a_forgotten_surface_is_caught():
    """This is the test the drift itself would have failed.

    `project.json` carried the current version and nothing here read it —
    exactly the shape of a silently forgotten surface. Rather than hand-add
    project.json to a list and call it fixed, this test greps every tracked
    file for the CURRENT version string and requires each hit to be either a
    known `Surface` or a justified entry in `NON_SURFACES`. The next file that
    hand-copies the version in is caught the day it is written, not the next
    time somebody happens to compare two lists by eye.
    """
    surface_paths = {s.path for s in SURFACES}
    known = surface_paths | set(NON_SURFACES)
    # a whole number by itself (e.g. the "5" release-notes emoji count) is not
    # a version; the version never appears as a substring of a longer one.
    achador = re.compile(rf"(?<![0-9.]){re.escape(VERSAO)}(?![0-9.])")
    esquecidos = []
    for rel in _tracked_files():
        if rel in known:
            continue
        p = RAIZ / rel
        try:
            texto = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if achador.search(texto):
            esquecidos.append(rel)
    assert not esquecidos, (
        f"{esquecidos} contain the current version ({VERSAO}) but are in "
        f"neither scripts/version_surfaces.SURFACES nor its NON_SURFACES "
        f"allowlist. Add a Surface if scripts/set_version.py should rewrite "
        f"it, or a justified NON_SURFACES entry if it should not.")


def test_every_surface_and_non_surface_still_exists():
    """A path that no longer exists proves nothing either way — it cannot be
    grepped, so `test_a_forgotten_surface_is_caught` would go on passing after
    a rename left a stale entry on either list."""
    for surface in SURFACES:
        if surface.optional:
            continue
        assert surface.exists(RAIZ), f"{surface.path} is listed as a Surface but is missing"
    for rel in NON_SURFACES:
        assert (RAIZ / rel).is_file(), (
            f"{rel} is listed in NON_SURFACES but is missing — remove the "
            f"entry, it no longer needs an excuse")


# -- set_version.py actually moves what the list says, and only that -----------

def test_set_version_rewrites_every_surface_and_nothing_else(tmp_path):
    """Runs the real `bump()` — the function `set_version.py`'s CLI calls —
    against a throwaway copy of the tree, and checks both halves of the claim
    in its docstring: every surface moved to the new version, and nothing
    else in those files or outside them did.

    `frontend/js/whats-new.js` is copied in as a CONTROL: it carries the
    current version as hand-written content (see NON_SURFACES) and a bump
    must leave it untouched. Without a file that is known to contain the
    version and known NOT to be a surface, "nothing else changed" would only
    ever be checked against files that were never going to change anyway.
    """
    maior, menor, remendo = (int(x) for x in VERSAO.split("."))
    nova_versao = f"{maior}.{menor}.{remendo + 1}"
    assert nova_versao != VERSAO

    control = "frontend/js/whats-new.js"
    a_copiar = {s.path for s in SURFACES} | set(ANDROID_MANIFESTS) | {control}

    antes = {}
    for rel in sorted(a_copiar):
        origem = RAIZ / rel
        if not origem.is_file():
            continue
        destino = tmp_path / rel
        destino.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origem, destino)
        antes[rel] = destino.read_bytes()

    mudou = set_version.bump(nova_versao, tmp_path)
    assert mudou, "bump() reported that nothing changed"

    # half one: every listed surface actually moved.
    for surface in SURFACES:
        if surface.optional and not surface.exists(tmp_path):
            continue
        achado = surface.current(tmp_path)
        assert achado == nova_versao, (
            f"{surface.path} was not rewritten: still says {achado!r}")

    # half two: the control was not touched, and every touched file changed
    # ONLY on the lines that carried a version number.
    depois = {rel: (tmp_path / rel).read_bytes() for rel in antes}
    assert depois[control] == antes[control], (
        f"{control} is not a version surface (see NON_SURFACES) and must not "
        f"change when the product version is bumped")

    for rel, bytes_antes in antes.items():
        if rel == control:
            continue
        bytes_depois = depois[rel]
        linhas_antes = bytes_antes.decode("utf-8").splitlines()
        linhas_depois = bytes_depois.decode("utf-8").splitlines()
        assert len(linhas_antes) == len(linhas_depois), (
            f"{rel}: line count changed ({len(linhas_antes)} -> {len(linhas_depois)})")
        diffs = [(a, d) for a, d in zip(linhas_antes, linhas_depois) if a != d]
        assert diffs, f"{rel} was copied but bump() left it byte-identical"
        for antiga, nova in diffs:
            e_versionname_ou_arquivo_simples = VERSAO in antiga and nova_versao in nova
            e_versioncode = rel in ANDROID_MANIFESTS and "versionCode" in antiga
            assert e_versionname_ou_arquivo_simples or e_versioncode, (
                f"{rel}: a line changed that does not carry the version:\n"
                f"  before: {antiga!r}\n  after:  {nova!r}")
