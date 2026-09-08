"""Deleting what Wavr learned must not delete the Wavr somebody set up.

The Trust screen could show what is stored and could not act on it. For a
product whose entire pitch is privacy, "here is everything we keep about you,
and no, you cannot remove any of it" is the wrong end of the sentence.

## The distinction the whole feature turns on, and the test that matters most

"Delete what you know about me" and "delete my installation" are different
requests. `test_erasing_observations_leaves_the_installation_standing` is the
one to read first: after the biggest erase the default offers, the Space, the
people, the cameras, the paired devices and the floor plan are all still there.
Getting that wrong costs somebody an afternoon of setup and there is no undo.

## Why the classification is checked for completeness

A category added to `data_inventory.py` later has no entry in
`CLASSIFICATION`, and the safe reading of silence is "not erasable" — which is
what the code does. But silence is also how a genuinely personal new table
stays undeletable forever without anyone noticing, so the completeness test
fails until somebody decides which it is.
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.camera_store import CameraStore
from wavr.data_erasure import (CLASSIFICATION, DEFAULT_OBSERVATIONS,
                               OBSERVATION, SETUP, ErasureError, counts,
                               erasable, erase, plan)
from wavr.data_inventory import CATEGORIES, NEVER, PERSISTENT, SESSION
from wavr.fusion import FusionEngine
from wavr.hub import Hub
from wavr.storage import Storage


# -- the classification, which is the reviewable part -------------------------

def test_every_stored_category_is_classified():
    stored = {c.key for c in CATEGORIES
              if c.retention == PERSISTENT and c.table}
    missing = sorted(stored - set(CLASSIFICATION))
    assert not missing, (
        f"these are stored on disk and unclassified: {missing}. Silence reads "
        f"as 'not erasable', which is safe and is also how a personal new "
        f"table stays undeletable forever. Decide: observation or setup?")


def test_nothing_is_classified_that_is_not_stored():
    stored = {c.key for c in CATEGORIES
              if c.retention == PERSISTENT and c.table}
    extra = sorted(set(CLASSIFICATION) - stored)
    assert not extra, (
        f"classified but not stored on disk: {extra}. Offering to delete "
        f"something never written teaches people the screen is theatre.")


def test_only_two_kinds_exist():
    bad = {k: v for k, v in CLASSIFICATION.items() if v not in (OBSERVATION, SETUP)}
    assert not bad, f"unknown classification values: {bad}"


def test_the_default_set_is_observations_only():
    wrong = [k for k in DEFAULT_OBSERVATIONS
             if CLASSIFICATION.get(k) != OBSERVATION]
    assert not wrong, (
        f"the default erase would remove setup: {wrong}. There is no "
        f"shorthand for wiping an installation, on purpose.")


def test_never_and_session_categories_are_not_offered():
    """Nothing is written, so nothing can be deleted. Listing them as '0
    deleted' would be theatre."""
    offered = set(erasable())
    for c in CATEGORIES:
        if c.retention in (NEVER, SESSION):
            assert c.key not in offered, (
                f"{c.key} is {c.retention} and is being offered for deletion")


# -- planning refuses rather than skipping ------------------------------------

def test_an_unknown_category_is_refused_not_ignored():
    with pytest.raises(ErasureError) as e:
        plan(["occupancy_log", "not_a_thing"])
    assert "not_a_thing" in str(e.value)


def test_erasing_nothing_is_refused():
    with pytest.raises(ErasureError):
        plan([])


def test_a_never_stored_category_cannot_be_named():
    with pytest.raises(ErasureError):
        plan(["camera_frames"])


# -- the deletion itself ------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    """A Core's database with real rows in it, made the way the app makes it."""
    path = str(tmp_path / "wavr.db")
    app = create_app(sources=[], storage=Storage(path), hub=Hub(),
                     fusion=FusionEngine(), camera_store=CameraStore(path),
                     health_resolvers={})
    with TestClient(app):
        pass                       # let every store create its schema
    return path


def _seed(path):
    """Rows in both classes, so an erase can be shown to hit one and not the
    other."""
    from wavr.space_store import SpaceStore
    store = SpaceStore(path)
    store.create_space("The flat", kind="apartment")
    store.add_person("Augusto", role="owner")

    CameraStore(path).add("hall", "sala", "rtsp://127.0.0.1/x", 0.6)

    # Through the real store, so the schema is whatever the product creates
    # rather than whatever this test guessed. The log is edge-triggered, so the
    # room has to actually change state to leave a row.
    from wavr.occupancy_log import OccupancyLog
    log = OccupancyLog(path)
    for i in range(5):
        log.append_if_changed("sala", bool(i % 2 == 0), 0.9, None,
                              f"2026-09-0{i + 1}T10:00:00+00:00")


