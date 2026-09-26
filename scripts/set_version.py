"""Set the product version everywhere it is declared, from one argument.

    python scripts/set_version.py 0.5.0

`VERSION` is the source of truth and backend/tests/test_the_version_is_one_number.py
fails when any declaration disagrees with it; this script is the one way to move
them together, so a bump is one command instead of eleven hand edits. It also
moves the files that test does not read but that carry the same number (the npm
lockfiles, the Cargo lockfiles), so none is left stale.

The files it moves, and the pattern each is matched by, live in
`scripts/version_surfaces.py` — the same list the test imports, so a surface
known to only one side is no longer possible.

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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from version_surfaces import ANDROID_MANIFESTS, REPO, SURFACES  # noqa: E402

SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def bump(v: str, repo: Path = REPO) -> list[str]:
    """Move every surface in `repo` to version `v`. Returns what actually changed.

    Takes `repo` so the same code path a real bump runs can be pointed at a
    throwaway copy of the tree in a test.
    """
    major, minor, patch = (int(x) for x in v.split("."))
    code = major * 10000 + minor * 100 + patch
    changed = []

    for surface in SURFACES:
        if surface.optional and not surface.exists(repo):
            continue
        if surface.rewrite(v, repo):
            changed.append(surface.path)

    for rel in ANDROID_MANIFESTS:
        p = repo / rel
        with open(p, encoding="utf-8", newline="") as f:
            text = f.read()
        current = int(re.search(r"versionCode (\d+)", text).group(1))
        if current < code:
            p.write_text(text.replace(f"versionCode {current}", f"versionCode {code}", 1),
                         encoding="utf-8", newline="")
            changed.append(f"{rel} (versionCode {current} -> {code})")
    return changed


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not SEMVER.match(argv[0]):
        print("usage: python scripts/set_version.py MAJOR.MINOR.PATCH")
        return 64
    v = argv[0]
    changed = bump(v)
    print(f"version {v}:\n  " + "\n  ".join(changed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
