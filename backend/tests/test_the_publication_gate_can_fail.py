"""A gate nobody has watched fail is a gate nobody has tested.

`scripts/publication_gate.py` answers one question — is this repository safe to
make public — and the only interesting thing about its answer is whether it could
have been different. A scanner that matches nothing prints the same reassuring
"No blockers" as a scanner that works.

So each detector below is handed the exact shape it exists to catch, and asserted
to catch it. These are positive controls, not unit tests of regex syntax: if a
pattern is loosened, narrowed or deleted, the corresponding test names the
disclosure that stops being detected.

## And the rule the gate itself has to obey

The previous version of this gate worked from a denylist of the exact private
strings that had been removed from the tree — a coordinate, an account name,
device models — and excluded itself from its own scan so it would not report
them. It became the only file in the repository still carrying any of those
values, and the self-exclusion is what hid that.

The last test in this file is that one. A privacy checker cannot be the thing
that publishes the secret, and now something fails when it is.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GATE_PATH = REPO / "scripts" / "publication_gate.py"


def _load():
    spec = importlib.util.spec_from_file_location("publication_gate", GATE_PATH)
    mod = importlib.util.module_from_spec(spec)
    # Registered BEFORE execution: @dataclass resolves annotations through
    # sys.modules[cls.__module__], and a module loaded from a spec that is not
    # registered gets None there and raises during class creation.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gate():
    assert GATE_PATH.exists(), "the publication gate is missing"
    return _load()


# ---------------------------------------------------------------------------
# Each detector, handed what it is for
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line", [
    r'EXE = "C:\Users\someone\AppData\Local\App\x.exe"',  # publication-gate: synthetic
    r"path = 'C:/Users/someone/Desktop/notes.txt'",  # publication-gate: synthetic
    "HOME = '/home/someone/projects/thing'",  # publication-gate: synthetic
    'cache = "/Users/someone/Library/Caches"',  # publication-gate: synthetic
])
def test_it_sees_an_absolute_path_naming_an_account(gate, line):
    assert gate.ABSOLUTE_PATH.search(line), (
        "an absolute path carrying a local account name went undetected; that is "
        "the machine and whoever runs it")


@pytest.mark.parametrize("line", [
    "./scripts/run.sh",
    "from pathlib import Path",
    "usr/local/bin",
    "see /docs/INSTALL.md",
    "https://example.com/home/page",
])
def test_it_does_not_cry_about_ordinary_paths(gate, line):
    """A false blocker teaches people to ignore the tool, which costs more than
    the check is worth."""
    assert not gate.ABSOLUTE_PATH.search(line), f"false positive on {line!r}"


@pytest.mark.parametrize("line", [
    "WAVR_HOME_LAT=51.4778392",  # publication-gate: synthetic
    'home_latitude: "-33.8567844"',  # publication-gate: synthetic
    "HOUSE_LON = 18.4232430921",  # publication-gate: synthetic
    "casa_lat = 40.6892474",  # publication-gate: synthetic
])
def test_it_sees_a_doorstep_coordinate_in_a_home_context(gate, line):
    assert gate.HOME_COORD.search(line), (
        "a coordinate precise enough to be a doorstep, named as a home, went "
        "undetected")


def test_it_sees_a_precise_coordinate_pair_anywhere(gate):
    assert gate.COORD_PAIR_PRECISE.search("point = (51.5007292, -0.1246254)")  # publication-gate: synthetic


@pytest.mark.parametrize("line", [
    "lat = 51.5",
    "threshold = 0.523",
    "version = 1.234567",          # not a coordinate: no home word, no pair
    "timeout_seconds = 30.0",
])
def test_it_does_not_cry_about_ordinary_numbers(gate, line):
    assert not gate.HOME_COORD.search(line), f"false positive on {line!r}"
    assert not gate.COORD_PAIR_PRECISE.search(line), f"false positive on {line!r}"


@pytest.mark.parametrize("line,what", [
    ("aws_key = 'AKIAIOSFODNN7EXAMPLE'", "an AWS access key id"),  # publication-gate: synthetic
    ("token = ghp_" + "x" * 36, "a GitHub token"),
    ("-----BEGIN RSA PRIVATE KEY-----", "a private key block"),  # publication-gate: synthetic
    ("slack = 'xoxb-1234567890-abcdefghij'", "a Slack token"),  # publication-gate: synthetic
    ('password = "hunter2hunter2"', "a hard-coded credential"),  # publication-gate: synthetic
])
def test_it_sees_a_credential_shape(gate, line, what):
    hit = any(p.search(line) for p, _ in gate.CREDENTIALS)
    assert hit, f"{what} went undetected"


@pytest.mark.parametrize("line", [
    'password = ""',
    "api_key = os.environ['API_KEY']",
    'password = "${SECRET}"',
    "password: <your password here>",
])
def test_it_does_not_cry_about_a_placeholder(gate, line):
    hit = any(p.search(line) for p, _ in gate.CREDENTIALS)
    assert not hit, f"false positive on {line!r}"


@pytest.mark.parametrize("path", [
    ".env", "backend/.env", "app/local.properties", "release.keystore",
    "cert.p12", "data/wavr.db", "keys/id_rsa", "config/secrets.json",
])
def test_it_refuses_a_file_class_that_must_never_be_tracked(gate, path):
    hit = any(p.search(path) for p, _ in gate.FORBIDDEN_TRACKED)
    assert hit, f"{path} would have been allowed into a published repository"


@pytest.mark.parametrize("path", [
    ".env.example", ".env.sample", "backend/.env.template", "docs/env.md",
    "src/keystore_docs.md", "tests/test_db.py",
])
def test_it_allows_the_templates_that_document_configuration(gate, path):
    """`.env.example` is the file that tells a contributor which variables exist.
    Refusing it pushes that list into prose nobody keeps current."""
    hit = any(p.search(path) for p, _ in gate.FORBIDDEN_TRACKED)
    assert not hit, f"false positive on {path!r}"


def test_it_reads_a_branch_out_of_a_published_url(gate):
    found = gate.BRANCH_IN_URLS.findall(
        "https://raw.githubusercontent.com/owner/repo/main/scripts/install.sh")
    assert "main" in found


def test_it_does_not_read_the_api_host_as_a_branch(gate):
    """`api.github.com/repos/<owner>/<repo>/…` has an extra path segment, so the
    same pattern used to report the REPO name as a missing branch."""
    found = gate.BRANCH_IN_URLS.findall(
        "https://api.github.com/repos/owner/repo/releases/latest")
    assert not found, f"the API URL was read as naming branch {found!r}"


def test_the_waiver_must_be_written_in_the_line_it_excuses(gate):
    assert gate.WAIVER.search('X = r"C:\\Users\\someone\\x"  # publication-gate: synthetic')
    assert not gate.WAIVER.search('X = r"C:\\Users\\someone\\x"')


# ---------------------------------------------------------------------------
# The rule the gate itself has to obey
# ---------------------------------------------------------------------------

def test_the_gate_does_not_carry_the_secrets_it_looks_for():
    """It must contain SHAPES, never values.

    A privacy checker that lists the exact strings it is protecting has published
    them, and no amount of excluding itself from its own scan changes that. This
    test is the reason the denylist moved outside the repository.

    The check is structural rather than a list of forbidden strings, because a
    list of forbidden strings here would be the same mistake one file along.
    """
    source = GATE_PATH.read_text(encoding="utf-8")

    # A literal coordinate with 5+ decimals, outside the docstring examples that
    # teach the pattern. Any real one would look exactly like this.
    for match in re.finditer(r"-?\b\d{1,3}\.\d{5,}\b", source):
        line = source[:match.start()].count("\n") + 1
        raise AssertionError(
            f"publication_gate.py:{line} contains a high-precision coordinate "
            f"literal. The gate must know the SHAPE of a doorstep coordinate, "
            f"never one.")

    # No self-exclusion. The previous version skipped itself, which is how it
    # became the only file in the tree still carrying private values.
    assert "SELF" not in source, (
        "the gate excludes itself from its own scan again. That is what hid the "
        "private literals last time: whatever it cannot see, it cannot report.")

    # The denylist must be refused if it lives inside the repository.
    assert "startswith(os.path.abspath(os.curdir)" in source, (
        "the gate no longer refuses a denylist stored inside the repository, "
        "which is the exact mistake the option exists to avoid")


def test_the_gate_scans_every_tracked_file_including_itself():
    """No exemption by filename for the gate.

    `tracked_files()` returns `git ls-files` unfiltered. If a future change adds
    a filter, this fails and names why.
    """
    source = GATE_PATH.read_text(encoding="utf-8")
    body = source[source.index("def tracked_files"):]
    body = body[:body.index("\ndef ")]
    assert "publication_gate" not in body, (
        "the gate filters itself out of its own file list")
