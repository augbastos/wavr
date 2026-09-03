"""Operator settings: the allow-list, the consent gate, and the rule that the
environment always beats the UI."""
import pytest

from wavr.settings_store import (
    CONSENT_PHRASE, SETTING_SPECS, SPECS_BY_KEY, SettingsError, SettingsStore,
    apply_stored_settings, coerce,
)


@pytest.fixture
def env():
    return {}


@pytest.fixture
def store(env):
    s = SettingsStore(":memory:", environ=env)
    yield s
    s.close()


# -- The allow-list is the security boundary ---------------------------------

def test_unknown_key_is_refused(store):
    with pytest.raises(SettingsError, match="unknown setting"):
        store.set("PATH", "/evil")
    with pytest.raises(SettingsError):
        store.set("WAVR_LOCAL_TOKEN", "hunter2")


def test_no_secret_is_storable():
    # If any of these ever become settable from the UI, that is a vulnerability,
    # not a feature. Assert their absence so adding one breaks a test.
    forbidden = {
        "WAVR_LOCAL_TOKEN", "WAVR_HA_TOKEN", "WAVR_TLS_CERT", "WAVR_TLS_KEY",
        "GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "WAVR_ANTHROPIC_KEY", "WAVR_OPENAI_KEY", "WAVR_DIAG_ENDPOINT",
    }
    assert {s.env for s in SETTING_SPECS} & forbidden == set()


def test_active_attack_primitives_stay_env_only():
    # ARP blocking and agent actuation must be exactly as hard to enable as
    # before this store existed.
    envs = {s.env for s in SETTING_SPECS}
    assert "WAVR_NET_BLOCKING" not in envs
    assert "WAVR_MCP_CONTROL" not in envs


def test_every_spec_targets_a_real_wavr_env_var():
    for spec in SETTING_SPECS:
        assert spec.env.startswith("WAVR_"), spec.env
        assert spec.label and spec.description, spec.key


# -- Consent -----------------------------------------------------------------

def test_sensitive_key_refuses_without_the_consent_phrase(store):
    with pytest.raises(SettingsError, match="acknowledgement"):
        store.set("lan_access", True)
    assert store.get("lan_access") is None


def test_sensitive_key_accepts_with_consent(store):
    assert store.set("lan_access", True, consent=CONSENT_PHRASE) == "1"
    assert store.get("lan_access") == "1"


def test_a_wrong_phrase_is_not_close_enough(store):
    with pytest.raises(SettingsError):
        store.set("lan_access", True, consent="yes")


def test_non_sensitive_key_needs_no_consent(store):
    assert store.set("instance_name", "Kitchen Pi") == "Kitchen Pi"


def test_turning_a_sensitive_key_OFF_also_needs_consent(store):
    # Symmetry matters: silently disabling LAN access would strand every paired
    # device with no explanation.
    with pytest.raises(SettingsError):
        store.set("lan_access", False)


# -- The environment always wins ---------------------------------------------

def test_env_beats_stored(store, env):
    store.set("instance_name", "FromUI")
    env["WAVR_INSTANCE_NAME"] = "FromEnv"
    eff = store.effective("instance_name")
    assert eff["value"] == "FromEnv" and eff["source"] == "env" and eff["locked"]


def test_stored_beats_default(store):
    store.set("instance_name", "FromUI")
    eff = store.effective("instance_name")
    assert eff["value"] == "FromUI" and eff["source"] == "stored" and not eff["locked"]


def test_default_when_nothing_is_set(store):
    eff = store.effective("instance_name")
    assert eff["value"] == "Wavr" and eff["source"] == "default"


def test_export_never_overwrites_an_existing_env_var(store, env):
    store.set("instance_name", "FromUI")
    store.set("port", 9001)
    env["WAVR_INSTANCE_NAME"] = "OperatorSetThis"
    applied = store.export_to_env(env)
    assert env["WAVR_INSTANCE_NAME"] == "OperatorSetThis"
    assert env["WAVR_PORT"] == "9001"
    assert applied == ["WAVR_PORT"]


def test_export_treats_an_empty_env_var_as_unset(store, env):
    store.set("instance_name", "FromUI")
    env["WAVR_INSTANCE_NAME"] = ""
    store.export_to_env(env)
    assert env["WAVR_INSTANCE_NAME"] == "FromUI"


def test_a_key_removed_from_the_allow_list_stays_inert(store, env):
    # Simulate a row left behind by an older version whose key we since dropped.
    store._conn.execute(                                  # noqa: SLF001
        "INSERT INTO settings (key, value, updated_ts) VALUES ('retired', '1', 'x')")
    store._conn.commit()                                  # noqa: SLF001
    assert store.export_to_env(env) == []
    assert env == {}


