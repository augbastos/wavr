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
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
VERSAO = (RAIZ / "VERSION").read_text(encoding="utf-8").strip()


def ler(rel: str) -> str:
    p = RAIZ / rel
    if not p.is_file():
        pytest.skip(f"{rel} is not in this checkout")
    return io.open(p, encoding="utf-8", newline="").read()


def test_the_version_file_is_a_version():
    assert re.fullmatch(r"\d+\.\d+\.\d+", VERSAO), (
        f"VERSION reads {VERSAO!r}; it must be a bare semantic version")


def test_the_backend_package_agrees():
    fonte = ler("backend/pyproject.toml")
    achado = re.search(r'^version\s*=\s*"([^"]+)"', fonte, re.M)
    assert achado, "backend/pyproject.toml has no version"
    assert achado.group(1) == VERSAO, (
        f"backend says {achado.group(1)}, VERSION says {VERSAO}")



def test_the_running_package_agrees():
    """The number a person is actually shown, asked of the package itself.

    Every other check here reads a FILE. `backend/wavr/__init__.py` was not one
    of the files, so `wavr.__version__` sat at the previous release while
    `VERSION` and four manifests moved — and `wavr.__version__` is what
    `config_export` stamps into the diagnostic bundle somebody sends to a
    person helping them. The check covered the declarations and missed the
    value.

    Imported rather than parsed, deliberately: parsing the file would test the
    same string a sixth time, and what matters is what the running product
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

def test_the_desktop_shell_agrees():
    for rel in ("desktop/package.json", "desktop/src-tauri/tauri.conf.json"):
        dados = json.loads(ler(rel))
        assert dados.get("version") == VERSAO, (
            f"{rel} says {dados.get('version')}, VERSION says {VERSAO}")


def test_the_companion_agrees():
    dados = json.loads(ler("mobile/package.json"))
    assert dados.get("version") == VERSAO, (
        f"mobile/package.json says {dados.get('version')}, VERSION says {VERSAO}")

    fonte = ler("mobile/android/app/build.gradle")
    nome = re.search(r'versionName\s+"([^"]+)"', fonte)
    assert nome and nome.group(1) == VERSAO, (
        f"the companion's versionName is {nome and nome.group(1)}, "
        f"VERSION says {VERSAO}")


def test_the_core_launcher_agrees():
    fonte = ler("core-launcher/app/build.gradle")
    nome = re.search(r'versionName\s+"([^"]+)"', fonte)
    assert nome and nome.group(1) == VERSAO, (
        f"the Core launcher's versionName is {nome and nome.group(1)}, "
        f"VERSION says {VERSAO}")


@pytest.mark.parametrize("rel", ["mobile/android/app/build.gradle",
                                 "core-launcher/app/build.gradle"])
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
