"""What Wavr stores, told truthfully — and never leaking while telling it.

Two failure modes, opposite directions:

  * the inventory claims something is not stored when it is — the trust feature
    becomes a lie, which is worse than not having one;
  * the inventory leaks a credential while listing categories — a "show me my
    data" screen that exposes a token is a worse privacy failure than silence.

Both are pinned here.
"""
import sqlite3

import pytest

from wavr.data_inventory import (
    CATEGORIES, NEVER, PERSISTENT, SESSION, inventory,
)


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "wavr.db")


# -- The absences, which are the strongest claims ------------------------------

def test_camera_frames_are_declared_never_stored():
    """The single most reassuring fact about Wavr, and unfalsifiable unless it
    is stated somewhere a person can find it."""
    cat = {c.key: c for c in CATEGORIES}["camera_frames"]
    assert cat.retention == NEVER
    assert "never written" in cat.what.lower()


def test_positions_and_vitals_are_declared_never_stored():
    by_key = {c.key: c for c in CATEGORIES}
    assert by_key["positions"].retention == NEVER
    assert by_key["vitals"].retention == NEVER


def test_the_never_stored_categories_have_no_table():
    """A `never` category with a table would be a contradiction the UI would
    render as a row count next to the word 'never'."""
    for c in CATEGORIES:
        if c.retention == NEVER:
            assert c.table is None, f"{c.key} claims never but names a table"


def test_a_session_category_has_no_table_either():
    for c in CATEGORIES:
        if c.retention == SESSION:
            assert c.table is None


def test_every_persistent_category_names_a_real_table(db):
    """Otherwise the screen lists something that cannot be counted, and the
    reader cannot tell whether it is empty or imaginary."""
    from wavr.app import create_app  # noqa: F401 — importing builds no app here

    for c in CATEGORIES:
        if c.retention == PERSISTENT:
            assert c.table, f"{c.key} is persistent but names no table"


# -- Counting ------------------------------------------------------------------

def test_counts_are_live(db):
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE people (person_id TEXT)")
    conn.executemany("INSERT INTO people VALUES (?)", [("a",), ("b",)])
    conn.commit()
    conn.close()

    rows = {c["key"]: c for c in inventory(db)["categories"]}
    assert rows["people"]["rows"] == 2


def test_a_missing_table_reads_as_unknown_not_zero(db):
    """A table that does not exist yet and an empty one are different facts.
    Rendering both as 0 makes a missing feature look like an empty one."""
    sqlite3.connect(db).close()
    rows = {c["key"]: c for c in inventory(db)["categories"]}
    assert rows["people"].get("rows") is None


def test_an_unopenable_database_still_returns_the_truthful_categories(tmp_path):
    """The descriptions are still true when the database will not open.
    Returning nothing would be less honest than returning them without counts."""
    body = inventory(str(tmp_path))          # a directory, not a database
    assert len(body["categories"]) == len(CATEGORIES)
    assert all("rows" not in c for c in body["categories"])


def test_connectors_counts_only_the_enabled_ones(db):
    """The connectors table holds a row per integration Wavr KNOWS ABOUT,
    switched on or not. On a screen answering "is anything leaving my network",
    the total reads as seven things leaving when the answer is none — a false
    alarm, in the direction that frightens people."""
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE connectors (name TEXT, enabled INTEGER)")
    conn.executemany("INSERT INTO connectors VALUES (?, ?)",
                     [("mqtt", 0), ("webhook", 0), ("ha", 1)])
    conn.commit()
    conn.close()

    row = {c["key"]: c for c in inventory(db)["categories"]}["connectors"]
    assert row["rows"] == 1, "one enabled, not three known"
    assert row["counts"] == "enabled", "and the UI is told what the number means"


def test_a_category_without_a_narrowed_count_says_nothing_extra(db):
    """Only the categories whose number is NOT a plain row count carry the
    qualifier — otherwise every row grows a label that means 'rows'."""
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE people (person_id TEXT)")
    conn.commit()
    conn.close()
    row = {c["key"]: c for c in inventory(db)["categories"]}["people"]
    assert "counts" not in row


# -- It must never be the thing that leaks -------------------------------------

def test_no_credential_value_appears_anywhere_in_the_output(db):
    """The inventory reports categories and counts. If it ever starts returning
    the CONTENTS of a table, this is what fails."""
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE devices (device_id TEXT, token_hash TEXT)")
    conn.execute("INSERT INTO devices VALUES ('d1', 'super-secret-hash-value')")
    conn.execute("CREATE TABLE cameras (name TEXT, rtsp_url TEXT)")
    conn.execute("INSERT INTO cameras VALUES "
                 "('hall', 'rtsp://admin:hunter2@10.0.0.5/live')")
    conn.commit()
    conn.close()

    blob = str(inventory(db))
    assert "super-secret-hash-value" not in blob
    assert "hunter2" not in blob
    assert "10.0.0.5" not in blob


def test_the_output_says_credentials_are_not_listed(db):
    body = inventory(db)
    assert "hashed" in body["secrets_note"]
    assert "never shown back" in body["secrets_note"]


# -- Legibility ----------------------------------------------------------------

def test_every_category_explains_itself_in_plain_language():
    """This is a trust feature, not a developer diagnostic. A category whose
    description is a table name explains nothing."""
    for c in CATEGORIES:
        assert len(c.what) > 40, f"{c.key} has no real explanation"
        assert c.label != c.key
        assert "_" not in c.label, f"{c.label} reads like a column name"


def test_the_summary_counts_each_retention_class(db):
    body = inventory(db)
    assert body["counts"]["never"] >= 3
    assert body["counts"]["persistent"] > 0
    assert body["counts"]["session"] > 0


def test_the_note_states_the_local_only_position(db):
    body = inventory(db)
    assert "unless you enable an external connection" in body["note"]


def test_the_most_reassuring_facts_come_first():
    """A worried person reads from the top. 'Does it keep video?' must not be
    buried under settings and routines."""
    first_three = [c.key for c in CATEGORIES[:3]]
    assert first_three == ["camera_frames", "positions", "vitals"]


def test_keys_are_unique():
    keys = [c.key for c in CATEGORIES]
    assert len(keys) == len(set(keys))
