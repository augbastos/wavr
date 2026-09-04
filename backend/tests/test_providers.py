"""A provider declares itself, and a declaration can never buy trust.

Two properties hold this together:

  * **reach has no safe default** — an unstated one would resolve to the most
    comfortable value at exactly the moment nobody is checking, and the failure
    is silent and privacy-shaped;
  * **a ceiling only ever lowers** — a provider must not be able to declare its
    way past what the technology can physically do, or any adapter author could
    out-vote a calibrated camera by typing `"position"`.
"""
import pytest

from wavr.fusion import RESOLUTION_SCOPE
from wavr.provider_catalog import build_registry
from wavr.providers import (
    CONF_PROBABILITY, CONF_SCORE, KIND_SENSOR, KINDS, REACH_CLOUD,
    REACH_INTERNET, REACH_LAN, REACH_LOCAL, REACH_ORDER, ProviderError,
    ProviderRegistry, describe,
)


# -- Reach: no default, because the default would be the comfortable one -------

def test_a_provider_that_does_not_state_its_reach_is_refused():
    with pytest.raises(ProviderError, match="how far it reaches"):
        describe("x", "X", KIND_SENSOR, "")


def test_an_unknown_reach_is_refused():
    with pytest.raises(ProviderError):
        describe("x", "X", KIND_SENSOR, "probably-fine")


def test_only_cloud_counts_as_leaving_the_home():
    """`internet` contacts a server; `cloud` means a third party learns
    something ABOUT this house. Conflating them would either cry wolf over a
    speed test or wave through an AI provider."""
    assert describe("a", "A", KIND_SENSOR, REACH_INTERNET).leaves_the_home is False
    assert describe("b", "B", KIND_SENSOR, REACH_CLOUD).leaves_the_home is True
    assert describe("c", "C", KIND_SENSOR, REACH_LAN).needs_internet is False
    assert describe("d", "D", KIND_SENSOR, REACH_INTERNET).needs_internet is True


# -- A declaration is a claim, never an authorisation --------------------------

def test_a_modality_sets_the_ceiling_from_fusions_own_table():
    """So a provider inherits Wavr's judgement about what the technology can do
    rather than asserting its own."""
    d = describe("cam", "Cam", KIND_SENSOR, REACH_LOCAL, modality="camera")
    assert d.precision_ceiling == RESOLUTION_SCOPE["camera"] == "count"


def test_a_declaration_may_lower_a_ceiling():
    d = describe("cam", "Cam", KIND_SENSOR, REACH_LOCAL, modality="camera",
                 precision_ceiling="room")
    assert d.precision_ceiling == "room"


def test_a_declaration_may_not_raise_a_ceiling():
    """The load-bearing one. Otherwise any adapter author could out-vote a
    calibrated camera by typing "position" into a config file."""
    d = describe("pir", "PIR", KIND_SENSOR, REACH_LOCAL, modality="pir",
                 precision_ceiling="position")
    assert d.precision_ceiling == "room", "the modality's physics wins"


def test_an_unknown_ceiling_is_refused_rather_than_ignored():
    with pytest.raises(ProviderError, match="precision ceiling"):
        describe("x", "X", KIND_SENSOR, REACH_LOCAL, precision_ceiling="perfect")


def test_a_provider_with_no_modality_defaults_to_the_coarsest():
    d = describe("x", "X", KIND_SENSOR, REACH_LOCAL)
    assert d.precision_ceiling == "house"


# -- Input hygiene -------------------------------------------------------------

def test_an_unknown_kind_is_refused():
    with pytest.raises(ProviderError, match="kind must be"):
        describe("x", "X", "vibes", REACH_LOCAL)


def test_an_unknown_confidence_semantics_is_refused():
    """0.8 from two vendors is not the same 0.8. A provider that will not say
    what its number means cannot have it used."""
    with pytest.raises(ProviderError, match="confidence_semantics"):
        describe("x", "X", KIND_SENSOR, REACH_LOCAL,
                 confidence_semantics="vibes")


def test_a_provider_needs_a_human_label():
    with pytest.raises(ProviderError):
        describe("x", "", KIND_SENSOR, REACH_LOCAL)


