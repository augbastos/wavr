"""A document that is confidently wrong is worse than no document at all.

Every check here was written after a sentence in this repository described
something the code had stopped doing. Not one of them was a typo: each was true
when it was written, and went quietly false while nobody was reading it.

  * `WAVR-PROTOCOL.md` §4.2 listed the paths exempt from the token requirement
    and named six of them. The code exempts three more — every module under
    `/js/`, everything under `/experiences/`, and the JavaScript SDK — which is
    forty-six files the spec's own **MUST NOT** clause forbade. An implementer
    following the document would have written a Core that cannot serve its own
    dashboard, and an auditor reading it would have flagged the real Core as
    non-conformant.
  * `space_store.py` said `device_role_for_person()` "is NOT yet consulted" and
    that "nothing in this file changes what any credential reaches today". By
    then it was on the authorization path of every authenticated request. The
    most dangerous kind of stale comment: one that says a security control is
    inert.
  * `README.md` and `PRODUCT.md` called the frontend a single static HTML file
    for as long as it took the shell to become 44 modules.
  * `CLAUDE.md` sent every new reader to `docs/ROADMAP.md`, deleted long ago.
  * `localize.py` pointed at `index.html:3206` for a function that now lives in
    another file entirely — a line number outliving the file it indexed.

## What this file does about it

Nothing here reads a document for style. Each test extracts a CLAIM and
compares it to the thing claimed, so the document cannot drift back on its own:
the protocol's path list is diffed against `app.py`'s frozensets, the
changelog's version against `wavr.__version__`, the stated module count against
`ls frontend/js`, the "Read first" list against the filesystem.

`test_contracts.py` holds `EXTERNAL-PROVIDERS.md` to the code the same way and
is the pattern this follows: when the code moves, the test names the sentence
that has to move with it.
"""
from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

import pytest

import wavr

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend" / "wavr"
DOCS = REPO / "docs"
FRONTEND = REPO / "frontend"

PROTOCOL = DOCS / "WAVR-PROTOCOL.md"
README = REPO / "README.md"
PRODUCT = REPO / "PRODUCT.md"
REPO_CLAUDE_MD = REPO / "CLAUDE.md"
# AGENTS.md is the canonical, model-neutral contract; CLAUDE.md was reduced to
# a thin adapter over it. The "Read first" list moved with the content, so the
# checks below follow it -- they are about where a new reader is SENT, and that
# is now here for every agent and every human, not only for one vendor.
REPO_AGENTS_MD = REPO / "AGENTS.md"
SW = FRONTEND / "sw.js"
WHATS_NEW = FRONTEND / "js" / "whats-new.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _slice(text: str, start: str, end: str) -> str:
    """The text between two anchors, with a loud failure when an anchor moves.

    A `find` that returns -1 slices from the end of the string and every
    assertion below it passes on an empty string — a green test measuring
    nothing, which is the failure mode this whole file exists to catch.
    """
    a = text.find(start)
    assert a != -1, f"anchor moved, this test is now reading nothing: {start!r}"
    b = text.find(end, a)
    assert b != -1, f"anchor moved, this test is now reading nothing: {end!r}"
    return text[a:b]


# -- §4.2: the unauthenticated surface, as specified vs. as implemented --------

_EXEMPTIONS_START = "Certain paths are exempt from the token requirement"
_EXEMPTIONS_END = "A Core implementation **MUST NOT**"

# `` `POST /api/nodes/claim` `` and `` `/js/` `` both name a path; the HTTP verb
# is prose, not part of it.
_DOC_PATH = re.compile(r"`(?:GET|POST|PUT|DELETE)?\s*(/[^`\s]*)`")


def _spec_exempt_paths() -> set[str]:
    body = _slice(_read(PROTOCOL), _EXEMPTIONS_START, _EXEMPTIONS_END)
    return set(_DOC_PATH.findall(body))


def _code_exempt_paths() -> set[str]:
    from wavr.app import (_STATIC_SHELL_PATHS, _STATIC_SHELL_PREFIXES,
                          _UNAUTH_ONBOARDING_PATHS)
    return (set(_UNAUTH_ONBOARDING_PATHS) | set(_STATIC_SHELL_PATHS)
            | set(_STATIC_SHELL_PREFIXES))


def test_the_spec_lists_exactly_the_paths_the_code_exempts():
    """§4.2 IS the unauthenticated surface, so a gap in it is not a typo.

    The section ends with a **MUST NOT** forbidding any exemption the document
    does not already describe. While three real exemptions were missing from the
    list, that clause said the shipping Core was non-conformant — and a second
    implementation written from the spec would have refused to serve `/js/*`,
    which is the entire application.
    """
    spec, code = _spec_exempt_paths(), _code_exempt_paths()
    assert spec == code, (
        "§4.2 of docs/WAVR-PROTOCOL.md and app.py's exemption constants "
        f"disagree.\n  exempt in code, absent from the spec: {sorted(code - spec)}"
        f"\n  promised by the spec, not exempt in code: {sorted(spec - code)}")


