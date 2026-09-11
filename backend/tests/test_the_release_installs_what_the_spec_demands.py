"""The freeze spec states its requirements. Something has to satisfy them.

`desktop/sidecar/wavr-core.spec` refuses to build a Core without `zeroconf`,
and explains why in the error it raises: that import is lazy, so a build
WITHOUT it succeeds and produces a Core that never advertises `_wavr._tcp` —
and the phone's "Find your Wavr hub" then spends a minute failing to find a hub
that was never there. The guard exists because that shipped once.

The release workflow installed `backend` with no extras. So the guard fired,
the Windows job died at `pip install`, and the two jobs after it — the installer
smoke test and the draft release — were skipped. No artifacts, no release.

Nobody knew, because this repository has no releases: the first real tag was
the first time that job ran to the end.

This is the missing half. The spec declares what it needs; this asserts the
workflow installs it. A requirement with nobody satisfying it is the defect
class this repository names out loud in its own contributing guide — *a
guarantee needs a producer* — and here the guarantee had been written down for
months with nothing on the other side of it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "desktop" / "sidecar" / "wavr-core.spec"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
PYPROJECT = ROOT / "backend" / "pyproject.toml"

# module name -> the extra in backend/pyproject.toml that provides it
PROVIDED_BY = {
    "zeroconf": "mdns",
    "ifaddr": "mdns",        # zeroconf's own dependency, named by the spec too
}


def _spec_requirements() -> list[str]:
    """The modules the spec refuses to build without, read from the spec."""
    src = SPEC.read_text(encoding="utf-8")
    block = re.search(r"for _needed, _why in \((.*?)\n\):", src, re.S)
    assert block, "the spec's requirement loop is no longer recognisable here"
    return re.findall(r'\(\s*"([a-z0-9_]+)"', block.group(1))


def _sidecar_install_lines() -> list[str]:
    """The pip lines in the job that freezes the Core."""
    yml = RELEASE.read_text(encoding="utf-8")
    job = yml[yml.index("build the bundled Core sidecar"):]
    job = job[:job.index("- name:", 1)]
    return [l.strip() for l in job.splitlines() if "pip install" in l]


def test_the_spec_still_states_its_requirements():
    """The control. If the spec stops guarding, this file is asserting nothing
    and should fail loudly rather than pass emptily."""
    needed = _spec_requirements()
    assert needed, "the spec no longer refuses to build without anything"
    assert "zeroconf" in needed, needed


@pytest.mark.parametrize("module", sorted(PROVIDED_BY))
def test_the_release_job_installs_it(module):
    if module not in _spec_requirements():
        pytest.skip(f"the spec no longer requires {module}")
    extra = PROVIDED_BY[module]
    lines = _sidecar_install_lines()
    assert any(f"[{extra}]" in l for l in lines), (
        f"the spec refuses to build without {module!r}, which comes from the "
        f"`{extra}` extra, and the release job installs:\n  "
        + "\n  ".join(lines)
        + "\nThe build will die at pip install, and the smoke test and the "
          "draft release will be skipped with it.")


def test_the_extra_exists_and_provides_it():
    """And the extra is not a name invented by this test."""
    toml = PYPROJECT.read_text(encoding="utf-8")
    block = re.search(r"\[project\.optional-dependencies\](.*?)(\n\[|\Z)",
                      toml, re.S)
    assert block, "backend/pyproject.toml declares no optional dependencies"
    assert re.search(r"^\s*mdns\s*=", block.group(1), re.M), (
        "the `mdns` extra the release job installs does not exist")
    assert "zeroconf" in block.group(1), "the mdns extra does not ship zeroconf"
