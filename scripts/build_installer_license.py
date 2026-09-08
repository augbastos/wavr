"""Rebuild the page the Windows installer shows before somebody agrees.

`desktop/installer-license.txt` is `INSTALL-NOTICE.txt` followed by the AGPL,
and `tauri.conf.json` points the MSI and NSIS builders at it.

It is COMMITTED rather than generated during the build, because the build reads
it: a clean clone that had to run a script first would fail with a missing-file
error that says nothing about what to do. `test_installer_notice.py` fails when
it drifts from its two sources, so the copy in the tree cannot go stale.

    python scripts/build_installer_license.py
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOTICE = ROOT / "desktop" / "INSTALL-NOTICE.txt"
LICENSE = ROOT / "LICENSE"
OUT = ROOT / "desktop" / "installer-license.txt"
RULE = "=" * 74


def build() -> str:
    return (NOTICE.read_text(encoding="utf-8").rstrip()
            + "\n\n" + RULE + "\n\n"
            + LICENSE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    # CRLF: this is read by Windows installer tooling, and NSIS renders a
    # lone LF as one long unreadable paragraph.
    OUT.write_text(build(), encoding="utf-8", newline="\r\n")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
