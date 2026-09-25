"""Absent hardware must never be reported as a successful hardware probe."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("script", ["check_ld2450.py", "check_uwb.py"])
def test_python_probe_without_source_cannot_pass(script):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "backend")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "hw" / script)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout.startswith("CANNOT TEST:"), result.stdout


def test_an_unreachable_uwb_source_is_cannot_test_not_fail():
    # Nothing answered, so nothing was tested: 2, the same as no source at all.
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "backend")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "hw" / "check_uwb.py"),
         "--source", "http://127.0.0.1:9/reading", "--timeout", "2"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout.startswith("CANNOT TEST:"), result.stdout


def test_linux_probe_without_binary_cannot_pass():
    sh = shutil.which("sh")
    if not sh:
        pytest.skip("POSIX sh is unavailable on this host")
    env = os.environ.copy()
    env["WAVR_BIN"] = str(ROOT / "scripts" / "hw" / "missing-wavr-binary")
    result = subprocess.run(
        [sh, str(ROOT / "scripts" / "hw" / "check_node_linux.sh")],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout.startswith("CANNOT TEST:"), result.stdout
