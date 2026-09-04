"""The privacy screen must list every table, or it is not a privacy screen.

`data_inventory` opens by calling itself "one truthful answer to *what does this
thing actually store about my home?*" and says the list "must be complete".

It was not. FOURTEEN tables were missing when this test was written — five from
modules added the same week and nine that had never been listed, seven of those
already on the public branch. The most invasive was `assistant_log` —
every question a household member typed to the assistant and every answer it
gave, kept on disk and named nowhere a person could find it.

Nothing was hiding it. The list is written by hand and the schema is written
somewhere else, so the two drift the way any two hand-maintained lists drift:
silently, in the direction of the newer one. A trust feature that decays by
default is worse than none, because it keeps being cited after it stops being
true.

So the completeness is checked rather than intended. A table added tomorrow
fails this test until somebody either describes it for a household or writes
down why a household does not need it named.
"""
from __future__ import annotations

import re
from pathlib import Path

from wavr import data_inventory

PACKAGE = Path(data_inventory.__file__).parent

# `CREATE TABLE IF NOT EXISTS <name> (` — the one form this codebase uses. The
# trailing paren is load-bearing: without it this matched three COMMENTS that
# say "CREATE TABLE IF NOT EXISTS won't add a column to an existing table", and
# reported a table called `won`. A scan that invents tables is as useless as one
# that misses them, and it fails loudly rather than quietly, which is the only
# reason it was cheap to find.
CREATE = re.compile(
    r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([a-z_][a-z0-9_]*)\s*\(",
    re.IGNORECASE)

# Tables a household has no use for hearing about, each with the reason. This
# list is deliberately hard to add to: "it is internal" is not a reason, because
# every table is internal. The reason has to be that naming it would tell a
# person nothing about what Wavr knows about their home.
EXEMPT = {
    # Nothing about the home: a scratch table created by tests and fixtures.
}


def _declared() -> set[str]:
    out: set[str] = set()
    for cat in data_inventory.CATEGORIES:
        out |= set(cat.tables)
    return out


def _in_schema() -> set[str]:
    found: set[str] = set()
    for path in PACKAGE.glob("*.py"):
        found |= set(m.group(1).lower()
                     for m in CREATE.finditer(path.read_text(encoding="utf-8")))
    return found


def test_every_table_wavr_creates_is_named_on_the_privacy_screen():
    missing = _in_schema() - _declared() - set(EXEMPT)
    assert not missing, (
        "these tables are stored and not named on the privacy screen: "
        + ", ".join(sorted(missing))
        + " — describe each in data_inventory.CATEGORIES, or add it to EXEMPT "
          "with the reason a household does not need it named")


def test_the_inventory_does_not_claim_a_table_that_does_not_exist():
    """The other direction. A category naming a table nobody creates renders as
    a permanent "cannot tell" on the screen, which reads as a fault in Wavr
    rather than as a stale list."""
    phantom = _declared() - _in_schema() - set(EXEMPT)
    assert not phantom, f"declared but never created: {sorted(phantom)}"


def test_the_schema_scan_actually_finds_tables():
    """A regex that silently stops matching would make both tests above pass
    forever. This is the assertion that fails when the scan breaks rather than
    when the schema does."""
    found = _in_schema()
    assert len(found) > 20, f"the scan found only {sorted(found)}"
    assert "occupancy_log" in found and "devices" in found


def test_the_assistant_log_is_named_and_marked_sensitive():
    """The specific omission that motivated this file. It is the one table
    holding sentences a person typed, so it is the one whose absence mattered
    most and the one most likely to be dropped again in a refactor."""
    cat = next(c for c in data_inventory.CATEGORIES if c.key == "assistant_log")
    assert cat.sensitive
    assert "question" in cat.what.lower() and "answer" in cat.what.lower()


def test_a_grouped_category_counts_only_its_own_table():
    """`also` exists so one legible row can cover a feature split across
    tables. It must not make the number on the screen the sum of unrelated
    things: a count that quietly aggregates is a number nobody can check."""
    for cat in data_inventory.CATEGORIES:
        if cat.also:
            assert cat.table, f"{cat.key} has `also` but no table to count"
            assert cat.table not in cat.also