def test_unset_falls_back_to_the_default(store):
    store.set("instance_name", "FromUI")
    assert store.unset("instance_name") is True
    assert store.effective("instance_name")["source"] == "default"
    assert store.unset("instance_name") is False


def test_a_value_we_exported_ourselves_is_not_reported_as_locked(store, env):
    """Regression, found by restarting a real Core.

    `export_to_env` publishes stored values into the environment. On the NEXT
    boot, `effective()` read one of those back and concluded the operator had
    set it by hand — so the settings screen showed a value the user had just
    chosen as `env`-owned and un-editable. One restart and the UI became
    read-only for everything it had ever written."""
    store.set("instance_name", "FromUI")
    store.export_to_env(env)
    assert env["WAVR_INSTANCE_NAME"] == "FromUI"

    eff = store.effective("instance_name")
    assert eff["value"] == "FromUI"
    assert eff["source"] == "stored", "we put it there; it is not the operator's"
    assert eff["locked"] is False, "the UI must still be able to change it"


def test_a_value_the_operator_set_is_still_reported_as_locked(store, env):
    """The other half: the fix must not make a real `.env` look editable."""
    env["WAVR_INSTANCE_NAME"] = "SetByHand"
    store.set("instance_name", "FromUI")
    store.export_to_env(env)          # must refuse to overwrite

    eff = store.effective("instance_name")
    assert eff["value"] == "SetByHand"
    assert eff["source"] == "env" and eff["locked"] is True


def test_unsetting_stops_us_claiming_we_published_it(store, env):
    store.set("instance_name", "FromUI")
    store.export_to_env(env)
    store.unset("instance_name")
    # The exported value is still live in the environment until a restart, and
    # that is now what it honestly reads as.
    assert store.effective("instance_name")["source"] == "env"


# -- Validation --------------------------------------------------------------

@pytest.mark.parametrize("raw,expect", [
    (True, "1"), (False, "0"),
    ("yes", "1"), ("on", "1"), ("true", "1"), ("1", "1"),
    ("no", "0"), ("off", "0"), ("false", "0"), ("0", "0"),
])
def test_bool_coercion(raw, expect):
    assert coerce(SPECS_BY_KEY["lan_access"], raw) == expect


@pytest.mark.parametrize("raw", ["nonsense", "enabled", None, [], 7])
def test_an_unrecognised_bool_is_rejected_not_read_as_off(raw):
    # This test previously asserted the BUG: anything unrecognised became "0"
    # and the API answered 200. An operator sending {"value": "enabled"} would
    # be told their setting saved, while it stored "off" and nothing said so.
    with pytest.raises(SettingsError):
        coerce(SPECS_BY_KEY["lan_access"], raw)


def test_numeric_bounds_are_enforced(store):
    with pytest.raises(SettingsError, match="at least"):
        store.set("port", 80)
    with pytest.raises(SettingsError, match="at most"):
        store.set("port", 70000)
    assert store.set("port", 8443) == "8443"


def test_nan_and_infinity_are_rejected(store):
    # Both survive float() and both poison every downstream comparison.
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(SettingsError, match="finite"):
            store.set("fusion_threshold", bad)


def test_non_numeric_is_rejected(store):
    with pytest.raises(SettingsError, match="must be a number"):
        store.set("port", "eight thousand")


def test_choice_is_closed(store):
    with pytest.raises(SettingsError, match="must be one of"):
        store.set("bind_host", "0.0.0.0/0", consent=CONSENT_PHRASE)
    assert store.set("bind_host", "0.0.0.0", consent=CONSENT_PHRASE) == "0.0.0.0"


def test_string_cannot_smuggle_a_second_assignment(store):
    # A newline in a value is how you turn one env var into two.
    assert "\n" not in store.set("instance_name", "Kitchen\nWAVR_MULTIDEVICE=1")
    assert store.get("instance_name") == "Kitchen WAVR_MULTIDEVICE=1"


def test_empty_string_is_rejected(store):
    with pytest.raises(SettingsError):
        store.set("instance_name", "   ")


def test_string_is_length_bounded(store):
    assert len(store.set("instance_name", "x" * 500)) <= 128


# -- The UI payload ----------------------------------------------------------

