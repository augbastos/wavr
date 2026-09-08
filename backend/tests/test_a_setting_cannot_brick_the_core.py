"""No switch on the Settings screen may stop the Core from starting.

## The defect

"Connect to other Cores" and "Accept sensor nodes" are both writable from
Settings, and `create_app` raised RuntimeError when either was on without
multidevice. So an operator who turned one on and did not turn on "Let other
devices connect" first had a Core that refused to boot — and the screen that
could have undone it is served by the process that no longer boots. The only
way out was a terminal and an environment variable, on a product whose whole
pitch is that it needs neither.

## Two layers, because one is not enough

The write is refused, which stops it happening again. And the start DEGRADES
instead of dying, which recovers an install that already has the bad pair on
disk — the first layer cannot help those, because they are already down.

The original intent is kept exactly: the peer and node routers are still not
mounted without multidevice, because they reference stores that do not exist.
The flag is treated as the off it effectively is, and said so loudly.
"""
from __future__ import annotations

import pytest

from wavr.settings_store import (
    CONSENT_PHRASE, SPECS_BY_KEY, SettingsError, SettingsStore,
)

PREREQ = [k for k, s in SPECS_BY_KEY.items() if s.requires]


def _store(tmp_path):
    return SettingsStore(str(tmp_path / "settings.db"), environ={})


def test_there_is_at_least_one_setting_with_a_prerequisite(tmp_path):
    """Otherwise everything below passes against an empty list."""
    assert PREREQ, "no setting declares `requires`, so this file proves nothing"


@pytest.mark.parametrize("key", PREREQ)
def test_a_setting_is_refused_until_its_prerequisite_is_on(tmp_path, key):
    store = _store(tmp_path)
    with pytest.raises(SettingsError) as caught:
        store.set(key, "1", consent=CONSENT_PHRASE)
    said = str(caught.value)
    # The message has to name BOTH switches in the words on the screen, or it
    # is a puzzle rather than an instruction.
    spec = SPECS_BY_KEY[key]
    assert spec.label in said, said
    for needed in spec.requires:
        assert SPECS_BY_KEY[needed].label in said, said


@pytest.mark.parametrize("key", PREREQ)
def test_it_is_accepted_once_the_prerequisite_is_on(tmp_path, key):
    store = _store(tmp_path)
    for needed in SPECS_BY_KEY[key].requires:
        store.set(needed, "1", consent=CONSENT_PHRASE)
    assert store.set(key, "1", consent=CONSENT_PHRASE) == "1"


@pytest.mark.parametrize("key", PREREQ)
def test_turning_it_off_is_never_refused(tmp_path, key):
    """A prerequisite gates switching ON. Refusing an OFF would be a second
    way to get stuck, which is the thing being fixed."""
    store = _store(tmp_path)
    assert store.set(key, "0", consent=CONSENT_PHRASE) == "0"


def _paths(app) -> set[str]:
    """Every endpoint the app actually serves.

    NOT `app.routes`. This app includes its routers through a `_IncludedRouter`
    wrapper, so `app.routes` holds one opaque entry per included router —
    thirty-two of them, each with no `path` at all — and the endpoints inside
    them are invisible to a scan of that list.

    The first version of these two tests scanned `app.routes` for
    `/api/nodes`. It could never have found one: the positive test failed
    against a Core that mounts nodes perfectly well, and the negative test
    passed **vacuously** — it would have gone on passing if the degrade had
    stopped working entirely, which is the single thing it exists to catch.

    `app.openapi()` is the app answering for itself, flattened.
    """
    return set(app.openapi().get("paths", {}))


def test_a_core_that_already_has_the_bad_pair_still_starts(tmp_path, monkeypatch):
    """The recovery layer. This raised RuntimeError, which is unrecoverable
    from the only screen that could fix it."""
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "wavr.db"))
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(tmp_path / "house.json"))
    monkeypatch.setenv("WAVR_LOCAL_TOKEN", "")
    monkeypatch.setenv("WAVR_MULTIDEVICE", "0")
    monkeypatch.setenv("WAVR_PEERS_ENABLED", "1")
    monkeypatch.setenv("WAVR_NODES_ENABLED", "1")

    from wavr.app import create_app

    app = create_app()          # must not raise
    paths = _paths(app)
    assert paths, "the Core started with no routes at all"

    # And the routers that need multidevice are still NOT mounted, which is
    # what the original RuntimeError was protecting.
    assert not any(p.startswith("/api/peers") for p in paths), sorted(
        p for p in paths if p.startswith("/api/peers"))
    assert not any(p.startswith("/api/nodes") for p in paths), sorted(
        p for p in paths if p.startswith("/api/nodes"))


def test_the_core_still_starts_normally_when_the_pair_is_right(tmp_path, monkeypatch):
    """The degrade must not have turned the feature off for everybody."""
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "wavr.db"))
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(tmp_path / "house.json"))
    monkeypatch.setenv("WAVR_LOCAL_TOKEN", "")
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_NODES_ENABLED", "1")

    from wavr.app import create_app

    app = create_app()
    paths = _paths(app)
    assert any(p.startswith("/api/nodes") for p in paths), (
        "nodes were switched off even though multidevice is on")
