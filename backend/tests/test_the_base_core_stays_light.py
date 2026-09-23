"""A Core with no optional feature switched on imports no optional weight.

`pyproject.toml` promises that heavy sensing dependencies are lazy extras, and
for a long time one of them was not: `wavr.app -> calib_store -> localize`
imported numpy at start, so every Core -- a phone, a Pi, a laptop with no camera
-- paid for a 50 MB package and an OpenBLAS thread pool it never used, and the
dependency list had to declare numpy as base to stop the import crashing. The
fix moved numpy to the one function that solves a homography. This is what
keeps it there: a sentence in pyproject is not a guarantee.

It runs in a fresh interpreter, because this test process has long since
imported numpy for other tests and `sys.modules` here proves nothing.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys

# Packages a base Core must not import at start. Each is behind an extra, and
# each is imported lazily by the one feature that needs it.
HEAVY = ("numpy", "cv2", "torch", "ultralytics", "onnxruntime", "paho", "bleak",
         "serial", "zeroconf", "cryptography", "google.generativeai", "mcp",
         "jinja2")

_PROBE = """
import json, sys
import wavr.app  # noqa: F401 -- builds the default app, exactly as serve does
heavy = json.loads(sys.argv[1])
print(json.dumps(sorted(m for m in heavy if m in sys.modules)))
"""


def _imported_by_a_default_core(tmp_path) -> list[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("WAVR_")}
    env.update({"WAVR_DB": str(tmp_path / "wavr.db"),
                "WAVR_HOUSE_MAP": str(tmp_path / "house.json"),
                # An ancestor .env must not switch features on under the test.
                "PYTHON_DOTENV_DISABLED": "1"})
    out = subprocess.run([sys.executable, "-c", _PROBE, json.dumps(HEAVY)],
                         cwd=tmp_path, env=env, capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, out.stderr[-3000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_default_core_imports_no_optional_heavy_package(tmp_path):
    leaked = _imported_by_a_default_core(tmp_path)
    assert leaked == [], (
        f"importing wavr.app pulled in {leaked}. Each of these is an optional "
        f"extra; import it inside the function that needs it (see "
        f"wavr/localize.py for the pattern) rather than at module top.")


def test_the_probe_can_see_the_packages_it_guards():
    # The control. A guard over packages that are not installed passes whatever
    # the code does. The dev extra installs numpy and cryptography, so at least
    # those two are genuinely checked on every clean checkout.
    installed = [m for m in ("numpy", "cryptography")
                 if importlib.util.find_spec(m) is not None]
    assert installed == ["numpy", "cryptography"], (
        "the dev extra is supposed to install numpy and cryptography; without "
        "them the guard above checks nothing on a clean checkout")
