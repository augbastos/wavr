import importlib

import pytest

from wavr.events import SensingEvent
from wavr.fusion import FusionEngine
from wavr.mcp import (
    FusionStateProvider,
    build_mcp_server,
    get_house_map,
    get_room_context,
    list_rooms,
)

HOUSE = {"rooms": [{"name": "sala", "x": 0, "y": 0, "w": 4, "h": 3}]}


def _ev(room, modality, presence, conf):
    return SensingEvent(room=room, modality=modality, presence=presence, motion=1.0,
                        breathing_bpm=None, heart_bpm=None, confidence=conf,
                        ts="2026-07-02T10:00:00+00:00")


class FakeProvider:
    """Minimal StateProvider stand-in for pure shape tests (mocked state)."""

    def __init__(self, rooms, states, house):
        self._rooms = rooms
        self._states = states
        self._house = house

    def list_rooms(self):
        return list(self._rooms)

    def room_state(self, room):
        return self._states.get(room)

    def house_map(self):
        return self._house


# --- import is lazy: no `mcp` package needed to import the module ------------------

def test_import_wavr_mcp_without_mcp_sdk_installed():
    # Importing the module must NOT require the optional [mcp] extra.
    m = importlib.import_module("wavr.mcp")
    assert hasattr(m, "list_rooms") and hasattr(m, "build_mcp_server")


def test_building_server_without_mcp_sdk_raises_import_error():
    # The SDK import is deferred to build time. If it's genuinely absent (as in the
    # dev test venv), building raises ImportError; if a dev has it installed, skip.
    try:
        import mcp.server.fastmcp  # noqa: F401
    except Exception:
        with pytest.raises(ImportError):
            build_mcp_server(FakeProvider([], {}, {}))
    else:
        pytest.skip("mcp SDK is installed; lazy-import-absent path not exercised")


# --- plain tool logic against a mocked provider -----------------------------------

def test_list_rooms_shape():
    states = {
        "sala": {"room": "sala", "occupied": True, "confidence": 0.8},
        "quarto": {"room": "quarto", "occupied": False, "confidence": 0.1},
    }
    out = list_rooms(FakeProvider(["sala", "quarto"], states, HOUSE))
    assert out == [
        {"room": "sala", "occupied": True, "confidence": 0.8},
        {"room": "quarto", "occupied": False, "confidence": 0.1},
    ]


def test_list_rooms_skips_rooms_with_no_state():
    out = list_rooms(FakeProvider(["ghost"], {}, HOUSE))
    assert out == []


def test_get_room_context_returns_full_why():
    rs = {"room": "sala", "occupied": True, "confidence": 0.72,
          "sources": [{"modality": "wifi_csi", "presence": True, "confidence": 0.6}],
          "explanation": "wifi_csi: presente -> 72% ocupado"}
    ctx = get_room_context(FakeProvider(["sala"], {"sala": rs}, HOUSE), "sala")
    assert ctx["sources"][0]["modality"] == "wifi_csi"
    assert "ocupado" in ctx["explanation"]


def test_get_room_context_unknown_room_is_none():
    assert get_room_context(FakeProvider([], {}, HOUSE), "nope") is None


def test_get_room_context_strips_vitals_and_targets():
    # Privacy (audit CRITICAL-1): the MCP read tool must never expose per-person
    # breathing/heart rate or x/y tracking, even though the underlying RoomState
    # carries both. Only room-level occupancy/confidence + the explainable why.
    rs = {"room": "sala", "occupied": True, "confidence": 0.72,
          "vitals": {"breathing_bpm": 14, "heart_bpm": 68},
          "targets": [{"x": 1.2, "y": 3.4, "id": "person-1"}],
          "sources": [{"modality": "mmwave", "presence": True, "confidence": 0.9}],
          "explanation": "mmwave: presente -> 72% ocupado"}
    ctx = get_room_context(FakeProvider(["sala"], {"sala": rs}, HOUSE), "sala")
    assert "vitals" not in ctx
    assert "targets" not in ctx
    assert ctx["room"] == "sala"
    assert ctx["occupied"] is True
    assert ctx["confidence"] == 0.72
    assert ctx["sources"][0]["modality"] == "mmwave"
    assert "ocupado" in ctx["explanation"]


def test_get_room_context_strips_vitals_and_targets_from_real_fusion():
    # Same invariant against a REAL FusionEngine/RoomState (not just a fake dict).
    fusion = FusionEngine()
    fusion.update(SensingEvent(room="sala", modality="mmwave", presence=True, motion=1.0,
                               breathing_bpm=14.0, heart_bpm=68.0, confidence=0.9,
                               ts="2026-07-02T10:00:00+00:00"))
    provider = FusionStateProvider(fusion, HOUSE)
    # Sanity: the underlying state actually carries vitals (proves this is a real test).
    assert provider.room_state("sala")["vitals"]
    ctx = get_room_context(provider, "sala")
    assert "vitals" not in ctx
    assert "targets" not in ctx


def test_get_house_map_passthrough():
    assert get_house_map(FakeProvider([], {}, HOUSE)) == HOUSE


# --- FusionStateProvider adapter against a REAL FusionEngine ----------------------

def test_provider_lists_rooms_and_context_from_real_fusion():
    fusion = FusionEngine()
    fusion.update(_ev("sala", "camera", True, 0.95))
    fusion.update(_ev("quarto", "network", False, 0.5))
    provider = FusionStateProvider(fusion, HOUSE)

    rooms = list_rooms(provider)
    names = {r["room"] for r in rooms}
    assert names == {"sala", "quarto"}
    sala = next(r for r in rooms if r["room"] == "sala")
    assert sala["occupied"] is True and 0.0 < sala["confidence"] <= 1.0

    ctx = get_room_context(provider, "sala")
    assert ctx["room"] == "sala"
    assert ctx["sources"][0]["modality"] == "camera"
    assert ctx.get("explanation")           # the explainable "why" is present

    assert get_house_map(provider) == HOUSE


def test_provider_unknown_room_returns_none():
    provider = FusionStateProvider(FusionEngine(), HOUSE)
    assert provider.room_state("void") is None
    assert get_room_context(provider, "void") is None