def test_a_descriptor_cannot_rewrite_itself():
    """Frozen: a provider that could change its own declaration at runtime could
    quietly widen what Wavr believes it may do."""
    d = describe("x", "X", KIND_SENSOR, REACH_LOCAL)
    with pytest.raises(Exception):
        d.reach = REACH_CLOUD


# -- The registry --------------------------------------------------------------

def test_two_providers_cannot_share_an_id():
    """Their evidence would be indistinguishable, and reliability is keyed on
    identity."""
    reg = ProviderRegistry()
    reg.register(describe("dup", "One", KIND_SENSOR, REACH_LOCAL))
    with pytest.raises(ProviderError, match="already registered"):
        reg.register(describe("dup", "Two", KIND_SENSOR, REACH_LOCAL))


def test_the_registry_names_what_would_leave_the_home():
    reg = ProviderRegistry()
    reg.register(describe("local", "Local", KIND_SENSOR, REACH_LOCAL))
    reg.register(describe("cloudy", "Cloudy", KIND_SENSOR, REACH_CLOUD))
    assert [d.provider_id for d in reg.reaching_beyond_the_home()] == ["cloudy"]


def test_the_catalog_groups_by_reach_not_by_kind():
    """"What can I use without anything leaving my house" is the question a
    person sorts on. "What is a derived provider" is not."""
    cat = build_registry().catalog()
    assert list(cat["by_reach"]) == [r for r in REACH_ORDER if r in cat["by_reach"]]
    assert cat["by_reach"][REACH_LOCAL], "there are local providers"


# -- Wavr's own catalog is truthful --------------------------------------------

def test_every_shipped_provider_declares_a_valid_reach_and_kind():
    for d in build_registry().all():
        assert d.reach in REACH_ORDER
        assert d.kind in KINDS


def test_exactly_the_two_known_egress_paths_are_marked_as_such():
    """`speedtest.py` calls itself "THE single sanctioned external egress" and
    `narrator.py` says the non-Ollama engines reach a cloud endpoint. Those two,
    and nothing else, may be marked as reaching outside."""
    reg = build_registry()
    outward = {d.provider_id for d in reg.all() if d.needs_internet}
    assert outward == {"speedtest", "cloud_llm"}


def test_only_the_cloud_assistant_is_marked_as_leaving_the_home():
    """A speed test contacts a server; it does not tell anyone about the house.
    The distinction is the whole reason there are four reach levels."""
    reg = build_registry()
    assert {d.provider_id for d in reg.reaching_beyond_the_home()} == {"cloud_llm"}


def test_every_sensing_provider_is_local_or_lan():
    """No sensing modality may require the internet. If one ever does, this
    fails and the claim "Wavr works with no internet" needs revisiting."""
    for d in build_registry().all():
        if "presence" in d.observes:
            assert d.reach in (REACH_LOCAL, REACH_LAN), d.provider_id


def test_the_radar_declares_the_sensor_frame_it_actually_speaks():
    """It reports relative to itself. Declaring `room` here would re-introduce
    the bug where a person was drawn wherever they stood relative to the radar."""
    reg = build_registry()
    assert reg.get("mmwave").coordinate_frame == "sensor"
    assert reg.get("camera").coordinate_frame == "room"


def test_providers_that_report_no_coordinates_declare_no_frame():
    reg = build_registry()
    for pid in ("network", "ble", "pir"):
        assert reg.get(pid).coordinate_frame == ""


def test_every_ceiling_matches_what_fusion_allows_that_modality():
    """The catalog cannot drift from the engine. If somebody raises a modality's
    scope in fusion, this is what notices the catalog did not follow."""
    reg = build_registry()
    for modality, scope in RESOLUTION_SCOPE.items():
        d = reg.get(modality)
        if d is not None:
            assert d.precision_ceiling == scope, modality


def test_providers_needing_credentials_say_which():
    """So the UI can say "needs an API key" instead of failing at first use."""
    reg = build_registry()
    assert reg.get("home_assistant").requires
    assert reg.get("cloud_llm").requires
    assert not reg.get("ble").requires


def test_every_provider_explains_itself_to_a_person():
    for d in build_registry().all():
        assert d.label and d.label != d.provider_id
        if d.observes or d.needs_internet:
            assert d.notes, f"{d.provider_id} has no explanation"


def test_the_catalog_states_that_local_only_is_enough():
    cat = build_registry().catalog()
    assert "nothing above 'local network' is required" in cat["note"]
