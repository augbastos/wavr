"""`scripts/gen_releases_manifest.py`: does the manifest tell the truth?

## What could go wrong here, specifically

This generator's entire job is to be more trustworthy than a hand-written
JSON file next to the installers -- so the failure modes worth testing are
the ones a hand-written file would have gotten wrong:

  * a stale `SHA256SUMS.txt` (a real artifact was rebuilt on 2026-09-06 and
    the sums file next to it was not -- see `project_wavr...` session
    notes) must not leak its recorded hash into the manifest;
  * a missing artifact must produce an honest entry, not a KeyError or a
    manifest that silently omits the product;
  * "outdated" must actually compare mtimes, not just exist as a field that
    is always False.

Every fixture here lives under `tmp_path`; nothing touches the real
`_local/first-user-rc/` (100+ MB of real installers) or the real
`site/public/assets/releases.json`.
"""
from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import gen_releases_manifest as genmod  # noqa: E402


# The five real filenames this generator must know about. Duplicated here
# (rather than imported from PRODUCTS) on purpose: this is an independent
# check that nobody renamed a spec's `internal_filename` without also
# updating it to match what the build actually produces.
REAL_INTERNAL_FILENAMES = {
    "windows-installer-exe": "Wavr-Setup-Windows.exe",
    "windows-installer-msi": "Wavr-Windows.msi",
    "android-companion": "2-wavr-app-for-phone-and-tablet.apk",
    "android-core-brain": "1-wavr-core-the-brain-INSTALL-ON-ONE-PHONE-ONLY.apk",
    "android-kiosk": "3-wavr-wall-screen-kiosk-optional.apk",
}


def _by_id(manifest: dict, product_id: str) -> dict:
    for entry in manifest["products"]:
        if entry["id"] == product_id:
            return entry
    raise AssertionError(f"no product with id={product_id!r} in manifest")


def test_product_specs_match_the_real_filenames():
    specs = {s.id: s.internal_filename for s in genmod.PRODUCTS}
    assert specs == REAL_INTERNAL_FILENAMES


def test_missing_artifact_is_reported_honestly(tmp_path: Path):
    rc_dir = tmp_path / "first-user-rc"
    rc_dir.mkdir()

    manifest, warnings = genmod.build_manifest(rc_dir=rc_dir)

    assert warnings == []
    assert len(manifest["products"]) == len(genmod.PRODUCTS)
    for entry in manifest["products"]:
        assert entry["available"] is False
        assert entry["sha256"] is None
        assert entry["sizeBytes"] is None
        assert entry["downloadPath"] is None
        assert entry["missingReason"]  # non-empty, names the file


def test_sha256_is_computed_not_copied(tmp_path: Path):
    """The manifest's sha256 must equal an independently computed digest --
    never a value read from anywhere. `hashlib` here is a second, separate
    computation from the one inside the generator, over the same bytes."""
    rc_dir = tmp_path / "first-user-rc"
    rc_dir.mkdir()
    payload = b"not a real installer, just fixture bytes" * 1000
    apk = rc_dir / "2-wavr-app-for-phone-and-tablet.apk"
    apk.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()

    manifest, warnings = genmod.build_manifest(rc_dir=rc_dir)

    entry = _by_id(manifest, "android-companion")
    assert warnings == []
    assert entry["available"] is True
    assert entry["sha256"] == expected
    assert entry["sizeBytes"] == len(payload)
    assert entry["downloadName"] == "Wavr-Android.apk"
    assert entry["internalFilename"] == "2-wavr-app-for-phone-and-tablet.apk"
    assert entry["downloadPath"] == "/download/Wavr-Android.apk"
    assert entry["maturity"] == "recommended"


def test_maturity_matches_what_the_site_should_hide_behind_advanced(tmp_path: Path):
    """The companion app is what a first-time user installs today; the
    other two Android builds are gated behind an "advanced downloads"
    section on the site, which reads this field to decide that."""
    manifest, _ = genmod.build_manifest(rc_dir=tmp_path)
    assert _by_id(manifest, "android-companion")["maturity"] == "recommended"
    assert _by_id(manifest, "android-core-brain")["maturity"] == "experimental"
    assert _by_id(manifest, "android-kiosk")["maturity"] == "specialized"
    assert _by_id(manifest, "windows-installer-exe")["maturity"] == "recommended"


def test_kiosk_purpose_warns_about_taking_over_the_home_screen(tmp_path: Path):
    """A device owner must not be able to install this by accident without
    the manifest itself having said, in plain language, what it does."""
    manifest, _ = genmod.build_manifest(rc_dir=tmp_path)
    purpose = _by_id(manifest, "android-kiosk")["purpose"].lower()
    assert "home screen" in purpose or "home-screen" in purpose


def test_core_and_kiosk_apks_are_marked_as_conflicting(tmp_path: Path):
    manifest, _ = genmod.build_manifest(rc_dir=tmp_path)
    core = _by_id(manifest, "android-core-brain")
    kiosk = _by_id(manifest, "android-kiosk")
    assert "android-kiosk" in core["conflictsWith"]
    assert "android-core-brain" in kiosk["conflictsWith"]


