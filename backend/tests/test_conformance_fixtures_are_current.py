"""conformance/ describes Wavr's semantics to every other implementation.

The native runtime (native/) is checked against those files, not against the
Python code, so a Python change that is not regenerated there leaves the two
implementations quietly disagreeing -- each passing its own tests. This fails
instead, and says which fixture to regenerate.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GEN = REPO / "scripts" / "gen_conformance.py"


def test_the_fixtures_match_the_python_they_were_generated_from():
    r = subprocess.run([sys.executable, str(GEN), "--check"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_check_notices_a_stale_fixture(tmp_path, monkeypatch):
    # The control: a --check that cannot fail proves nothing.
    spec = importlib.util.spec_from_file_location("gen_conformance", GEN)
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    monkeypatch.setattr(gen, "OUT", tmp_path)
    assert gen.main([]) == 0                         # writes a fresh set
    (tmp_path / "status.json").write_text("{}", encoding="utf-8")
    assert gen.main(["--check"]) == 1
