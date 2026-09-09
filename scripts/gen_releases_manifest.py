"""Build `site/public/assets/releases.json` from what is actually on disk.

## What this is for

The install site must never hardcode a filename, a size or a checksum in
HTML -- every one of those drifts the moment somebody rebuilds an installer,
and a hardcoded checksum next to a rebuilt file is worse than no checksum at
all. This script is the one producer of artifact identity: it looks at
`_local/first-user-rc/`, computes what is true right now, and writes that
out as data. The site (owned elsewhere) only ever reads this file.

## The rules that matter

* **The hash is always computed from the bytes, never copied.** A
  `SHA256SUMS*.txt` file can exist alongside an artifact and go stale the
  moment that artifact is rebuilt without the sums file being regenerated
  with it -- which is exactly what happened to the Android companion APK on
  2026-09-06. If a checksum file disagrees with what this script computes,
  that is reported as a warning (see `checksumFileMismatches` below); the
  manifest itself always carries the freshly computed value.
* **A missing artifact is not a broken link.** Every product this pass
  knows about gets an entry regardless of whether the file exists;
  `available: false` plus `missingReason` lets the site say so honestly
  instead of shipping a 404.
* **The internal filename is kept, never shown.** `internalFilename` is the
  build's own naming (`2-wavr-app-for-phone-and-tablet.apk`); `downloadName`
  is what a user actually saves (`Wavr-Android.apk`). Only the second one
  belongs in front of a first-time user.
* **A Windows installer built before the current Core binary is stale.**
  `desktop/sidecar/dist/wavr-core.exe` is what actually ships inside the
  installer; if the installer's mtime predates it, the installer was built
  from an older Core and `outdated` is set so the site can say so rather
  than silently handing out an old build.

Run directly to regenerate the real file:

    python scripts/gen_releases_manifest.py

Import `build_manifest()` / `write_manifest()` to reuse the same computation
from the portal (`first_user_portal.py`) or from tests, against a sandboxed
`rc_dir` so tests never touch the real 100+ MB release candidates.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RC_DIR = ROOT / "_local" / "first-user-rc"
CORE_BINARY = ROOT / "desktop" / "sidecar" / "dist" / "wavr-core.exe"
MANIFEST_PATH = ROOT / "site" / "public" / "assets" / "releases.json"
CHECKSUM_FILES = ("SHA256SUMS.txt", "SHA256SUMS-completo.txt")

SCHEMA_VERSION = 2


@dataclass(frozen=True)
class ProductSpec:
    id: str
    product: str
    platform: str  # "windows" | "android"
    maturity: str  # "recommended" | "experimental" | "specialized"
    purpose: str
    internal_filename: str
    download_name: str
    check_staleness_against_core: bool = False
    conflicts_with: tuple[str, ...] = field(default_factory=tuple)


# The five real artifacts of tomorrow's release candidate. Order here is
# display order on the site (recommended path first per platform).
PRODUCTS: tuple[ProductSpec, ...] = (
    ProductSpec(
        id="windows-installer-exe",
        product="Wavr for Windows",
        platform="windows",
        maturity="recommended",
        # No "no Python, no repo" here. Naming the two things somebody does not
        # need is only reassuring to a reader who already knows what they are;
        # to everybody else it introduces two worries where there were none.
        purpose=("Start here. It installs like any other Windows program, and "
                 "there is nothing to set up first."),
        internal_filename="Wavr-Setup-Windows.exe",
        # Already a clean, human name -- and the exact one LEIA-ME.md tells
        # the first user to expect -- so it is kept as-is rather than invented
        # anew. Only the numbered Android builds need a translation.
        download_name="Wavr-Setup-Windows.exe",
        check_staleness_against_core=True,
    ),
    ProductSpec(
        id="windows-installer-msi",
        product="Wavr for Windows (MSI)",
        platform="windows",
        maturity="specialized",
        purpose=("The same installer as an MSI package. Use this only if "
                 "the .exe above refuses to run on this machine."),
        internal_filename="Wavr-Windows.msi",
        download_name="Wavr-Windows.msi",
        check_staleness_against_core=True,
    ),
    ProductSpec(
        id="android-companion",
        product="Wavr for Android",
        platform="android",
        maturity="recommended",
        # NOT "see and control your home": in this release the app is a
        # viewer onto the Core, and nothing it shows is a control. It also used
        # to say "install this one today", which now argues with the card's own
        # warning that its dashboard lags the desktop's.
        purpose=("The app for your phone or tablet: see your Space from "
                 "anywhere on the same Wi-Fi."),
        internal_filename="2-wavr-app-for-phone-and-tablet.apk",
        download_name="Wavr-Android.apk",
    ),
    ProductSpec(
        id="android-core-brain",
        product="Wavr Core for Android",
        platform="android",
        maturity="experimental",
        purpose=("Turns one phone into the Wavr brain instead of a Windows "
                 "PC. This build has no first-run setup screen yet -- do "
                 "not install it today."),
        internal_filename="1-wavr-core-the-brain-INSTALL-ON-ONE-PHONE-ONLY.apk",
        download_name="Wavr-Core-Android.apk",
        conflicts_with=("android-kiosk",),
    ),
    ProductSpec(
        id="android-kiosk",
        product="Wavr Wall Screen",
        platform="android",
        maturity="specialized",
        purpose=("Turns one Android screen into a dedicated Wavr wall "
                 "display. It registers itself as that device's HOME "
                 "SCREEN -- do not install it on your own phone or "
                 "tablet."),
        internal_filename="3-wavr-wall-screen-kiosk-optional.apk",
        download_name="Wavr-Kiosk-Android.apk",
        conflicts_with=("android-core-brain",),
    ),
)

_SUMLINE = re.compile(r"^\s*([0-9a-fA-F]{64})\s+\*?(.+?)\s*$")


def _sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _parse_checksum_file(path: Path) -> dict[str, str]:
    """`{filename: lowercase hex digest}` from a `sha256sum`-style file.

    Tolerant of the two line shapes `sha256sum` and `sha256sum -c` both
    accept (`HASH *name` and `HASH  name`) and of a leading `#` comment
    block, since both files in `_local/first-user-rc/` open with one.
    """
    table: dict[str, str] = {}
    if not path.is_file():
        return table
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _SUMLINE.match(line)
        if match:
            table[match.group(2)] = match.group(1).lower()
    return table


def _staleness(product_path: Path, core_binary: Path) -> dict[str, Any]:
    if not core_binary.is_file():
        return {"outdatedCheck": "core-binary-missing", "outdated": False,
                "outdatedReason": None}
    core_mtime = core_binary.stat().st_mtime
    if product_path.stat().st_mtime < core_mtime:
        return {
            "outdatedCheck": "core-binary",
            "outdated": True,
            "outdatedReason": (
                "Built before the Core binary currently at "
                f"desktop/sidecar/dist/{core_binary.name} "
                f"({_iso(core_mtime)}). Rebuild this installer."),
        }
    return {"outdatedCheck": "core-binary", "outdated": False,
            "outdatedReason": None}


def build_manifest(
    rc_dir: Path = RC_DIR,
    core_binary: Path = CORE_BINARY,
    products: tuple[ProductSpec, ...] = PRODUCTS,
) -> tuple[dict[str, Any], list[str]]:
    """Return `(manifest_dict, warnings)`.

    `warnings` are human-readable strings worth printing at generation time
    (today: only checksum-file disagreements) -- they are not written into
    the manifest itself, which stays a plain description of current state.
    """
    warnings: list[str] = []
    checksum_tables = {name: _parse_checksum_file(rc_dir / name)
                        for name in CHECKSUM_FILES}

    entries: list[dict[str, Any]] = []
    for spec in products:
        path = rc_dir / spec.internal_filename
        entry: dict[str, Any] = {
            "id": spec.id,
            "product": spec.product,
            "platform": spec.platform,
            "maturity": spec.maturity,
            "purpose": spec.purpose,
            "internalFilename": spec.internal_filename,
            "downloadName": spec.download_name,
            "conflictsWith": list(spec.conflicts_with),
            "available": False,
            "sizeBytes": None,
            "sha256": None,
            "modifiedAt": None,
            "downloadPath": None,
            "outdatedCheck": "not-applicable",
            "outdated": False,
            "outdatedReason": None,
            "missingReason": None,
        }

        if not path.is_file():
            entry["missingReason"] = (
                f"'{spec.internal_filename}' was not found in "
                f"{rc_dir.name}/.")
            entries.append(entry)
            continue

        digest = _sha256(path)
        entry.update(
            available=True,
            sizeBytes=path.stat().st_size,
            sha256=digest,
            modifiedAt=_iso(path.stat().st_mtime),
            downloadPath=f"/download/{spec.download_name}",
        )
        if spec.check_staleness_against_core:
            entry.update(_staleness(path, core_binary))

        for sums_name, table in checksum_tables.items():
            recorded = table.get(spec.internal_filename)
            if recorded and recorded != digest:
                message = (
                    f"{sums_name} lists {recorded} for "
                    f"'{spec.internal_filename}', but the file on disk "
                    f"hashes to {digest} right now. {sums_name} is stale; "
                    "the manifest uses the hash computed just now, not the "
                    f"one recorded in {sums_name}.")
                warnings.append(message)
                entry.setdefault("checksumFileMismatches", []).append(
                    {"file": sums_name, "recorded": recorded})

        entries.append(entry)

    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": _iso(datetime.now(tz=timezone.utc).timestamp()),
        "generatedBy": "scripts/gen_releases_manifest.py",
        "sourceDir": "/".join(rc_dir.relative_to(ROOT).parts)
        if rc_dir.is_relative_to(ROOT) else str(rc_dir),
        # This string is printed as the FIRST line of the download page, so
        # it is the first sentence anybody reads there. It used to end "not for
        # distribution beyond this test", which is true of the build and reads,
        # to the one person it is addressed to, as "you are not supposed to have
        # this file" -- exactly the wrong thing to say to somebody who was just
        # invited to download it. Same facts, aimed at the reader.
        "note": ("A pre-release build, made for you to try at home. It is not "
                 "code-signed and is not on any app store, so Windows and "
                 "Android will each warn you once -- the pages below say what "
                 "to expect. Please keep it to yourself for now."),
        "products": entries,
    }
    return manifest, warnings


def write_manifest(
    manifest_path: Path = MANIFEST_PATH,
    rc_dir: Path = RC_DIR,
    core_binary: Path = CORE_BINARY,
    products: tuple[ProductSpec, ...] = PRODUCTS,
) -> tuple[dict[str, Any], list[str]]:
    manifest, warnings = build_manifest(rc_dir=rc_dir, core_binary=core_binary,
                                         products=products)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    return manifest, warnings


def main() -> int:
    manifest, warnings = write_manifest()
    for warning in warnings:
        print(f"WARNING: {warning}")
    available = sum(1 for e in manifest["products"] if e["available"])
    total = len(manifest["products"])
    print(f"wrote {MANIFEST_PATH} ({available}/{total} artifacts present)")
    for entry in manifest["products"]:
        if not entry["available"]:
            print(f"  missing: {entry['product']} -- {entry['missingReason']}")
        elif entry["outdated"]:
            print(f"  outdated: {entry['product']} -- {entry['outdatedReason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
