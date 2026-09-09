"""The site must not put a Download button in front of a file that is not there.

`site/public/assets/releases.json` is the manifest every download button on the
site is built from, and it is COMMITTED. `scripts/gen_releases_manifest.py`
writes it by looking at a local build directory and marking each product
available or not — which means the committed file records whatever happened to
be on one machine's disk when somebody last ran the generator.

That is exactly what had happened. All five products were committed with
`available: true`, `sha256` and a `downloadPath`, and the directory those paths
point into does not exist in this repository. Published, the site would have
offered five downloads that 404.

Worse, and this is why it is a test rather than a fix: `site/README.md` documents
that filling a release slot automatically HIDES the static "no release yet"
paragraph in the same card. So the honest note disappears at the same moment the
dead button appears. The failure removes its own warning.

## What this checks

An entry may claim `available` only if the file it names is really in the
repository. Nothing here needs to know whether a release exists — it compares the
manifest against the tree it ships with.

## What it does not check

Whether a PUBLISHED release exists on the forge. That is a fact about the outside
world, and a test that reached for it would be a network call in a unit suite.
The rule here is narrower and sufficient: do not advertise a file this repository
does not contain.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / "site" / "public" / "assets" / "releases.json"
SITE_PUBLIC = REPO / "site" / "public"


@pytest.fixture(scope="module")
def manifest() -> dict:
    assert MANIFEST.exists(), (
        "the releases manifest is missing; every download button on the site is "
        "built from it")
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_every_advertised_download_is_a_file_that_exists(manifest):
    offenders = []
    for entry in manifest.get("products", []):
        if not entry.get("available"):
            continue
        path = entry.get("downloadPath") or ""
        if not path:
            offenders.append(f"{entry.get('id')}: available with no downloadPath")
            continue
        target = SITE_PUBLIC / path.lstrip("/")
        if not target.is_file():
            offenders.append(
                f"{entry.get('id')}: available, downloadPath {path!r}, "
                f"nothing at {target.relative_to(REPO).as_posix()}")

    assert not offenders, (
        "the committed manifest advertises downloads this repository does not "
        "contain:\n  " + "\n  ".join(offenders)
        + "\n\nThe generator marks a product available by looking at a LOCAL build "
          "directory, and its output is committed. Regenerate it somewhere that "
          "directory is absent, or copy the artifacts in — but do not ship a "
          "button that 404s, especially since filling a slot hides the honest "
          "'no release yet' note beside it.")


def test_an_unavailable_entry_says_why(manifest):
    """`missingReason` is what the site shows instead of a button. Empty, the
    card is silently blank and a reader cannot tell whether the product exists."""
    silent = [e.get("id") for e in manifest.get("products", [])
              if not e.get("available") and not e.get("missingReason")]
    assert not silent, (
        f"unavailable with no reason given: {silent}. The site has nothing "
        f"honest to print in that card.")


def test_an_unavailable_entry_carries_no_download_path(manifest):
    """Belt and braces: a stale `downloadPath` on an unavailable entry is one
    edit away from being served."""
    leftovers = [e.get("id") for e in manifest.get("products", [])
                 if not e.get("available") and e.get("downloadPath")]
    assert not leftovers, (
        f"unavailable but still carrying a downloadPath: {leftovers}")


def test_the_manifest_still_describes_every_product(manifest):
    """The control.

    Everything above is a negative, and an EMPTY product list satisfies all of
    them. If the manifest ever loses its entries, the site quietly offers
    nothing and these tests applaud.
    """
    products = manifest.get("products", [])
    assert len(products) >= 3, (
        f"only {len(products)} product(s) in the manifest — the tests above pass "
        f"trivially on an empty list, so this is the check that they were "
        f"actually looking at something")
    for entry in products:
        assert entry.get("id"), "a product with no id"
        assert "available" in entry, f"{entry.get('id')}: no `available` field"
