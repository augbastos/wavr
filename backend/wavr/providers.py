"""What a source of spatial evidence declares about itself, before it says
anything about the house.

## The problem this solves

Wavr's own sources are hard-coded knowledge: `fusion.RESOLUTION_SCOPE` knows a
camera can count and a Wi-Fi scan cannot, `sensor_coverage` knows a BLE radio
sees a room, and `speedtest` knows it reaches the internet. That works because
one person wrote all of them.

It stops working the moment Wavr ingests something it did not write — a UWB
platform, an AR runtime, an enterprise positioning system. Each of those knows
things about itself that Wavr cannot infer: what it can observe, what its
confidence number MEANS, which coordinate frame it speaks in, and — the one a
household cares about most — whether using it sends anything off the premises.

A descriptor is that declaration. Adding a new kind of spatial technology should
become "write an adapter that declares itself", not "teach three modules about a
vendor".

## Two rules that make this honest rather than decorative

**A declaration is a claim, not an authorisation.** A provider saying
`precision_ceiling: "position"` does not make its coordinates trustworthy — it
tells Wavr the best this thing could possibly do, and `reliability` still decides
how much that is worth here. Nothing in a descriptor may raise trust; the
ceiling only ever caps it.

**Reach is not optional and has no safe default.** `REACH_LOCAL` means it never
leaves the machine, `REACH_CLOUD` means a third party sees something about this
home. There is no "unspecified" — a provider whose author did not say must be
treated as the most exposed until they do, because the opposite default is a
privacy failure that nobody notices.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from wavr.contracts import version as contract_version
from wavr.fusion import RESOLUTION_SCOPE, _SCOPE_RANK

# What kind of thing this is. The distinction that matters for fusion is whether
# a provider hands over EVIDENCE (which Wavr weighs) or a CONCLUSION it reached
# itself (which Wavr must weigh even more carefully, because the reasoning
# happened somewhere it cannot inspect).
KIND_SENSOR = "sensor"        # raw physical evidence: a radar, a camera, a PIR
KIND_DERIVED = "derived"      # evidence computed from something else: CSI, fusion of one vendor's own
KIND_SPATIAL = "spatial"      # arrives already reasoned: a positioning platform, an AR runtime
KIND_NETWORK = "network"      # observations about the network itself
KIND_PLATFORM = "platform"    # a device platform reporting its own capabilities

KINDS: frozenset[str] = frozenset({
    KIND_SENSOR, KIND_DERIVED, KIND_SPATIAL, KIND_NETWORK, KIND_PLATFORM})

# How far this provider reaches. Ordered from least to most exposed; the UI
# renders them in this order and a household reads down until it stops being
# comfortable.
REACH_LOCAL = "local"          # this machine only
REACH_LAN = "lan"              # the local network, nothing beyond it
REACH_INTERNET = "internet"    # contacts a server, but sends nothing about the home
REACH_CLOUD = "cloud"          # a third party receives something about this home

REACH_ORDER = (REACH_LOCAL, REACH_LAN, REACH_INTERNET, REACH_CLOUD)

# What a provider's own confidence number means, because 0.8 from two vendors is
# not the same 0.8 and copying one into a RoomState would be a category error.
CONF_PROBABILITY = "probability"    # calibrated: 0.8 means "right 80% of the time"
CONF_SCORE = "score"                # ordered but uncalibrated: higher is surer
CONF_QUALITY = "quality"            # signal quality, not certainty of presence
CONF_NONE = "none"                  # the provider does not express confidence

CONFIDENCE_SEMANTICS: frozenset[str] = frozenset({
    CONF_PROBABILITY, CONF_SCORE, CONF_QUALITY, CONF_NONE})


class ProviderError(ValueError):
    """A descriptor that cannot be trusted to describe anything."""


@dataclass(frozen=True)
class ProviderDescriptor:
    """One source of spatial evidence, describing itself.

    Frozen: a provider that could rewrite its own declaration at runtime could
    quietly widen what Wavr believes it may do.
    """

    provider_id: str
    label: str
    kind: str
    reach: str
    # What it can observe. Free-form strings rather than an enum because the
    # useful set genuinely differs per technology (an AR runtime reports `pose`,
    # a radar does not) and a closed enum would have to be edited for every new
    # integration — which is the coupling this module exists to remove.
    observes: tuple[str, ...] = ()
    # The finest answer this technology could ever give. Capped against fusion's
    # own ladder, so a provider cannot declare its way past physics.
    precision_ceiling: str = "house"
    confidence_semantics: str = CONF_NONE
    coordinate_frame: str = ""      # "" = reports no coordinates at all
    version: str = ""
    # The DESCRIPTOR's shape, from `contracts` — not the provider's own
    # software version, which is the `version` field above.
    # `contract_version`, not `version`: this dataclass HAS a field called
    # `version`, and inside a class body that name is already bound to the
    # field's default by the time this line runs — so a bare `version(...)`
    # would call the string "". The same shadowing has bitten this codebase
    # twice before.
    protocol_version: int = contract_version("provider_descriptor")
    # Credentials the operator must supply before this can work. Named so the UI
    # can say "needs an API key" instead of failing silently at first use.
    requires: tuple[str, ...] = ()
    notes: str = ""
    detail: dict = field(default_factory=dict)

    @property
    def leaves_the_home(self) -> bool:
        """Whether enabling this means something about the house goes elsewhere.

        The single question a privacy screen has to answer per provider, and the
        reason `reach` has no default.
        """
        return self.reach == REACH_CLOUD

    @property
    def needs_internet(self) -> bool:
        return self.reach in (REACH_INTERNET, REACH_CLOUD)

    def to_dict(self) -> dict:
        out = {
            "provider_id": self.provider_id,
            "label": self.label,
            "kind": self.kind,
            "reach": self.reach,
            "observes": list(self.observes),
            "precision_ceiling": self.precision_ceiling,
            "confidence_semantics": self.confidence_semantics,
            "needs_internet": self.needs_internet,
            "leaves_the_home": self.leaves_the_home,
            "protocol_version": self.protocol_version,
        }
        if self.coordinate_frame:
            out["coordinate_frame"] = self.coordinate_frame
        if self.version:
            out["version"] = self.version
        if self.requires:
            out["requires"] = list(self.requires)
        if self.notes:
            out["notes"] = self.notes
        if self.detail:
            out["detail"] = self.detail
        return out


def describe(provider_id: str, label: str, kind: str, reach: str,
             *, modality: str = "", **kw) -> ProviderDescriptor:
    """Build a descriptor, refusing the ones that would mislead.

    `modality`, when given, sets the precision ceiling from
    `fusion.RESOLUTION_SCOPE` — so a provider that maps onto an existing modality
    inherits Wavr's own judgement about what that technology can do rather than
    asserting its own. A provider that also passes `precision_ceiling` gets the
    LOWER of the two: a declaration may lower a ceiling, never raise it.
    """
    if not provider_id or not label:
        raise ProviderError("a provider needs an id and a human label")
    if kind not in KINDS:
        raise ProviderError(f"kind must be one of {sorted(KINDS)}")
    if reach not in REACH_ORDER:
        # No default on purpose. An unstated reach would resolve to the most
        # comfortable value at exactly the moment nobody is paying attention.
        raise ProviderError(
            f"reach must be one of {list(REACH_ORDER)} — a provider that does "
            f"not say how far it reaches cannot be enabled safely")
    conf = kw.pop("confidence_semantics", CONF_NONE)
    if conf not in CONFIDENCE_SEMANTICS:
        raise ProviderError(f"confidence_semantics must be one of "
                            f"{sorted(CONFIDENCE_SEMANTICS)}")

    declared = kw.pop("precision_ceiling", None)
    ceilings = []
    if modality:
        ceilings.append(RESOLUTION_SCOPE.get(modality, "house"))
    if declared is not None:
        if declared not in _SCOPE_RANK:
            raise ProviderError(f"unknown precision ceiling {declared!r}")
        ceilings.append(declared)
    ceiling = min(ceilings, key=lambda c: _SCOPE_RANK[c]) if ceilings else "house"

    return ProviderDescriptor(
        provider_id=provider_id, label=label, kind=kind, reach=reach,
        precision_ceiling=ceiling, confidence_semantics=conf,
        observes=tuple(kw.pop("observes", ()) or ()),
        requires=tuple(kw.pop("requires", ()) or ()),
        **kw)


class ProviderRegistry:
    """Everything Wavr can currently take spatial evidence from.

    In memory and rebuilt on every start: a provider is a property of what this
    Core has running, not something to persist and go stale. A registry loaded
    from disk would list a camera that was removed months ago.
    """

    def __init__(self):
        self._by_id: dict[str, ProviderDescriptor] = {}

    def register(self, descriptor: ProviderDescriptor) -> None:
        if descriptor.provider_id in self._by_id:
            raise ProviderError(
                f"{descriptor.provider_id} is already registered — two providers "
                f"sharing an id would make their evidence indistinguishable")
        self._by_id[descriptor.provider_id] = descriptor

    def get(self, provider_id: str) -> ProviderDescriptor | None:
        return self._by_id.get(provider_id)

    def all(self) -> list[ProviderDescriptor]:
        return [self._by_id[k] for k in sorted(self._by_id)]

    def reaching_beyond_the_home(self) -> list[ProviderDescriptor]:
        """The ones a privacy screen must name.

        Not "the ones that are enabled" — that is the connector store's job.
        This is "the ones that WOULD send something out if enabled", which is
        what somebody deciding whether to switch one on needs to know.
        """
        return [d for d in self.all() if d.leaves_the_home]

    def catalog(self) -> dict:
        """The compatibility view: what this Wavr can talk to, grouped by reach.

        Grouped by reach rather than by kind because that is the axis a person
        actually sorts on. "What can I use without anything leaving my house" is
        the question; "what is a derived provider" is not.
        """
        groups: dict[str, list] = {r: [] for r in REACH_ORDER}
        for d in self.all():
            groups[d.reach].append(d.to_dict())
        return {
            "providers": [d.to_dict() for d in self.all()],
            "by_reach": {r: groups[r] for r in REACH_ORDER if groups[r]},
            "any_leaves_the_home": bool(self.reaching_beyond_the_home()),
            "note": ("Every provider here declares how far it reaches. Wavr "
                     "works fully with only the local ones — nothing above "
                     "'local network' is required for any core feature."),
        }