def test_counts_are_reported_before_anything_is_deleted(db):
    _seed(db)
    n = counts(db, ["occupancy_log"])
    assert n["occupancy_log"] > 0, (
        "the number a person is shown before confirming has to be the real one")


def test_erasing_deletes_the_rows_and_says_how_many(db):
    _seed(db)
    seeded = counts(db, ["occupancy_log"])["occupancy_log"]
    assert seeded > 0, "the fixture seeded nothing, so this proves nothing"
    out = erase(db, ["occupancy_log"])
    assert out["deleted"]["occupancy_log"] == seeded
    assert out["before"]["occupancy_log"] == seeded
    assert counts(db, ["occupancy_log"])["occupancy_log"] == 0


def test_erasing_observations_leaves_the_installation_standing(db):
    """THE test. After the biggest erase the default offers, everything a
    person set up by hand is still there."""
    _seed(db)
    from wavr.space_store import SpaceStore

    erase(db, list(DEFAULT_OBSERVATIONS))

    store = SpaceStore(db)
    space = store.get_space()
    assert space is not None and space.name == "The flat", (
        "erasing history destroyed the Space")
    assert [p.display_name for p in store.list_people()] == ["Augusto"], (
        "erasing history destroyed the people")
    assert [c["name"] for c in CameraStore(db).list()] == ["hall"], (
        "erasing history destroyed the cameras")


def test_erasing_one_category_does_not_touch_another(db):
    _seed(db)
    before = counts(db, ["occupancy_log", "cameras"])
    erase(db, ["occupancy_log"])
    after = counts(db, ["occupancy_log", "cameras"])
    assert after["occupancy_log"] == 0
    assert after["cameras"] == before["cameras"], (
        "an erase reached past the category it was given")


def test_a_table_this_build_does_not_have_is_reported_not_hidden(db):
    """Silence about a table that could not be read is how a partial erase
    gets reported as a complete one."""
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE IF EXISTS occupancy_log")
        conn.commit()
    out = erase(db, ["occupancy_log"])
    assert out.get("tables_not_present"), (
        "a missing table was swallowed; the report claims a clean erase")