def test_the_spec_says_why_a_page_cannot_be_gated_on_the_csrf_header():
    """The list alone reads like a hole somebody forgot to close.

    A reviewer's first instinct on seeing `/js/` unauthenticated is to put the
    `X-Wavr-Local` header in front of it, and that instinct breaks the product:
    a browser NAVIGATING to a URL sends no custom header, and no page-side code
    has run yet to add one. The reason has to be next to the exemption, or it
    gets 'fixed'.
    """
    body = _slice(_read(PROTOCOL), _EXEMPTIONS_START, _EXEMPTIONS_END).lower()
    assert "x-wavr-local" in body, "§4.2 does not mention the CSRF header at all"
    assert "navigat" in body, (
        "§4.2 exempts the shell from the CSRF header without saying why. The "
        "reason is that a browser navigating to a URL cannot send one.")


def test_the_must_not_clause_covers_every_shape_actually_present():
    """The clause used to allow two shapes — a one-time code, or a static asset
    — and then claim "every existing exemption follows exactly that shape". Two
    of them do not: `/api/nodes/claim` is bounded by a 192-bit capability, and
    the node data plane self-verifies a different credential entirely. A rule
    its own examples break is a rule nobody can apply.
    """
    tail = _slice(_read(PROTOCOL), _EXEMPTIONS_END, "### 4.3").lower()
    for shape in ("code", "capability", "credential space", "static asset"):
        assert shape in tail, (
            f"the MUST NOT clause in §4.2 does not admit the {shape!r} shape, "
            f"but §4.2 lists an exemption of exactly that kind")


# -- the person cap: a comment must not call a live control inert --------------

def test_space_store_does_not_describe_the_person_cap_as_inert():
    src = _read(BACKEND / "space_store.py")
    for dead in ("NOT yet consulted",
                 "nothing in this file changes what any credential reaches"):
        assert dead not in src, (
            f"space_store.py still says {dead!r}. `device_role_for_person()` is "
            f"read on every authenticated request through `auth._apply_person_cap`.")


def test_the_wiring_space_store_now_claims_is_really_there():
    """The docstring's replacement claim, held to the code that makes it true.

    Unwire any half of this and the module goes back to describing something it
    does not do — which is the defect, not the wiring.
    """
    app_src = _read(BACKEND / "app.py")
    assert "device_role_for_person" in app_src, (
        "app.py no longer calls device_role_for_person, so space_store.py's "
        "docstring is wrong again — this time in the reassuring direction")
    assert "person_role_fn=_person_role" in app_src, (
        "nothing injects the person cap into auth any more")


def test_pairing_still_does_not_mint_from_the_person_role():
    """The one clause of the original note that survived. `space_store.py` says
    the cap is a ceiling over a credential `devices.py` already issued, never
    the mint. The day `pairing.py` adopts it that sentence needs rewriting, and
    the change deserves a security reader rather than a silent merge.
    """
    src = _read(BACKEND / "pairing.py")
    assert "device_role_for_person" not in src, (
        "pairing.py now derives a credential role from a person role. That is a "
        "MINT, not a cap — update the axis-3 note at the top of space_store.py "
        "in the same change.")


# -- the frontend is not one file, and has not been for a while ----------------

_SINGLE_FILE = re.compile(r"single[-\s](?:static\s+)?(?:HTML\s+)?file", re.I)


@pytest.mark.parametrize("path", [README, PRODUCT, REPO_CLAUDE_MD, SW],
                         ids=lambda p: p.name)
def test_no_document_calls_the_frontend_one_file(path: Path):
    hits = [m.group(0) for m in _SINGLE_FILE.finditer(_read(path))]
    assert not hits, (
        f"{path.name} still calls the frontend {hits[0]!r}. It is an "
        f"index.html shell plus the modules in frontend/js/ — see "
        f"docs/FRONTEND-MODULES.md.")


def test_a_stated_module_count_is_the_real_one():
    """A document is free not to give a number. If it gives one it must be true:
    a count is the kind of detail a reader trusts precisely because nobody
    bothers to invent one.
    """
    real = len(list((FRONTEND / "js").glob("*.js")))
    assert real > 1, "frontend/js is empty — this test is measuring nothing"
    pattern = re.compile(r"(\d+)\s+(?:classic\s+)?(?:scripts|modules)\b")
    for path in (README, PRODUCT, REPO_CLAUDE_MD):
        for stated in pattern.findall(_read(path)):
            assert int(stated) == real, (
                f"{path.name} says {stated} frontend modules; frontend/js/ holds "
                f"{real}. Either the count moved or a module did.")


def test_the_service_worker_precaches_every_script_the_shell_loads():
    """`sw.js` claims its list "is every script index.html loads". That claim is
    the whole offline story: a script the shell loads and the list omits is not
    cached, so an offline launch breaks from that tag onward.

    `test_sw_shell.py` owns the drift check; this one only refuses to let the
    SENTENCE stand while it is false.
    """
    shell_scripts = set(re.findall(r'<script src="js/([\w.-]+\.js)"',
                                   _read(FRONTEND / "index.html")))
    assert shell_scripts, "index.html loads no js/ modules — anchor moved"
    listed = set(re.findall(r'"\./js/([\w.-]+\.js)"', _read(SW)))
    assert shell_scripts <= listed, (
        "sw.js says its SHELL list is every script index.html loads. Missing: "
        f"{sorted(shell_scripts - listed)}")


