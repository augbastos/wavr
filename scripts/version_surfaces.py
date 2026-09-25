"""The one list of files that carry Wavr's product version as a literal string.

`scripts/set_version.py` and `backend/tests/test_the_version_is_one_number.py`
used to each keep their own list of these files, and the lists drifted:
`project.json` carries the version but was missing from the test, so a bump
that forgot it would have shipped silently. This module is the one place
both read from, so a surface can no longer be known to only one side.

Each `Surface` names a file and a MULTILINE regex whose capturing group is
exactly the version value — nothing more, nothing less. That one convention
is what lets the same pattern both *read* the current value (`current()`) and
*rewrite* only that span (`rewrite()`), leaving everything else in the file
byte-for-byte untouched.

`NON_SURFACES` is the other half of the contract: every file in this tree
that contains the current version string but is deliberately NOT one of the
files above. `backend/tests/test_the_version_is_one_number.py`'s
`test_a_forgotten_surface_is_caught` greps the tree for the current version
and fails on anything that is neither a `Surface` nor in this allowlist, so
the next forgotten surface is caught the same day it is written, not the
next time somebody thinks to add a test for it.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

Pattern = str | Callable[[Path], str]


@dataclass(frozen=True)
class Surface:
    path: str
    pattern: Pattern
    optional: bool = False

    def _regex(self, repo: Path) -> str:
        return self.pattern(repo) if callable(self.pattern) else self.pattern

    def file(self, repo: Path = REPO) -> Path:
        return repo / self.path

    def exists(self, repo: Path = REPO) -> bool:
        return self.file(repo).is_file()

    def _read(self, repo: Path) -> str:
        # newline="" preserves each file's own line endings on disk; callers
        # normalise to "\n" for matching and restore CRLF on write.
        with open(self.file(repo), encoding="utf-8", newline="") as f:
            return f.read()

    def current(self, repo: Path = REPO) -> str:
        """The version value this surface declares right now."""
        text = self._read(repo).replace("\r\n", "\n")
        m = re.search(self._regex(repo), text, re.M)
        if not m:
            raise SystemExit(f"{self.path}: no version declaration matched {self._regex(repo)!r}")
        return m.group(1)

    def rewrite(self, new_version: str, repo: Path = REPO) -> bool:
        """Replace exactly the captured version span with `new_version`.

        Returns whether the file's content actually changed.
        """
        text = self._read(repo)
        crlf = "\r\n" in text
        body = text.replace("\r\n", "\n")

        def repl(m: re.Match[str]) -> str:
            start, end = m.span(1)
            return m.group(0)[: start - m.start()] + new_version + m.group(0)[end - m.start():]

        new_body, n = re.subn(self._regex(repo), repl, body, count=1, flags=re.M)
        if n == 0:
            raise SystemExit(f"{self.path}: no version declaration matched {self._regex(repo)!r}")
        new_text = new_body.replace("\n", "\r\n") if crlf else new_body
        if new_text != text:
            self.file(repo).write_text(new_text, encoding="utf-8", newline="")
            return True
        return False


def _cargo_lock_pattern(toml_rel: str) -> Callable[[Path], str]:
    """The lockfile entry for the crate named in `toml_rel`'s own `[package]`.

    The crate's own lockfile entry is found by name rather than by position,
    since a lockfile lists dependencies too and several of those carry their
    own unrelated `version = "..."` lines.
    """

    def pattern(repo: Path) -> str:
        toml_text = (repo / toml_rel).read_text(encoding="utf-8")
        name = re.search(r'^name = "([^"]+)"', toml_text, re.M).group(1)
        return rf'^name = "{re.escape(name)}"\nversion = "([^"]+)"'

    return pattern


# The nested entry npm writes for the root package itself, inside
# `"packages": { "": { ... } }` (lockfileVersion 2 and 3).
_NPM_LOCK_ROOT_ENTRY = r'^    "": \{\n(?:      .*\n)*?      "version": "([^"]+)"'

# The two Android manifests. `versionName` (the product version, a SURFACE
# below) and `versionCode` (a platform-monotonic integer, NOT the product
# version — see test_the_android_version_code_orders_with_the_product_version)
# both live in these same two files.
ANDROID_MANIFESTS: tuple[str, ...] = (
    "core-launcher/app/build.gradle",
    "mobile/android/app/build.gradle",
)

SURFACES: tuple[Surface, ...] = (
    Surface("VERSION", r"^(\d+\.\d+\.\d+)$"),
    Surface("backend/pyproject.toml", r'^version = "([^"]+)"'),
    Surface("backend/wavr/__init__.py", r'^__version__ = "([^"]+)"'),
    Surface("project.json", r'^    "version": "([^"]+)"'),
    Surface("desktop/package.json", r'^  "version": "([^"]+)"'),
    Surface("desktop/src-tauri/tauri.conf.json", r'^  "version": "([^"]+)"'),
    Surface("mobile/package.json", r'^  "version": "([^"]+)"'),
    Surface("desktop/src-tauri/Cargo.toml", r'^version = "([^"]+)"'),
    Surface("clients/desktop/Cargo.toml", r'^version = "([^"]+)"'),
    *(Surface(rel, r'versionName "([^"]+)"') for rel in ANDROID_MANIFESTS),
    Surface("desktop/package-lock.json", r'^  "version": "([^"]+)"', optional=True),
    Surface("desktop/package-lock.json", _NPM_LOCK_ROOT_ENTRY, optional=True),
    Surface("mobile/package-lock.json", r'^  "version": "([^"]+)"', optional=True),
    Surface("mobile/package-lock.json", _NPM_LOCK_ROOT_ENTRY, optional=True),
    Surface("desktop/src-tauri/Cargo.lock",
            _cargo_lock_pattern("desktop/src-tauri/Cargo.toml"), optional=True),
    Surface("clients/desktop/Cargo.lock",
            _cargo_lock_pattern("clients/desktop/Cargo.toml"), optional=True),
)

# Files that contain the current version string as CONTENT, not as a
# declaration set_version.py must move. Each entry says why it is not a
# surface, so the next person who finds their file flagged here knows
# whether to add a Surface or extend this list.
NON_SURFACES: dict[str, str] = {
    "frontend/js/whats-new.js": (
        "WAVR_APP_VERSION and the changelog entries are hand-written content "
        "(what changed, in what order), not a mechanical copy of VERSION. "
        "backend/tests/test_docs_match_the_code.py "
        "(test_the_changelog_is_current_for_the_version_that_ships) already "
        "fails if this drifts from wavr.__version__."
    ),
    "frontend/js/locale-pt.js": (
        "a translated comment labelling the release-notes block above it "
        "with the release it is for; it names a release, it does not "
        "declare the product's version."
    ),
    "scripts/set_version.py": (
        "the version in its own module docstring is a usage example for the "
        "command line, not a declaration."
    ),
}
