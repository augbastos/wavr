"""A published hash that nobody can reproduce is decoration.

Three vendored bundles declare no version of their own, so THIRD-PARTY-NOTICES.md
identifies them by SHA-256 instead. That is the honest choice — a number a reader
can check beats a version number nobody can — but only if the number is actually
checkable, and the first two attempts at it were not:

  * the first hash was of the file INCLUDING the provenance banner that had just
    been prepended to it, so anyone verifying got a mismatch with no way to tell
    why;
  * the second was of the body but over RAW bytes, in a tree that is mixed
    CRLF/LF and whose files Git rewrites on checkout. That number disagrees with
    itself between a Windows clone and a Linux one.

So the contract is now specific: the hash covers everything after the banner,
with line endings normalised to LF. This test is the producer for that contract.
It recomputes each hash from the file on disk and fails if the notices, the
banner, or the bytes have drifted apart — which is the only way the claim stays
true after somebody updates a bundle and forgets the number.

It is deliberately not clever. If it fails, the fix is to re-run the command
printed in THIRD-PARTY-NOTICES.md and paste the result into both places.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
NOTICES = REPO / "THIRD-PARTY-NOTICES.md"

# (file, the byte sequence that closes its banner)
VENDORED = [
    (Path("mobile/vendor/jsqr.js"), b"*/"),
    (Path("frontend/vendor/qrcode.js"), b"//" + b"-" * 69),
]


def _body_after_banner(raw: bytes, terminator: bytes) -> bytes:
    """Everything after the provenance banner, line endings normalised.

    The banner is the one part of the file this project wrote, so it cannot be
    inside a hash that claims to identify upstream's bytes.
    """
    end = raw.index(terminator) + len(terminator)
    if terminator.startswith(b"//"):
        # A `//` banner ends with a rule line, and the file's own upstream header
        # opens with an identical rule. The banner's closing rule is the SECOND
        # occurrence, not the first.
        end = raw.index(terminator, end) + len(terminator)
    while end < len(raw) and raw[end] in (13, 10):
        end += 1
    return raw[end:].replace(b"\r\n", b"\n")


_ANY_PATH = re.compile(r"`([\w./-]+\.(?:js|mjs|css))`")


def _published_hashes() -> dict[str, str]:
    """Each published hash, keyed by the file it is stated about.

    Attribution is by the LAST path named before the hash, and every path in the
    document counts -- not only the ones this test checks. That distinction is
    the whole point: the qrcode section lists two files, and an earlier version
    of this parser tracked only the file it cared about, so it read the OTHER
    copy's hash and reported a mismatch that did not exist. A parser that
    silently attributes one file's number to another is exactly the failure
    these tests exist to catch, so it does not get to make it itself.
    """
    found: dict[str, str] = {}
    current: str | None = None
    for line in NOTICES.read_text(encoding="utf-8").splitlines():
        named = _ANY_PATH.findall(line)
        if len(named) == 1:
            current = named[0]
        elif len(named) > 1:
            current = None          # ambiguous line: attribute nothing to it
        m = re.search(r"sha256 ([0-9a-f]{64})", line)
        if m and current:
            found.setdefault(current, m.group(1))
    return found


@pytest.mark.parametrize("relative,terminator", VENDORED,
                         ids=[p.name for p, _ in VENDORED])
def test_the_banner_states_the_hash_the_file_actually_has(relative, terminator):
    path = REPO / relative
    assert path.exists(), f"{relative} is vendored code the notices promise exists"
    raw = path.read_bytes()
    actual = hashlib.sha256(_body_after_banner(raw, terminator)).hexdigest()

    head = raw[:4000].decode("utf-8", "replace")
    claimed = re.search(r"sha256 ([0-9a-f]{64})", head)
    assert claimed, f"{relative} publishes no hash in its banner"
    assert claimed.group(1) == actual, (
        f"{relative}: its banner claims {claimed.group(1)}, the bytes hash to "
        f"{actual}. Either the bundle was updated without recomputing, or the "
        f"hash was taken over the wrong span. See THIRD-PARTY-NOTICES.md for "
        f"the command."
    )


@pytest.mark.parametrize("relative,terminator", VENDORED,
                         ids=[p.name for p, _ in VENDORED])
def test_the_notices_agree_with_the_banner(relative, terminator):
    published = _published_hashes()
    key = relative.as_posix()
    assert key in published, (
        f"THIRD-PARTY-NOTICES.md names no hash for {key}. A vendored bundle with "
        f"no version and no hash has no identity at all."
    )
    actual = hashlib.sha256(
        _body_after_banner((REPO / relative).read_bytes(), terminator)).hexdigest()
    assert published[key] == actual, (
        f"THIRD-PARTY-NOTICES.md says {published[key]} for {key}; the file hashes "
        f"to {actual}. The notices and the file disagree, and a reader has no way "
        f"to know which one is stale."
    )


def test_every_vendored_bundle_names_its_licence():
    """The reason all of this exists: redistribution carries the notice with it."""
    for relative, _ in VENDORED:
        head = (REPO / relative).read_bytes()[:4000].decode("utf-8", "replace")
        assert "SPDX-License-Identifier:" in head, (
            f"{relative} ships inside the product with no SPDX identifier")
        assert re.search(r"[Cc]opyright|@license", head), (
            f"{relative} ships with no copyright or @license line")
