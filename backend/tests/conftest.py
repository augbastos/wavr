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


def _served_paths(app) -> set[str]:
    """Every endpoint the app actually serves.

    NOT `app.routes`, and this is the whole point of the function.

    FastAPI 0.139 changed `include_router`: instead of copying each router's
    routes into the app, it appends ONE `_IncludedRouter` wrapper per router.
    This app includes thirty-two of them, so `app.routes` now holds thirty-two
    opaque entries with no `path` at all, and every endpoint reached through a
    router is invisible to a scan of that list. Including a router with three
    routes grows `app.routes` by one.

    Four tests in this suite enumerated `app.routes` looking for a path. Two
    were positive assertions and started failing, which is how this was found.
    The other two were NEGATIVE — "these routes must NOT be mounted when the
    prerequisite is off" — and they kept passing, silently, measuring nothing.
    They would have gone on passing if the protection they guard had been
    deleted outright. One of them guards a UWB session key.

    So there is one producer of this answer now, and it asks the app rather
    than reading its internals. `openapi()` is the app describing its own
    surface; it survives whatever FastAPI does to route storage next.
    """
    return set(app.openapi().get("paths", {}))


def _served_routes(app) -> list:
    """The real route objects, including the ones nested inside included routers.

    `served_paths` is the right answer when a name is all you need. This exists
    for the one test that needs the route ITSELF — the UWB claim endpoint, whose
    guarantee is that the claiming device comes from the token and never from a
    parameter, and the only honest way to check that is to read the signature of
    the function that will actually run.

    Walking FastAPI's internals is a cost, not a preference: a nested route has
    to be reached somehow. `test_the_route_walk_still_sees_everything` cross-
    checks this walk against `openapi()`, so if a future FastAPI changes the
    nesting again, the disagreement is reported instead of quietly returning a
    shorter list.
    """
    achadas: list = []
    vistos: set[int] = set()

    def andar(rotas) -> None:
        for r in rotas:
            if id(r) in vistos:
                continue
            vistos.add(id(r))
            # `_IncludedRouter` keeps the router it wrapped under
            # `original_router` and exposes no `routes` of its own — the first
            # version of this walk looked only for `.routes` and came back with
            # exactly the same blind list it was written to replace.
            dentro = getattr(r, "routes", None)
            if dentro is None:
                envelope = getattr(r, "original_router", None)
                dentro = getattr(envelope, "routes", None)
            if dentro:
                andar(dentro)
            if getattr(r, "path", None) and hasattr(r, "endpoint"):
                achadas.append(r)

    andar(app.routes)
    return achadas


# Handed to tests as fixtures rather than imported: `tests/` is not a package,
# so `from conftest import ...` works on some invocations of pytest and not on
# others, and a helper that only exists under the right rootdir is a helper
# nobody uses.
@pytest.fixture
def served_paths():
    return _served_paths


@pytest.fixture
def served_routes():
    return _served_routes


@pytest.fixture(autouse=True)
def _isolate_wavr_env(monkeypatch, _disposable_defaults):
    for key in list(os.environ):
        if key.startswith("WAVR_") or key in _ISOLATED_KEYS:
            monkeypatch.delenv(key, raising=False)
    # AFTER the clear, not before it. The two CWD-relative defaults are pinned
    # here rather than once per session because this fixture runs before every
    # test and would otherwise delete the pin — which is exactly what happened
    # the first time, leaving it inert. See `_disposable_defaults` for why the
    # pin exists at all.
    #
    # A test that genuinely wants the real default deletes the variable itself
    # (`test_config` does) and still sees `wavr.db`.
    for var, path in _disposable_defaults.items():
        monkeypatch.setenv(var, path)


@pytest.fixture(autouse=True)
def _forget_the_launcher():
    """Nothing a test says about the listening socket may reach the next one.

    `wavr.lan_reachability` remembers which address and port the launcher
    handed to uvicorn. That is genuinely one fact per process -- there is one
    socket -- so it lives in a module global, and a module global survives the
    test that set it.

    `test_max_body_size.py` calls `serve.main()` with uvicorn replaced, to
    exercise the body-size cap. A side effect of `main()` is recording the
    bind, so from that test onwards every later test believed the Core was on
    loopback. Four address tests then failed in the full run and passed alone,
    which is the shape that gets called flakiness and re-run instead of read.

    Reset before AND after: before, so a test starts from "no launcher has
    said anything", and after, so the poisoning cannot outlive the test that
    did it whichever order pytest picks.
    """
    from wavr import lan_reachability

    lan_reachability.note_bound_host("")
    lan_reachability.reset_cache()
    yield
    lan_reachability.note_bound_host("")
    lan_reachability.reset_cache()


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


@pytest.fixture(scope="session")
def _disposable_defaults(tmp_path_factory):
    """Where the CWD-relative defaults point during a test run.

    PREVENTION, not detection. `WAVR_DB` and `WAVR_HOUSE_MAP` default to a path
    relative to the working directory, and pytest runs from `backend/`. A
    fixture that injects `Storage(tmp_path)` but forgets the env pin leaves
    anything reading `cfg.db_path` — the erasure route, for one — pointed at
    `backend/wavr.db` instead. Not theoretical: a route test for the DELETE
    feature passed by erasing eighteen thousand rows out of a developer's local
    database while asserting about a temporary one it never touched.

    Detecting it afterwards turned out to be the wrong instrument. A developer
    machine legitimately has a Core running from `backend/` writing to that
    same file every few seconds, so an mtime check fails runs for reasons that
    have nothing to do with the tests — and a guard that cries wolf gets
    deleted, which leaves nothing at all.

    One directory for the whole session, applied per test by
    `_isolate_wavr_env` above (which would otherwise clear it).

    ## Why the house map is WRITTEN here rather than left to the default

    `housemap.DEFAULT_MAP` used to be a three-room house — `sala`, `quarto`,
    `quintal`, on a floor called `Térreo` — and it shipped as the fallback for
    every real install, so somebody putting Wavr in a workshop landed on a map
    of three rooms that do not exist. It is empty now, because an install that
    has not drawn its rooms genuinely has none.

    Thirty-seven tests depended on that fictional house without ever saying so.
    They assert about rooms, calibration spots, coverage and experience
    context, and they got their rooms from a PRODUCT default that was never
    meant to be a fixture. The fix is to make the dependency explicit —
    `SAMPLE_MAP` is the same three rooms, kept for this and for the simulated
    demo — not to keep a fake house in the product so the suite has something
    to stand on.

    A test that wants a FRESH install points `WAVR_HOUSE_MAP` at its own path
    and gets the empty default the product actually ships.
    """
    import json

    from wavr.housemap import SAMPLE_MAP

    d = tmp_path_factory.mktemp("session-defaults")
    house = d / "house.json"
    house.write_text(json.dumps(SAMPLE_MAP), encoding="utf-8")
    return {"WAVR_DB": str(d / "wavr.db"), "WAVR_HOUSE_MAP": str(house)}


@pytest.fixture(scope="session", autouse=True)
def _no_state_left_in_the_repo():
    """A file this run CREATED in the repo, named at the end of the session.

    Deliberately not "these files must not exist": `house.json` and `wavr.db`
    at the repo root are what you get from running the Core locally, which is a
    normal thing to do.

    Also deliberately not "these files must not CHANGE". That was tried, and it
    fails every run on any machine with a Core running from `backend/` — a
    background process writing to `backend/wavr.db` is not a test bug, and a
    check that reports it as one is noise that gets switched off. The fixture
    above removes the need: with the defaults pinned, a test cannot reach those
    paths at all.
    """
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