def test_describe_carries_everything_the_ui_needs(store, env):
    env["WAVR_MULTIDEVICE"] = "1"
    rows = {r["key"]: r for r in store.describe()}
    assert len(rows) == len(SETTING_SPECS)
    lan = rows["lan_access"]
    assert lan["locked"] is True and lan["source"] == "env" and lan["sensitive"] is True
    assert lan["label"] and lan["description"]
    assert rows["bind_host"]["choices"] == ["127.0.0.1", "0.0.0.0"]


# -- The integration that makes any of this matter ----------------------------

def test_a_stored_setting_actually_reaches_the_running_config(monkeypatch):
    """The bug this exists to prevent: a store whose export nobody calls.

    Every other test here proves the value is validated and persisted. This one
    proves it is APPLIED — that `create_app()` publishes it before
    `load_config()` reads the environment. Without this assertion the feature
    can be entirely dead while the whole unit suite stays green, which is
    exactly what happened."""
    import os

    from wavr.app import create_app
    from wavr.camera_store import CameraStore
    from wavr.core_registry import CoreRegistry
    from wavr.discovery_inbox import DiscoveryInbox
    from wavr.fusion import FusionEngine
    from wavr.hub import Hub
    from wavr.space_store import SpaceStore
    from wavr.storage import Storage

    for key in [k for k in os.environ if k.startswith("WAVR_")]:
        monkeypatch.delenv(key, raising=False)

    settings = SettingsStore(":memory:", environ=os.environ)
    settings.set("instance_name", "Attic Pi")
    settings.set("fusion_threshold", 0.35)

    app = create_app(
        sources=[], storage=Storage(":memory:"), hub=Hub(), fusion=FusionEngine(),
        camera_store=CameraStore(":memory:"), health_resolvers={},
        space_store=SpaceStore(":memory:"), core_registry=CoreRegistry(":memory:"),
        settings_store=settings, discovery_inbox=DiscoveryInbox(":memory:"))

    # The value the operator saved is what the running app is configured with.
    from wavr.config import load_config
    assert load_config().instance_name == "Attic Pi"
    assert load_config().fusion_threshold == 0.35
    assert app is not None


def test_the_environment_still_beats_a_stored_setting_at_startup(monkeypatch):
    """The other half of the contract: an operator's own `.env` is never
    overridden by something a UI wrote."""
    import os

    from wavr.app import create_app
    from wavr.camera_store import CameraStore
    from wavr.core_registry import CoreRegistry
    from wavr.discovery_inbox import DiscoveryInbox
    from wavr.fusion import FusionEngine
    from wavr.hub import Hub
    from wavr.space_store import SpaceStore
    from wavr.storage import Storage

    for key in [k for k in os.environ if k.startswith("WAVR_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WAVR_INSTANCE_NAME", "SetByTheOperator")

    settings = SettingsStore(":memory:", environ=os.environ)
    settings.set("instance_name", "SetByTheUI")

    create_app(
        sources=[], storage=Storage(":memory:"), hub=Hub(), fusion=FusionEngine(),
        camera_store=CameraStore(":memory:"), health_resolvers={},
        space_store=SpaceStore(":memory:"), core_registry=CoreRegistry(":memory:"),
        settings_store=settings, discovery_inbox=DiscoveryInbox(":memory:"))

    from wavr.config import load_config
    assert load_config().instance_name == "SetByTheOperator"


def test_no_path_valued_setting_is_writable():
    """`WAVR_HOUSE_MAP` was briefly settable as a free-form string. Because
    `housemap.save_house_map` WRITES to that path, an unvalidated path setting
    is an arbitrary-file-write primitive. Nothing path-shaped belongs in the
    allow-list without a real path check."""
    path_shaped = {"WAVR_HOUSE_MAP", "WAVR_DB", "WAVR_TLS_CERT", "WAVR_TLS_KEY",
                   "WAVR_TLS_DIR"}
    assert {s.env for s in SETTING_SPECS} & path_shaped == set()


# -- The launcher wrapper ----------------------------------------------------

def test_apply_stored_settings_is_a_no_op_on_a_missing_db(tmp_path):
    env = {}
    # A settings db that cannot be opened must never stop Wavr booting.
    assert apply_stored_settings(str(tmp_path / "nested" / "no.db"), env) == []
    assert env == {}


def test_apply_stored_settings_exports_from_a_real_file(tmp_path):
    path = str(tmp_path / "wavr.db")
    s = SettingsStore(path, environ={})
    s.set("instance_name", "Attic Pi")
    s.close()
    env = {}
    assert apply_stored_settings(path, env) == ["WAVR_INSTANCE_NAME"]
    assert env["WAVR_INSTANCE_NAME"] == "Attic Pi"