# -- AGENTS.md sends a reader somewhere that exists ----------------------------

def test_read_first_points_at_documents_that_exist():
    block = _slice(_read(REPO_AGENTS_MD), "## Read first", "\n## ")
    cited = re.findall(r"^- `([^`]+)`", block, re.M)
    assert len(cited) >= 3, "the Read-first list is not where this reads it"
    missing = [c for c in cited if not (REPO / c).is_file()]
    assert not missing, (
        f"AGENTS.md sends every new reader to files that do not exist: {missing}")


def test_the_adapter_defers_to_the_canonical_contract():
    """CLAUDE.md may add vendor-specific notes; it may not become a second
    source of truth. If it grows its own Read-first list, two documents start
    telling a reader different things and neither knows about the other."""
    claude = _read(REPO_CLAUDE_MD)
    assert "AGENTS.md" in claude, (
        "CLAUDE.md no longer points at AGENTS.md, so a Claude session never "
        "reads the canonical contract")
    assert "## Read first" not in claude, (
        "CLAUDE.md grew its own Read-first list. That list lives in AGENTS.md; "
        "two of them drift apart the moment one is edited.")


def test_the_deleted_roadmap_is_not_cited_as_if_it_existed():
    """It may be NAMED — several ADRs still do, and saying so is useful. What it
    may not do is appear in this file as somewhere to go."""
    assert not (DOCS / "ROADMAP.md").exists(), (
        "docs/ROADMAP.md exists again — then CLAUDE.md's note saying it was "
        "deleted is the thing that is now wrong")
    for line in (_read(REPO_CLAUDE_MD) + _read(REPO_AGENTS_MD)).splitlines():
        if "ROADMAP.md" not in line:
            continue
        assert re.search(r"\bno\b|deleted|dead", line, re.I), (
            f"the agent contract cites the deleted roadmap as a live document: "
            f"{line.strip()}")


# -- a comment may not index a file by line ------------------------------------

def test_no_backend_comment_indexes_the_shell_by_line_number():
    """`localize.py` said `index.html:3206`. Two things were wrong with it by
    then: the function had moved to `frontend/js/housemap.js`, and index.html
    had lost 14,000 lines — so the number pointed at real, unrelated markup.

    A symbol name survives a refactor. A line number in another file is wrong
    the first time anybody edits above it, and nothing ever tells you.
    """
    offenders = []
    for path in sorted(BACKEND.rglob("*.py")):
        for n, line in enumerate(_read(path).splitlines(), 1):
            if re.search(r"index\.html:\d+", line):
                offenders.append(f"{path.name}:{n}")
    assert not offenders, (
        f"these cite a line number inside frontend/index.html: {offenders}. "
        f"Name the symbol and the module instead.")


# -- the changelog is current for the version that ships -----------------------

def _whats_new():
    src = _read(WHATS_NEW)
    marker = re.search(r'window\.WAVR_APP_VERSION\s*=\s*"([^"]+)"', src)
    assert marker, "WAVR_APP_VERSION is no longer a plain literal"
    entries = re.findall(r'\{\s*version:\s*"([^"]+)",\s*date:\s*"([^"]+)"', src)
    assert entries, "no release entries found — this test is reading nothing"
    return marker.group(1), entries


def test_the_changelog_is_current_for_the_version_that_ships():
    """What's New sat at 0.2.0 while the product shipped two languages, a new
    navigation and a floor plan that no longer invents rooms. Nobody was told,
    because nothing connected the marker to the release.

    Bumping `wavr.__version__` now requires an entry that says what changed.
    That is the point: a changelog is only worth having while it is a promise.
    """
    marker, entries = _whats_new()
    assert marker == wavr.__version__, (
        f"What's New is current for {marker}, the product ships "
        f"{wavr.__version__}. Add the entry (frontend/js/whats-new.js) or fix "
        f"the marker — a household on the new version is shown the old notes.")
    assert entries[0][0] == marker, (
        f"the newest entry is {entries[0][0]}, the marker says {marker}; the "
        f"takeover card looks up the marker and would find nothing")


def test_every_release_carries_a_real_date_and_the_newest_is_first():
    _, entries = _whats_new()
    tomorrow = _dt.date.today() + _dt.timedelta(days=1)   # absorbs clock skew
    dates = []
    for version, raw in entries:
        try:
            d = _dt.date.fromisoformat(raw)
        except ValueError:
            pytest.fail(f"{version} is dated {raw!r}, which is not YYYY-MM-DD")
        assert d <= tomorrow, f"{version} is dated {raw}, in the future"
        dates.append((version, d))
    assert dates == sorted(dates, key=lambda vd: vd[1], reverse=True), (
        f"entries are not newest-first: {[(v, str(d)) for v, d in dates]}")
