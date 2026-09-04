"""Every documented shape Wavr publishes, and the version of each.

## Why one file

Versions were scattered: the experience context carried one, the trace carried
one, a provider descriptor carried one, the config export carried one — each
defined next to its producer, none of them visible together, and two of the
shapes carried none at all.

That is fine until somebody adds a ninth. A contract that ships unversioned
cannot be changed afterwards without breaking something silently, because there
is nothing in the payload for a consumer to check. And a developer asking "what
does this Core speak" should not have to find out one 404 at a time.

## The rule, which is narrower than it looks

**Adding a field is not a break.** Every consumer in this repository — and every
one the SDKs encourage — reads named fields and ignores the rest, so a new field
reaches an old client as something it never looks at.

**Removing a field, or changing what one MEANS, is a break.** That is the case a
version exists for, and specifically the second one: a removed field produces a
visible `KeyError` somewhere, while a field whose meaning changed is read
confidently and wrongly, forever, by everything that was written before.

So the bar for a bump is not "we changed something". It is "an old reader would
now be wrong".

## What a consumer does with a version it does not recognise

Not refuse. The SDKs surface `protocolAhead` and carry on reading known fields,
because refusing would break every installed application the day somebody
updates their Core — and the person who updated it is not the person whose app
stopped working.
"""
from __future__ import annotations

# Every published shape, and what it is at. Keys are stable identifiers; a
# consumer that hard-codes one is doing the right thing.
CONTRACTS: dict[str, int] = {
    # What an application is told about a room. The most-consumed shape here.
    # 2 (04/09): `sensors[].sensor_id` REMOVED and replaced by a derived
    # `label`, plus `source` and `simulated`. A removal, not an addition, so it
    # is the case an old reader cannot detect for itself -- the id was the
    # camera's or the node's operator-typed NAME, which this layer's own
    # docstring says never reaches an experience.
    "experience_context": 2,
    # The semantic event stream, on `/ws/events` and `/api/events/recent`.
    "spatial_events": 1,
    # What an experience declares it needs.
    "experience_manifest": 1,
    # A running experience, its room and its targets.
    "experience_session": 1,
    # Named places, and their external id mappings.
    "anchors": 1,
    # A recorded, replayable sequence of observations.
    "trace": 1,
    # What a source of spatial evidence declares about itself.
    "provider_descriptor": 1,
    # The inbound shape an external positioning system posts.
    "provider_observations": 1,
    # A Space's configuration, exported.
    "config_export": 1,
    # What Wavr can see, packaged for support.
    "diagnostic_bundle": 1,
    # Node <-> Core telemetry and enrolment. See docs/WAVR-PROTOCOL.md.
    "node_protocol": 1,
    # The Core-side contract an iOS client speaks.
    "apple_spatial": 1,
    # Anchor identity and alignment with an XR runtime.
    "openxr_interop": 1,
}


def version(name: str) -> int:
    """The version of one contract, or a loud failure.

    `KeyError` rather than a default, deliberately. A producer stamping a
    contract this file has never heard of is a contract nobody can version, and
    a `0` would let it ship looking like it had one.
    """
    return CONTRACTS[name]


def summary() -> dict:
    """Everything this Core speaks, for a developer and for the SDKs.

    One response rather than a discovery exercise: finding out a platform's
    surface one endpoint at a time is how people conclude a capability does not
    exist.
    """
    return {
        "contracts": dict(CONTRACTS),
        "compatibility": (
            "Adding a field is not a break — every consumer reads named fields "
            "and ignores the rest. A version goes up when a field is removed or "
            "changes meaning, which is the case an old reader cannot detect."),
        "if_this_core_is_newer": (
            "Keep reading the fields you know. The SDKs surface it as "
            "`protocolAhead` rather than refusing, because refusing would break "
            "every installed application the day somebody updates their Core."),
    }