def test_stale_checksum_file_is_flagged_and_never_trusted(tmp_path: Path):
    """This is the exact failure mode that happened for real: an APK was
    rebuilt and SHA256SUMS.txt still named the old build's hash."""
    rc_dir = tmp_path / "first-user-rc"
    rc_dir.mkdir()
    payload = b"the rebuilt apk contents"
    apk = rc_dir / "2-wavr-app-for-phone-and-tablet.apk"
    apk.write_bytes(payload)
    real_digest = hashlib.sha256(payload).hexdigest()
    wrong_digest = "0" * 64
    assert wrong_digest != real_digest
    (rc_dir / "SHA256SUMS.txt").write_text(
        f"{wrong_digest} *2-wavr-app-for-phone-and-tablet.apk\n",
        encoding="utf-8")

    manifest, warnings = genmod.build_manifest(rc_dir=rc_dir)

    entry = _by_id(manifest, "android-companion")
    assert entry["sha256"] == real_digest, (
        "the manifest must use the freshly computed hash, never the one "
        "recorded in a checksum file on disk")
    assert len(warnings) == 1
    assert wrong_digest in warnings[0]
    assert "SHA256SUMS.txt" in warnings[0]
    assert entry["checksumFileMismatches"] == [
        {"file": "SHA256SUMS.txt", "recorded": wrong_digest}]


def test_matching_checksum_file_raises_no_warning(tmp_path: Path):
    rc_dir = tmp_path / "first-user-rc"
    rc_dir.mkdir()
    payload = b"correctly-summed installer bytes"
    exe = rc_dir / "Wavr-Setup-Windows.exe"
    exe.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    (rc_dir / "SHA256SUMS.txt").write_text(
        f"{digest} *Wavr-Setup-Windows.exe\n", encoding="utf-8")

    manifest, warnings = genmod.build_manifest(rc_dir=rc_dir)

    assert warnings == []
    entry = _by_id(manifest, "windows-installer-exe")
    assert "checksumFileMismatches" not in entry


def test_installer_older_than_core_binary_is_outdated(tmp_path: Path):
    rc_dir = tmp_path / "first-user-rc"
    rc_dir.mkdir()
    core_binary = tmp_path / "wavr-core.exe"

    installer = rc_dir / "Wavr-Setup-Windows.exe"
    installer.write_bytes(b"an old installer build")
    now = time.time()
    _set_mtime(installer, now - 3600)

    core_binary.write_bytes(b"a newer core binary")
    _set_mtime(core_binary, now)

    manifest, _ = genmod.build_manifest(rc_dir=rc_dir, core_binary=core_binary)

    entry = _by_id(manifest, "windows-installer-exe")
    assert entry["outdated"] is True
    assert entry["outdatedCheck"] == "core-binary"
    assert entry["outdatedReason"]


def test_installer_newer_than_core_binary_is_not_outdated(tmp_path: Path):
    rc_dir = tmp_path / "first-user-rc"
    rc_dir.mkdir()
    core_binary = tmp_path / "wavr-core.exe"

    core_binary.write_bytes(b"an older core binary")
    now = time.time()
    _set_mtime(core_binary, now - 3600)

    installer = rc_dir / "Wavr-Setup-Windows.exe"
    installer.write_bytes(b"a fresh installer build")
    _set_mtime(installer, now)

    manifest, _ = genmod.build_manifest(rc_dir=rc_dir, core_binary=core_binary)

    entry = _by_id(manifest, "windows-installer-exe")
    assert entry["outdated"] is False


def test_staleness_check_is_scoped_to_windows_only(tmp_path: Path):
    """Android APKs have no relationship to desktop/sidecar/dist/wavr-core.exe
    -- an old core binary must never mark a fresh APK as stale."""
    rc_dir = tmp_path / "first-user-rc"
    rc_dir.mkdir()
    core_binary = tmp_path / "wavr-core.exe"
    core_binary.write_bytes(b"core")
    _set_mtime(core_binary, time.time() + 3600)  # even artificially "future"

    apk = rc_dir / "2-wavr-app-for-phone-and-tablet.apk"
    apk.write_bytes(b"apk bytes")

    manifest, _ = genmod.build_manifest(rc_dir=rc_dir, core_binary=core_binary)
    entry = _by_id(manifest, "android-companion")
    assert entry["outdatedCheck"] == "not-applicable"
    assert entry["outdated"] is False


def test_missing_core_binary_does_not_crash_the_staleness_check(tmp_path: Path):
    rc_dir = tmp_path / "first-user-rc"
    rc_dir.mkdir()
    (rc_dir / "Wavr-Setup-Windows.exe").write_bytes(b"an installer")
    missing_core = tmp_path / "does-not-exist" / "wavr-core.exe"

    manifest, _ = genmod.build_manifest(rc_dir=rc_dir, core_binary=missing_core)

    entry = _by_id(manifest, "windows-installer-exe")
    assert entry["outdatedCheck"] == "core-binary-missing"
    assert entry["outdated"] is False


def test_write_manifest_produces_valid_json_at_the_given_path(tmp_path: Path):
    rc_dir = tmp_path / "first-user-rc"
    rc_dir.mkdir()
    (rc_dir / "Wavr-Windows.msi").write_bytes(b"msi bytes")
    out = tmp_path / "assets" / "releases.json"

    manifest, warnings = genmod.write_manifest(manifest_path=out, rc_dir=rc_dir)

    assert warnings == []
    assert out.is_file()
    import json
    on_disk = json.loads(out.read_text(encoding="utf-8"))
    assert on_disk == manifest
    assert on_disk["schemaVersion"] == 2
    assert "products" in on_disk


def _set_mtime(path: Path, timestamp: float) -> None:
    import os
    os.utime(path, (timestamp, timestamp))
