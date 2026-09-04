"""Wavr's own sources, declaring themselves through the provider contract.

This file is what stops `providers.py` from being a scaffold. A contract with no
implementer describes nothing; these are the real ones, and they are the
reference an external adapter is written against.

Every declaration here is checkable against the code it describes:

  * `reach` — `speedtest.py` names itself "THE single sanctioned external
    egress"; `narrator.py` says "only Ollama is LOCAL, the rest reach a cloud
    endpoint by default". Those are the two that leave, and they are the two
    marked as leaving.
  * `precision_ceiling` — taken from `fusion.RESOLUTION_SCOPE` via the
    `modality` argument, so it cannot drift from what fusion actually allows.
  * `coordinate_frame` — only the two providers that emit coordinates declare
    one, and the radar declares `sensor` because that is the frame it genuinely
    speaks in (see `spatial_frames`).

Nothing here is aspirational. A provider Wavr cannot currently talk to does not
belong in this list, however easy the integration looks — a catalog that lists
intentions is a catalog nobody can trust.
"""
from __future__ import annotations

from wavr import ha_presence
from wavr.providers import (
    CONF_PROBABILITY, CONF_QUALITY, CONF_SCORE, KIND_DERIVED, KIND_NETWORK,
    KIND_SENSOR, REACH_CLOUD, REACH_INTERNET, REACH_LAN, REACH_LOCAL,
    ProviderRegistry, describe,
)


def _builtin() -> list:
    """The providers that ship with Wavr and need no credential."""
    return [
        describe("network", "Network discovery", KIND_NETWORK, REACH_LAN,
                 modality="network",
                 observes=("presence", "device_identity"),
                 confidence_semantics=CONF_SCORE,
                 notes=("Watches which devices are on your network. One antenna "
                        "localizes to the house, never to a room.")),

        describe("ble", "Bluetooth", KIND_SENSOR, REACH_LOCAL,
                 modality="ble",
                 observes=("presence", "proximity", "device_identity"),
                 confidence_semantics=CONF_QUALITY,
                 notes=("Signal strength to this machine's own radio. Coarse "
                        "proximity, never a position.")),

        describe("camera", "Camera", KIND_SENSOR, REACH_LOCAL,
                 modality="camera",
                 observes=("presence", "count", "posture", "position"),
                 confidence_semantics=CONF_PROBABILITY,
                 coordinate_frame="room",
                 notes=("Person detection on frames that are never stored. "
                        "Reports positions only once calibrated.")),

        describe("mmwave", "mmWave radar", KIND_SENSOR, REACH_LOCAL,
                 modality="mmwave",
                 observes=("presence", "count", "position", "movement"),
                 confidence_semantics=CONF_SCORE,
                 # It reports relative to ITSELF. Wavr places it only when the
                 # mount is known; otherwise the coordinate is dropped.
                 coordinate_frame="sensor",
                 notes=("Sees through darkness and cloth, and can lose a person "
                        "who stays completely still.")),

        describe("pir", "Motion sensor", KIND_SENSOR, REACH_LAN,
                 modality="pir",
                 observes=("presence", "movement"),
                 confidence_semantics=CONF_SCORE,
                 notes=("Notices movement. Cannot count, and loses somebody who "
                        "stops moving.")),

        describe("node", "Sensor node", KIND_SENSOR, REACH_LAN,
                 modality="node",
                 observes=("presence",),
                 confidence_semantics=CONF_SCORE,
                 notes=("A sensor you flashed yourself. What it reports is "
                        "capped by the type YOU assigned it, never by what it "
                        "claims to be.")),

        describe("wifi_csi", "Wi-Fi sensing", KIND_DERIVED, REACH_LAN,
                 modality="wifi_csi",
                 observes=("presence", "movement"),
                 confidence_semantics=CONF_SCORE,
                 notes=("Presence from Wi-Fi channel state. A seam in Wavr "
                        "today, not a working source — it needs a radio that "
                        "exposes CSI.")),

        describe("sim", "Simulator", KIND_DERIVED, REACH_LOCAL,
                 modality="sim",
                 observes=("presence", "count", "position"),
                 confidence_semantics=CONF_SCORE,
                 coordinate_frame="room",
                 notes=("Invented data, for running Wavr with no hardware "
                        "attached. Never mixed with real sensing in a live "
                        "install.")),
    ]


def _integrations() -> list:
    """Providers that exist but need the operator to connect something."""
    return [
        # Imported rather than re-declared here. Two descriptions of one provider
        # drift, and the one written beside the code is the one that stays true:
        # this entry used to claim KIND_SPATIAL and CONF_SCORE, and the adapter
        # turned out to hand over raw binary sensors with no confidence attached
        # at all — a catalogue entry promising more than the implementation
        # delivers, which is the exact failure this file's docstring forbids.
        ha_presence.descriptor(),

        describe("mqtt", "MQTT broker", KIND_SENSOR, REACH_LAN,
                 observes=("presence",),
                 precision_ceiling="room",
                 confidence_semantics=CONF_SCORE,
                 requires=("broker address",),
                 notes="Local message broker. Nothing leaves your network."),
    ]


def _egress() -> list:
    """The two things that genuinely reach outside, named as such.

    These are not spatial providers — they consume or produce nothing about the
    house's geometry. They are in the catalog because the catalog is what the
    privacy screen reads, and a list of everything that could reach outside is
    worthless if it omits the two that do.
    """
    return [
        describe("speedtest", "Internet speed test", KIND_NETWORK,
                 REACH_INTERNET,
                 observes=("link_quality",),
                 confidence_semantics=CONF_QUALITY,
                 notes=("Contacts a public speed-test server. The M-Lab "
                        "provider publishes the caller's public IP address; "
                        "off by default.")),

        describe("cloud_llm", "Cloud AI assistant", KIND_DERIVED, REACH_CLOUD,
                 observes=(),
                 confidence_semantics=CONF_SCORE,
                 requires=("provider API key",),
                 notes=("The optional built-in assistant, when pointed at "
                        "OpenAI, Anthropic or Gemini rather than a local "
                        "Ollama. Sends your question and the context it needs "
                        "to a third party. Off by default; Wavr is fully "
                        "usable with no assistant at all.")),
    ]


def build_registry() -> ProviderRegistry:
    """Every provider this Wavr can talk to, rebuilt at start.

    Not persisted: a provider is a property of what this build has, not
    something to store and let go stale. A registry loaded from disk would list
    a camera removed months ago.
    """
    reg = ProviderRegistry()
    for descriptor in _builtin() + _integrations() + _egress():
        reg.register(descriptor)
    return reg
