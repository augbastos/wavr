"""Test isolation from the operator's local .env.

`wavr.config` calls `load_dotenv()` once at import, which populates os.environ from
the developer's `./.env` (WAVR_* flags, LLM keys). Tests assert against the getenv
DEFAULTS and must not be perturbed by whatever features the operator has enabled on
their own machine. This autouse fixture clears those variables before every test;
a test that needs a specific value still sets it explicitly with
`monkeypatch.setenv`, which runs after this fixture. `load_dotenv` is import-time
only, so cleared values are not re-read during the test.
"""
import os

import pytest

_ISOLATED_KEYS = ("GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")


@pytest.fixture(autouse=True)
def _isolate_wavr_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("WAVR_") or key in _ISOLATED_KEYS:
            monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# The test run must not leave state in the repository.
#
# `WAVR_HOUSE_MAP`, `WAVR_DB` and the TLS paths all default to CWD-RELATIVE
# names. A fixture that forgets to pin one writes into `backend/`, where the
# file silently becomes the DEFAULT that every subsequent test reads — in that
# run and in every run afterwards.
#
# That is not hypothetical. A config-import test wrote `backend/house.json` with
# an English room name, and three developer-mode tests began failing with
# "kitchen is not a room in the default map" — a message with no connection to
# the test that caused it, in a file nobody had touched. Finding that cost far
# more than the mistake was worth.
#
# So it fails at the end of the session instead, naming the file. Deliberately
# NOT "these files must not exist": `house.json` and `wavr.db` at the repo root
# are what you get from running the Core locally, which is a normal thing to do.
# What is checked is that the RUN created them.
# ---------------------------------------------------------------------------

_CWD_RELATIVE = ("house.json", "wavr.db", "cert.pem", "key.pem")


def _watched_paths():
    from pathlib import Path
    backend = Path(__file__).resolve().parents[1]
    for directory in (backend, backend.parent):
        for name in _CWD_RELATIVE:
            yield directory / name


@pytest.fixture(scope="session", autouse=True)
def _no_state_left_in_the_repo():
    before = {p for p in _watched_paths() if p.exists()}
    yield
    created = sorted(str(p) for p in _watched_paths()
                     if p.exists() and p not in before)
    if created:
        raise AssertionError(
            "this test run wrote files into the repository: "
            + ", ".join(created)
            + ". A fixture is missing an env pin — WAVR_HOUSE_MAP, WAVR_DB and "
              "the TLS paths are CWD-relative by default, and a file left in "
              "`backend/` becomes the default every other test reads.")