# -- the route ----------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    """⚠ `WAVR_DB` must be pinned, and this fixture is why.

    The erasure router is built with `db_path=cfg.db_path`, which comes from
    the ENVIRONMENT — not from the stores a test injects. Handing
    `create_app` a `Storage(tmp)` while leaving `WAVR_DB` unset points the
    route at the config default, `wavr.db` relative to the CWD, and pytest
    runs from `backend/`.

    The first version of this fixture did exactly that. The route test PASSED,
    by deleting 18,000 occupancy rows out of a developer's `backend/wavr.db` —
    a database it had never been pointed at, while asserting about a temporary
    one it never touched. For a feature whose entire job is deleting things,
    that is the worst possible way for a test to be green.
    """
    path = str(tmp_path / "wavr.db")
    monkeypatch.setenv("WAVR_DB", path)
    monkeypatch.delenv("WAVR_LOCAL_TOKEN", raising=False)
    app = create_app(sources=[], storage=Storage(path), hub=Hub(),
                     fusion=FusionEngine(), camera_store=CameraStore(path),
                     health_resolvers={})
    with TestClient(app, headers={"X-Wavr-Local": "1"}) as c:
        c.wavr_db = path
        yield c


def test_the_route_acts_on_the_database_this_app_is_using(client, tmp_path):
    """Pinned separately from any deletion, because the symptom of getting it
    wrong is a test that passes while destroying data somewhere else."""
    _seed(client.wavr_db)
    before = counts(client.wavr_db, ["occupancy_log"])["occupancy_log"]
    assert before > 0, "the fixture seeded nothing"

    r = client.post("/api/privacy/erase",
                    json={"categories": ["occupancy_log"], "confirm": True})
    assert r.status_code == 200, r.text
    assert r.json()["deleted"]["occupancy_log"] == before, (
        "the route reported a different number of rows than the database this "
        "app was given held — it is erasing somewhere else")
    assert counts(client.wavr_db, ["occupancy_log"])["occupancy_log"] == 0


def test_the_screen_can_ask_what_can_go(client):
    r = client.get("/api/privacy/erasable")
    assert r.status_code == 200, r.text
    body = r.json()
    keys = {c["key"] for c in body["categories"]}
    assert "occupancy_log" in keys
    assert "camera_frames" not in keys, "offering to delete what is never stored"
    assert body["default"], "no default set, so the button has no meaning"
    one = next(c for c in body["categories"] if c["key"] == "occupancy_log")
    assert one["kind"] == OBSERVATION
    assert one["label"] and one["what"], "no words for a person to read"
    assert "rows" in one, "no count, so nobody can see what they are agreeing to"


def test_erasing_without_confirm_is_refused(client):
    r = client.post("/api/privacy/erase", json={"categories": ["occupancy_log"]})
    assert r.status_code == 400
    assert "confirm" in r.text


def test_erasing_an_unknown_category_is_a_400_not_a_silent_noop(client):
    r = client.post("/api/privacy/erase",
                    json={"categories": ["nope"], "confirm": True})
    assert r.status_code == 400
    assert "nope" in r.text


def test_erasing_through_the_route_works_and_reports(client):
    _seed(client.wavr_db)
    r = client.post("/api/privacy/erase",
                    json={"categories": ["occupancy_log"], "confirm": True})
    assert r.status_code == 200, r.text
    assert r.json()["deleted"]["occupancy_log"] > 0


def test_the_route_defaults_to_observations_when_nothing_is_named(client):
    _seed(client.wavr_db)
    r = client.post("/api/privacy/erase", json={"confirm": True})
    assert r.status_code == 200, r.text
    touched = set(r.json()["deleted"])
    assert touched == set(DEFAULT_OBSERVATIONS)
    assert not any(CLASSIFICATION[k] == SETUP for k in touched)


# -- the table that holds both halves ------------------------------------------

def test_erasing_discoveries_keeps_what_a_person_decided(db):
    """The `known_devices` trap through a different door, found by an
    independent audit of every category.

    `discoveries` rows are raised by the discovery sweep — observation — but
    each carries a `status` written by somebody pressing Ignore or Accept, and
    stored nowhere else. A whole-table erase destroys those answers, and the
    next sweep re-raises every dismissed card within seconds, badge and all.
    `discovery_inbox`'s own docstring calls that "how an inbox trains people to
    ignore it".
    """
    from wavr.discovery_inbox import (DiscoveryInbox, KIND_DEVICE_NEW,
                                      STATUS_DISMISSED, STATUS_PENDING)
    inbox = DiscoveryInbox(db)
    inbox.observe(KIND_DEVICE_NEW, "aa:aa:aa:aa:aa:01", "Something new")
    inbox.observe(KIND_DEVICE_NEW, "aa:aa:aa:aa:aa:02", "Something else")
    decided = inbox.list_items(status=STATUS_PENDING)[0]
    inbox.decide(decided.discovery_id, STATUS_DISMISSED)

    out = erase(db, ["discoveries"])

    assert out["deleted"]["discoveries"] == 1, (
        "the erase took more than the unanswered card")
    assert inbox.get(decided.discovery_id) is not None, (
        "a dismissal a person pressed was deleted by \"delete what Wavr "
        "learned\" — it will be re-raised on the next sweep")
    assert inbox.get(decided.discovery_id).status == STATUS_DISMISSED
    assert not inbox.list_items(status=STATUS_PENDING), (
        "the unanswered cards were supposed to go")


def test_the_count_shown_matches_what_the_erase_removes(db):
    """A filtered category counts the same rows it deletes, or the number
    somebody agreed to is a different promise from the one kept."""
    from wavr.discovery_inbox import (DiscoveryInbox, KIND_DEVICE_NEW,
                                      STATUS_DISMISSED, STATUS_PENDING)
    inbox = DiscoveryInbox(db)
    for i in range(4):
        inbox.observe(KIND_DEVICE_NEW, f"bb:bb:bb:bb:bb:0{i}", f"Thing {i}")
    inbox.decide(inbox.list_items(status=STATUS_PENDING)[0].discovery_id,
                 STATUS_DISMISSED)

    shown = counts(db, ["discoveries"])["discoveries"]
    removed = erase(db, ["discoveries"])["deleted"]["discoveries"]
    assert shown == removed == 3, (shown, removed)


def test_a_row_filter_only_exists_where_it_can_be_written_honestly():
    """The escape hatch that must not become a habit. A filter is only correct
    when it isolates rows carrying NO human decision; if that clause cannot be
    written, the category is setup."""
    from wavr.data_erasure import ROW_FILTER
    assert set(ROW_FILTER) == {"discoveries"}, (
        f"a second row filter appeared: {sorted(ROW_FILTER)}. Before adding "
        f"one, check the clause really does exclude every row somebody "
        f"answered — otherwise the category belongs in SETUP.")
    for key, clause in ROW_FILTER.items():
        assert key in erasable(), f"{key} is filtered but not erasable"
        assert "status" in clause or "decided" in clause, (
            f"{key}'s filter does not mention a decision column, so it "
            f"probably does not exclude decisions: {clause!r}")


def test_ha_devices_is_deletable_but_never_by_default():
    """Every column is copied out of Home Assistant's registry; nobody types
    anything. It is an observation — and one whose erasure visibly changes the
    Network screen until the next import, so it follows `gateway_binding` and
    stays out of the default set."""
    assert CLASSIFICATION["ha_devices"] == OBSERVATION
    assert "ha_devices" not in DEFAULT_OBSERVATIONS, (
        "importing from Home Assistant again is a manual step; deleting it "
        "under the plain button would silently degrade device identity")
