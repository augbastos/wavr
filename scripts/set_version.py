"""Set the product version everywhere it is declared, from one argument.

    python scripts/set_version.py 0.5.0

`VERSION` is the source of truth and backend/tests/test_the_version_is_one_number.py
fails when any declaration disagrees with it; this script is the one way to move
them together, so a bump is one command instead of eleven hand edits. It also
moves the files that test does not read but that carry the same number (the npm
lockfiles, the Cargo lockfiles), so none is left stale.

Android's versionCode is not the product version: it only has to stay
monotonic and encode at least MAJOR*10000 + MINOR*100 + PATCH, so it is raised
to that value and never lowered.

Convention: VERSION names the release being prepared. It is bumped right after
a release.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def _sub(rel: str, pattern: str, repl, count: int = 1) -> bool:
    p = REPO / rel
    if not p.exists():
        return False
    # newline="" keeps each file's own line endings (some are CRLF).
    with open(p, encoding="utf-8", newline="") as f:
        text = f.read()
    crlf = "\r\n" in text
    new, n = re.subn(pattern, repl, text.replace("\r\n", "\n"), count=count, flags=re.M)
    if crlf:
        new = new.replace("\n", "\r\n")
    if n == 0:
        raise SystemExit(f"{rel}: no version declaration matched {pattern!r}")
    if new != text:
        p.write_text(new, encoding="utf-8", newline="")
    return new != text


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not SEMVER.match(argv[0]):
        print("usage: python scripts/set_version.py MAJOR.MINOR.PATCH")
        return 64
    v = argv[0]
    major, minor, patch = (int(x) for x in v.split("."))
    code = major * 10000 + minor * 100 + patch
    changed = []

    (REPO / "VERSION").write_text(v + "\n", encoding="utf-8", newline="\n")
    changed.append("VERSION")
    edits = [
        ("backend/pyproject.toml", r'^version = "[^"]+"', f'version = "{v}"'),
        ("backend/wavr/__init__.py", r'^__version__ = "[^"]+"', f'__version__ = "{v}"'),
        ("desktop/package.json", r'^(  "version": )"[^"]+"', rf'\g<1>"{v}"'),
        ("desktop/src-tauri/tauri.conf.json", r'^(  "version": )"[^"]+"', rf'\g<1>"{v}"'),
        ("mobile/package.json", r'^(  "version": )"[^"]+"', rf'\g<1>"{v}"'),
        ("desktop/src-tauri/Cargo.toml", r'^version = "[^"]+"', f'version = "{v}"'),
        ("clients/desktop/Cargo.toml", r'^version = "[^"]+"', f'version = "{v}"'),
        ("core-launcher/app/build.gradle", r'versionName "[^"]+"', f'versionName "{v}"'),
        ("mobile/android/app/build.gradle", r'versionName "[^"]+"', f'versionName "{v}"'),
    ]
    for rel, pattern, repl in edits:
        if _sub(rel, pattern, repl):
            changed.append(rel)
    # npm lockfiles: the root package's own version, in both places npm writes it.
    for rel in ("desktop/package-lock.json", "mobile/package-lock.json"):
        if (REPO / rel).exists() and _sub(rel, r'^(  "version": )"[^"]+"', rf'\g<1>"{v}"'):
            changed.append(rel)
        if (REPO / rel).exists():
            _sub(rel, r'(^    "": \{\n(?:      .*\n)*?      "version": )"[^"]+"', rf'\g<1>"{v}"')
    # Cargo lockfiles: the workspace crate's own entry.
    for lock, toml in (("desktop/src-tauri/Cargo.lock", "desktop/src-tauri/Cargo.toml"),
                       ("clients/desktop/Cargo.lock", "clients/desktop/Cargo.toml")):
        if not (REPO / lock).exists():
            continue
        name = re.search(r'^name = "([^"]+)"', (REPO / toml).read_text(encoding="utf-8"), re.M).group(1)
        if _sub(lock, rf'(^name = "{re.escape(name)}"\nversion = )"[^"]+"', rf'\g<1>"{v}"'):
            changed.append(lock)
    for rel in ("core-launcher/app/build.gradle", "mobile/android/app/build.gradle"):
        p = REPO / rel
        with open(p, encoding="utf-8", newline="") as f:
            text = f.read()
        current = int(re.search(r"versionCode (\d+)", text).group(1))
        if current < code:
            p.write_text(text.replace(f"versionCode {current}", f"versionCode {code}", 1),
                         encoding="utf-8", newline="")
            changed.append(f"{rel} (versionCode {current} -> {code})")
    print(f"version {v}:\n  " + "\n  ".join(changed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
