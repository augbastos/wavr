"""scripts/package_native.py writes release-manifest.json: for each packaged
target, the strongest evidence docs/platform-matrix.json records for it. A
release note repeats that word, so it must never be stronger than what ran --
a cross-compile is "compile only", never support."""
import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("package_native", REPO / "scripts" / "package_native.py")
pkg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pkg)


def test_every_packaged_target_has_a_row_in_the_matrix():
    rows = {t["id"] for t in json.loads(
        (REPO / "docs" / "platform-matrix.json").read_text(encoding="utf-8"))["targets"]}
    # A target without a row would read "unverified" -- true, but it means the map drifted.
    assert set(pkg.MATRIX_IDS.values()) <= rows


def test_evidence_is_the_strongest_thing_actually_run_and_no_more():
    assert pkg.evidence({"native_runtime": {"compiles": True, "hardware_tested": False}}) == "compile only"
    assert pkg.evidence({"native_runtime": {"compiles": True, "unit_tested": True}}) == "runtime tested"
    assert pkg.evidence({"native_runtime": {"simulation_tested": True}}) == "simulation tested"
    assert pkg.evidence({"native_runtime": {"hardware_tested": "partial"}}) == "hardware tested (partial)"
    assert pkg.evidence({"native_runtime": {"hardware_tested": True}}) == "hardware tested"
    assert pkg.evidence(None) == "unverified"
    # A value that is not literally true is not evidence ("blocked", "emulator").
    assert pkg.evidence({"native_runtime": {"compiles": "blocked"}}) == "unverified"
