"""Wavr Assistant engine picker + bounded MCP tool loop (Phase 2B).

Covers the AssistantEngineStore round-trip, the local/cloud classification +
tool-scope reuse of the Phase-2A auth scopes, the bounded run_ask loop
(success/refusal/timeout/max-steps), the 4 API routes, and the adversarial
guarantees this feature's thesis depends on: a cloud engine can never reach a
sensitive tool even if it asks, a disabled cloud connector fails the ask
closed, call_ha_service is unreachable regardless of scope, and no secret
value is ever persisted or logged.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from wavr import assistant_engine as engine_mod
from wavr.app import create_app
from wavr.assistant_store import AssistantEngineStore
from wavr.auth import AGENT_DEFAULT_TOOL_SCOPE, AGENT_READ_TOOL_SCOPE
from wavr.connector_store import ConnectorStore
from wavr.fusion import FusionEngine
from wavr.storage import Storage

CSRF = {"X-Wavr-Local": "1"}


def _client(tmp_path, monkeypatch, *, assistant_store=None, connector_store=None):
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "w.db"))
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(tmp_path / "house.json"))
    astore = assistant_store or AssistantEngineStore(":memory:")
    cstore = connector_store or ConnectorStore(":memory:")
    app = create_app(sources=[], storage=Storage(":memory:"),
                     assistant_store=astore, connector_store=cstore)
    return TestClient(app, headers=CSRF), astore, cstore


def _fake_generate(monkeypatch, scripted_replies):
    """Monkeypatch the engine resolution to a canned, offline generate()
    closure that returns `scripted_replies` in order (repeating the last one
    once exhausted). Records every prompt it was called with."""
    calls = {"prompts": []}

    def fake_make_generate_for_engine(cfg, engine_id, manual_cfg=None):
        def generate(prompt: str) -> str:
            calls["prompts"].append(prompt)
            idx = min(len(calls["prompts"]) - 1, len(scripted_replies) - 1)
            return scripted_replies[idx]
        return generate

    monkeypatch.setattr(engine_mod, "make_generate_for_engine", fake_make_generate_for_engine)
    return calls


# --------------------------------------------------------------------------- #
# AssistantEngineStore: persistence round-trip.
# --------------------------------------------------------------------------- #

def test_store_selection_default_and_select():
    s = AssistantEngineStore(":memory:")
    assert s.selected("wavr_assistant") == "wavr_assistant"      # absent row => default
    s.select("openai")
    assert s.selected("wavr_assistant") == "openai"


def test_store_manual_config_round_trip():
    s = AssistantEngineStore(":memory:")
    assert s.get_manual_config() is None
    row = s.set_manual_config("http://127.0.0.1:8080/v1", "llama3", "MY_LOCAL_KEY")
    assert row["base_url"] == "http://127.0.0.1:8080/v1"
    assert s.get_manual_config()["key_env_var"] == "MY_LOCAL_KEY"
    # NEVER a key/value column anywhere in the row.
    assert "key" not in row and "api_key" not in row and "value" not in row


def test_store_log_ask_and_recent_log_order():
    s = AssistantEngineStore(":memory:")
    s.log_ask("wavr_assistant", "q1", ["list_rooms"], "a1")
    s.log_ask("openai", "q2", [], "a2")
    log = s.recent_log(10)
    assert [r["question"] for r in log] == ["q2", "q1"]   # most-recent-first
    assert log[0]["tool_names_called"] == []
    assert log[1]["tool_names_called"] == ["list_rooms"]


def test_store_persists_across_instances(tmp_path):
    p = str(tmp_path / "a.db")
    AssistantEngineStore(p).select("gemini")
    assert AssistantEngineStore(p).selected("wavr_assistant") == "gemini"


# --------------------------------------------------------------------------- #
# Local/cloud classification + tool-scope reuse of the EXISTING 2A scope model.
# --------------------------------------------------------------------------- #

def test_is_loopback_url():
    assert engine_mod.is_loopback_url("http://127.0.0.1:11434/v1") is True
    assert engine_mod.is_loopback_url("http://localhost:1234") is True
    assert engine_mod.is_loopback_url("https://api.groq.com/openai/v1") is False
    assert engine_mod.is_loopback_url("http://192.168.1.50:8000") is False  # LAN != loopback
    assert engine_mod.is_loopback_url(None) is False
    assert engine_mod.is_loopback_url("") is False


def test_is_loopback_url_case_insensitive_and_bracketed_ipv6():
    # urlparse lower-cases a plain hostname and strips IPv6 brackets on its own --
    # confirms is_loopback_url's own claimed host set (127.0.0.1 / ::1 / localhost)
    # actually matches real-world URL spellings, not just the two forms already
    # exercised above.
    assert engine_mod.is_loopback_url("http://LOCALHOST:1234") is True
    assert engine_mod.is_loopback_url("http://[::1]:1234/v1") is True


def _cfg(monkeypatch, **env):
    # verify FIX A: engine_is_cloud/tool_scope_for now REQUIRE `cfg` (they read the
    # engine's own actual configured endpoint, not a fixed-by-id assumption) --
    # this builds a real cfg via load_config() so the classification tests below
    # exercise the SAME config path production does, not a hand-rolled stand-in.
    from wavr.config import load_config
    monkeypatch.delenv("WAVR_OLLAMA_URL", raising=False)
    monkeypatch.delenv("WAVR_ASSISTANT_LOCAL_LLM_URL", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return load_config()


def test_engine_is_cloud_classification(monkeypatch):
    cfg = _cfg(monkeypatch)   # defaults: ollama_url/local_llm both loopback
    assert engine_mod.engine_is_cloud("wavr_assistant", cfg) is False
    assert engine_mod.engine_is_cloud("local_llm", cfg) is False
    assert engine_mod.engine_is_cloud("openai", cfg) is True
    assert engine_mod.engine_is_cloud("anthropic", cfg) is True
    assert engine_mod.engine_is_cloud("gemini", cfg) is True
    assert engine_mod.engine_is_cloud("manual", cfg, "http://127.0.0.1:1234") is False
    assert engine_mod.engine_is_cloud("manual", cfg, "https://api.example.com") is True
    assert engine_mod.engine_is_cloud("manual", cfg, None) is True   # unconfigured -> conservative
    with pytest.raises(ValueError):
        engine_mod.engine_is_cloud("nope", cfg)


def test_tool_scope_for_reuses_auth_scopes_verbatim(monkeypatch):
    cfg = _cfg(monkeypatch)
    assert engine_mod.tool_scope_for("openai", cfg) == AGENT_DEFAULT_TOOL_SCOPE
    assert engine_mod.tool_scope_for("anthropic", cfg) == AGENT_DEFAULT_TOOL_SCOPE
    assert engine_mod.tool_scope_for("gemini", cfg) == AGENT_DEFAULT_TOOL_SCOPE
    assert engine_mod.tool_scope_for("manual", cfg, "https://api.example.com") == AGENT_DEFAULT_TOOL_SCOPE
    assert engine_mod.tool_scope_for("wavr_assistant", cfg) == AGENT_READ_TOOL_SCOPE
    assert engine_mod.tool_scope_for("local_llm", cfg) == AGENT_READ_TOOL_SCOPE
    assert engine_mod.tool_scope_for("manual", cfg, "http://127.0.0.1:1234") == AGENT_READ_TOOL_SCOPE
    # Structural: call_ha_service is excluded from the BROAD scope too.
    assert "call_ha_service" not in AGENT_READ_TOOL_SCOPE


# --------------------------------------------------------------------------- #
# verify FIX A (HIGH, egress-gate bypass): before this fix, wavr_assistant/
# local_llm were classified LOCAL UNCONDITIONALLY by id, never checking their
# REAL configured endpoint -- pointing WAVR_OLLAMA_URL or
# WAVR_ASSISTANT_LOCAL_LLM_URL at a remote host still granted the broad tool
# scope with NO connector gate. These prove the fix: a remote endpoint now
# classifies exactly like a non-loopback "manual" engine.
# --------------------------------------------------------------------------- #

def test_engine_is_cloud_local_llm_remote_endpoint_is_cloud_not_local(monkeypatch):
    cfg = _cfg(monkeypatch, WAVR_ASSISTANT_LOCAL_LLM_URL="https://remote.example.com/v1")
    assert engine_mod.engine_is_cloud("local_llm", cfg) is True
    assert engine_mod.tool_scope_for("local_llm", cfg) == AGENT_DEFAULT_TOOL_SCOPE


def test_engine_is_cloud_wavr_assistant_remote_ollama_is_cloud_not_local(monkeypatch):
    cfg = _cfg(monkeypatch, WAVR_OLLAMA_URL="https://remote-ollama.example.com")
    assert engine_mod.engine_is_cloud("wavr_assistant", cfg) is True
    assert engine_mod.tool_scope_for("wavr_assistant", cfg) == AGENT_DEFAULT_TOOL_SCOPE


def test_ask_local_llm_pointed_remotely_is_gated_by_cloud_connector(tmp_path, monkeypatch):
    # End-to-end: a local_llm engine pointed at a REMOTE host must be treated
    # exactly like a cloud engine by the /ask route -- refused (fail-closed)
    # while the assistant-cloud connector stays default-OFF, never silently
    # allowed through as "local" just because of its id.
    monkeypatch.setenv("WAVR_ASSISTANT_LOCAL_LLM_URL", "https://remote.example.com/v1")
    calls = _fake_generate(monkeypatch, ["ANSWER: should never run"])
    c, a, _cn = _client(tmp_path, monkeypatch)
    a.select("local_llm")
    r = c.post("/api/assistant/ask", json={"question": "hi"})
    assert r.status_code == 503
    assert "Connectors" in r.json()["detail"]
    assert calls["prompts"] == []            # the engine was NEVER actually called


def test_ask_local_llm_remote_engine_gets_coarse_scope_once_connector_enabled(tmp_path, monkeypatch):
    # Same remote local_llm engine, but with the cloud connector explicitly
    # turned on: it now MAY answer, but only with the COARSE scope -- a
    # sensitive tool attempt must still be refused, exactly like openai/
    # anthropic/gemini.
    monkeypatch.setenv("WAVR_ASSISTANT_LOCAL_LLM_URL", "https://remote.example.com/v1")
    _fake_generate(monkeypatch, [
        'TOOL: get_network_inventory {}',
        'ANSWER: current occupancy only',
    ])
    c, a, _cn = _client(tmp_path, monkeypatch)
    a.select("local_llm")
    r = c.post("/api/connectors/assistant-cloud/enable", json={"enabled": True})
    assert r.status_code == 200
    r = c.post("/api/assistant/ask", json={"question": "any new devices?"})
    assert r.status_code == 200
    trace = r.json()["trace"]
    assert trace[0] == {"step": 1, "tool": "get_network_inventory", "ok": False}


# --------------------------------------------------------------------------- #
# Phase-2B re-threat FIX 1 (MEDIUM): get_house_map leaks the floor plan (room
# `id` encodes the room name in every real house.json, plus polygon geometry)
# -- it must be OUT of the default COARSE (cloud) scope, but still reachable
# by a BROAD (local-engine) scope. End-to-end through /api/assistant/ask,
# mirroring the get_network_inventory refusal test above.
# --------------------------------------------------------------------------- #

def test_ask_cloud_engine_is_refused_get_house_map(tmp_path, monkeypatch):
    monkeypatch.setenv("WAVR_ASSISTANT_LOCAL_LLM_URL", "https://remote.example.com/v1")
    _fake_generate(monkeypatch, [
        'TOOL: get_house_map {}',
        'ANSWER: current occupancy only',
    ])
    c, a, _cn = _client(tmp_path, monkeypatch)
    a.select("local_llm")
    r = c.post("/api/connectors/assistant-cloud/enable", json={"enabled": True})
    assert r.status_code == 200
    r = c.post("/api/assistant/ask", json={"question": "what does my house look like?"})
    assert r.status_code == 200
    trace = r.json()["trace"]
    assert trace[0] == {"step": 1, "tool": "get_house_map", "ok": False}


def test_ask_local_engine_can_still_call_get_house_map(tmp_path, monkeypatch):
    # The DEFAULT (wavr_assistant, a genuine loopback Ollama) resolves the
    # BROAD scope (AGENT_READ_TOOL_SCOPE), which still includes get_house_map
    # -- only the coarse/cloud default excludes it.
    _fake_generate(monkeypatch, [
        'TOOL: get_house_map {}',
        'ANSWER: here is the layout',
    ])
    c, _a, _cn = _client(tmp_path, monkeypatch)
    r = c.post("/api/assistant/ask", json={"question": "what does my house look like?"})
    assert r.status_code == 200
    trace = r.json()["trace"]
    assert trace[0] == {"step": 1, "tool": "get_house_map", "ok": True}


# --------------------------------------------------------------------------- #
# Phase-2B re-threat FIX 2 (LOW, honesty): wavr_assistant/local_llm's
# descriptor text must match their ACTUAL live classification (engine_is_cloud/
# _endpoint_for), not a hardcoded "No external egress" that FIX A already
# proved can be wrong (a remote WAVR_OLLAMA_URL/WAVR_ASSISTANT_LOCAL_LLM_URL is
# real egress).
# --------------------------------------------------------------------------- #

def test_engine_descriptions_are_honest_about_local_vs_cloud(tmp_path, monkeypatch):
    c, _a, _cn = _client(tmp_path, monkeypatch)
    by_id = {e["id"]: e for e in c.get("/api/assistant/engines").json()["engines"]}
    # Default (genuinely loopback) config -> both say "No external egress".
    assert "No external egress" in by_id["wavr_assistant"]["description"]
    assert "No external egress" in by_id["local_llm"]["description"]
    # get_house_map is never named as part of the cloud coarse summary --
    # FIX 1 removed it from AGENT_DEFAULT_TOOL_SCOPE, so claiming it here
    # would be the same class of false claim FIX D already closed once.
    for cid in ("openai", "anthropic", "gemini"):
        assert "floor-plan" not in by_id[cid]["description"]
        assert "house-status verdict" in by_id[cid]["description"]


def test_engine_descriptions_flip_to_egress_honest_text_when_remote(tmp_path, monkeypatch):
    monkeypatch.setenv("WAVR_OLLAMA_URL", "https://remote-ollama.example.com")
    monkeypatch.setenv("WAVR_ASSISTANT_LOCAL_LLM_URL", "https://remote.example.com/v1")
    c, _a, _cn = _client(tmp_path, monkeypatch)
    by_id = {e["id"]: e for e in c.get("/api/assistant/engines").json()["engines"]}
    for cid in ("wavr_assistant", "local_llm"):
        desc = by_id[cid]["description"]
        assert "No external egress" not in desc
        assert "external egress" in desc
        assert "floor-plan" not in desc


# --------------------------------------------------------------------------- #
# Phase-2B re-threat FIX 3 (UX HIGH #1 backend half): the assistant-cloud
# egress fact must be enumerable on the canonical trust receipt (GET
# /api/status.features, what the frontend's EGRESS_ITEMS reads), not only on
# GET /api/connectors.
# --------------------------------------------------------------------------- #

def test_status_features_discloses_assistant_cloud(tmp_path, monkeypatch):
    c, _a, _cn = _client(tmp_path, monkeypatch)
    feats = c.get("/api/status").json()["features"]
    assert "assistant_cloud" in feats
    assert feats["assistant_cloud"] is False        # default-off
    r = c.post("/api/connectors/assistant-cloud/enable", json={"enabled": True})
    assert r.status_code == 200
    assert c.get("/api/status").json()["features"]["assistant_cloud"] is True


# --------------------------------------------------------------------------- #
# build_tool_runtime: call_ha_service is structurally unreachable.
# --------------------------------------------------------------------------- #

def test_build_tool_runtime_never_registers_call_ha_service():
    tools = engine_mod.build_tool_runtime(FusionEngine(), {})
    assert "call_ha_service" not in tools
    assert set(tools) == {"list_rooms", "get_room_context", "get_house_map",
                          "get_house_status", "get_network_inventory", "get_alerts",
                          "query_occupancy_history", "get_ha_entities"}


# --------------------------------------------------------------------------- #
# run_ask: the bounded loop itself (no HTTP, direct unit tests).
# --------------------------------------------------------------------------- #

async def test_run_ask_direct_answer_no_tools():
    def generate(prompt):
        return "ANSWER: the house is quiet"
    answer, trace, called = await engine_mod.run_ask(
        "how is the house?", generate, AGENT_READ_TOOL_SCOPE, {},
        max_steps=4, tool_timeout=5)
    assert answer == "the house is quiet"
    assert trace == [{"step": 1, "final": True}]
    assert called == []


async def test_run_ask_calls_one_allowed_tool_then_answers():
    replies = iter(['TOOL: list_rooms {}', "ANSWER: sala is occupied"])

    def generate(prompt):
        return next(replies)

    async def _list_rooms(args):
        return [{"room": "sala", "occupied": True, "confidence": 0.9}]

    tools = {"list_rooms": _list_rooms}
    answer, trace, called = await engine_mod.run_ask(
        "who is home?", generate, AGENT_READ_TOOL_SCOPE, tools,
        max_steps=4, tool_timeout=5)
    assert answer == "sala is occupied"
    assert called == ["list_rooms"]
    assert trace[0] == {"step": 1, "tool": "list_rooms", "ok": True}
    assert trace[1] == {"step": 2, "final": True}


async def test_run_ask_refuses_out_of_scope_tool_and_continues():
    # Model tries a tool that isn't in its scope; the loop must REFUSE it
    # (never execute it) and keep going within budget.
    replies = iter([
        'TOOL: get_network_inventory {}',
        'ANSWER: I cannot see the network inventory',
    ])

    def generate(prompt):
        return next(replies)

    called_real_tool = {"n": 0}

    async def _network(args):
        called_real_tool["n"] += 1
        return {"devices": ["should never be reached"], "count": 1}

    tools = {"get_network_inventory": _network}
    answer, trace, called = await engine_mod.run_ask(
        "what's on my network?", generate, AGENT_DEFAULT_TOOL_SCOPE, tools,
        max_steps=4, tool_timeout=5)
    assert called_real_tool["n"] == 0            # the tool function NEVER ran
    assert trace[0] == {"step": 1, "tool": "get_network_inventory", "ok": False}
    assert "cannot see" in answer


async def test_run_ask_tool_timeout_is_a_refusal_not_a_crash():
    async def _slow(args):
        await asyncio.sleep(10)
        return {"ok": True}

    replies = iter(['TOOL: get_house_map {}', 'ANSWER: done'])

    def generate(prompt):
        return next(replies)

    tools = {"get_house_map": _slow}
    answer, trace, called = await engine_mod.run_ask(
        "map?", generate, AGENT_READ_TOOL_SCOPE, tools,
        max_steps=4, tool_timeout=0.05)
    assert trace[0]["ok"] is False
    assert answer == "done"


async def test_run_ask_max_steps_bounds_total_generate_calls():
    call_count = {"n": 0}

    def generate(prompt):
        call_count["n"] += 1
        return 'TOOL: list_rooms {}'   # NEVER answers -- would loop forever unbounded

    async def _list_rooms(args):
        return []

    tools = {"list_rooms": _list_rooms}
    answer, trace, called = await engine_mod.run_ask(
        "loop forever?", generate, AGENT_READ_TOOL_SCOPE, tools,
        max_steps=3, tool_timeout=5)
    # 3 tool-step generate() calls + exactly ONE forced final call = 4 total.
    assert call_count["n"] == 4
    assert trace[-1] == {"step": 4, "final": True, "forced": True}
    assert called == ["list_rooms", "list_rooms", "list_rooms"]
    assert isinstance(answer, str) and answer


# --------------------------------------------------------------------------- #
# Tolerant tool-line parser: closes the flagged risk that a real LLM wrapping
# a directive in extra prose (very common -- most providers narrate what
# they're about to do even when told "reply with EXACTLY one line") would
# silently miss the TOOL:/ANSWER: line, treat the prose itself as a
# premature final answer, and never call the tool at all -- no crash, just a
# wrong/ungrounded answer. `_find_tool_call`/`_find_answer` scan line-by-line
# instead of anchoring the whole raw completion, so these prove the directive
# is still found (and, for TOOL:, still actually EXECUTED and scope-checked)
# regardless of what the model wraps around it.
# --------------------------------------------------------------------------- #

async def test_run_ask_tolerates_leading_prose_before_tool_call():
    # A real LLM narrating its plan before the directive -- the pre-hardening
    # whole-string regex would have missed this entirely and returned the
    # prose itself as the (wrong, ungrounded) final answer without ever
    # calling list_rooms.
    replies = iter([
        "Let me check the rooms first.\nTOOL: list_rooms {}",
        "ANSWER: sala is occupied",
    ])

    def generate(prompt):
        return next(replies)

    async def _list_rooms(args):
        return [{"room": "sala", "occupied": True}]

    tools = {"list_rooms": _list_rooms}
    answer, trace, called = await engine_mod.run_ask(
        "who is home?", generate, AGENT_READ_TOOL_SCOPE, tools,
        max_steps=4, tool_timeout=5)
    assert called == ["list_rooms"]                      # the tool WAS actually called
    assert trace[0] == {"step": 1, "tool": "list_rooms", "ok": True}
    assert answer == "sala is occupied"


async def test_run_ask_tolerates_trailing_prose_after_tool_call():
    replies = iter([
        'TOOL: get_alerts {}\nI\'ll wait for the result before answering.',
        "ANSWER: no active alerts",
    ])

    def generate(prompt):
        return next(replies)

    async def _get_alerts(args):
        return {"alerts": []}

    tools = {"get_alerts": _get_alerts}
    answer, trace, called = await engine_mod.run_ask(
        "any alerts?", generate, AGENT_READ_TOOL_SCOPE, tools,
        max_steps=4, tool_timeout=5)
    assert called == ["get_alerts"]
    assert trace[0] == {"step": 1, "tool": "get_alerts", "ok": True}
    assert answer == "no active alerts"


async def test_run_ask_tolerates_leading_prose_before_answer_and_keeps_full_multiline_body():
    def generate(prompt):
        return ("Sure, here's what I found:\n"
                "ANSWER: The living room is occupied.\n"
                "The kitchen is empty.")

    answer, trace, called = await engine_mod.run_ask(
        "what's the status?", generate, AGENT_READ_TOOL_SCOPE, {},
        max_steps=4, tool_timeout=5)
    # Leading commentary before ANSWER: is dropped, but the multi-line answer
    # body after it is preserved in full -- a real final answer is often more
    # than one line.
    assert answer == "The living room is occupied.\nThe kitchen is empty."
    assert called == []


async def test_run_ask_prefers_tool_call_over_hedged_answer_in_same_reply():
    # A confused/hedging reply containing BOTH an ANSWER: line and a TOOL:
    # line in the same completion -- deliberate precedence: TOOL wins, so the
    # loop keeps gathering grounding instead of locking in a premature
    # "I don't know" that the model itself was still trying to resolve.
    replies = iter([
        "ANSWER: I don't have enough information yet.\nTOOL: get_alerts {}",
        "ANSWER: no active alerts",
    ])

    def generate(prompt):
        return next(replies)

    async def _get_alerts(args):
        return {"alerts": []}

    tools = {"get_alerts": _get_alerts}
    answer, trace, called = await engine_mod.run_ask(
        "any alerts?", generate, AGENT_READ_TOOL_SCOPE, tools,
        max_steps=4, tool_timeout=5)
    assert called == ["get_alerts"]           # the tool call was honored, not skipped
    assert trace[0]["tool"] == "get_alerts"
    assert answer == "no active alerts"


async def test_run_ask_engine_call_failure_raises_assistant_error_not_leaking_exception_text():
    def generate(prompt):
        raise RuntimeError("boom with sk-supersecret123 in it")

    with pytest.raises(engine_mod.AssistantError) as exc_info:
        await engine_mod.run_ask("q", generate, AGENT_READ_TOOL_SCOPE, {},
                                 max_steps=2, tool_timeout=5)
    assert "sk-supersecret123" not in str(exc_info.value)


# --------------------------------------------------------------------------- #
# GET /api/assistant/engines: registry shape + defaults.
# --------------------------------------------------------------------------- #

def test_engines_catalog_default_shape(tmp_path, monkeypatch):
    c, _a, _cn = _client(tmp_path, monkeypatch)
    body = c.get("/api/assistant/engines").json()
    assert body["selected"] == "wavr_assistant"
    by_id = {e["id"]: e for e in body["engines"]}
    assert set(by_id) == {"wavr_assistant", "local_llm", "openai", "anthropic",
                          "gemini", "manual"}
    # DEFAULT engine: local, zero egress, always available, selected.
    assert by_id["wavr_assistant"]["local"] is True
    assert by_id["wavr_assistant"]["egress"] is False
    assert by_id["wavr_assistant"]["available"] is True
    assert by_id["wavr_assistant"]["selected"] is True
    assert by_id["wavr_assistant"]["needs"] is None
    assert by_id["wavr_assistant"]["tool_scope"] == "broad"
    # local_llm: also local/zero-egress/always-available by construction.
    assert by_id["local_llm"]["local"] is True
    assert by_id["local_llm"]["egress"] is False
    assert by_id["local_llm"]["available"] is True
    # cloud engines: egress True, unavailable without a key -> needs "config".
    for cid in ("openai", "anthropic", "gemini"):
        assert by_id[cid]["egress"] is True
        assert by_id[cid]["available"] is False
        assert by_id[cid]["needs"] == "config"
        assert by_id[cid]["tool_scope"] == "coarse"
    # manual: unconfigured -> needs "config".
    assert by_id["manual"]["available"] is False
    assert by_id["manual"]["needs"] == "config"


def test_engines_catalog_cloud_key_present_but_connector_off_needs_connector(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    c, _a, _cn = _client(tmp_path, monkeypatch)
    body = c.get("/api/assistant/engines").json()
    by_id = {e["id"]: e for e in body["engines"]}
    assert by_id["openai"]["available"] is True
    assert by_id["openai"]["needs"] == "connector"    # key present, connector gate off


def test_engines_catalog_cloud_available_once_connector_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    c, _a, cn = _client(tmp_path, monkeypatch)
    r = c.post("/api/connectors/assistant-cloud/enable", json={"enabled": True})
    assert r.status_code == 200
    body = c.get("/api/assistant/engines").json()
    by_id = {e["id"]: e for e in body["engines"]}
    assert by_id["openai"]["needs"] is None


def test_engines_catalog_selected_flag_flips_after_switch(tmp_path, monkeypatch):
    # The picker's core purpose: switching engines must be reflected honestly on
    # the NEXT read -- the newly-selected id is the only one flagged True, and the
    # previously-selected default is no longer flagged.
    c, _a, _cn = _client(tmp_path, monkeypatch)
    r = c.post("/api/assistant/engine", json={"engine_id": "local_llm"})
    assert r.status_code == 200
    body = c.get("/api/assistant/engines").json()
    assert body["selected"] == "local_llm"
    by_id = {e["id"]: e for e in body["engines"]}
    assert by_id["local_llm"]["selected"] is True
    assert by_id["wavr_assistant"]["selected"] is False


def test_selected_engine_falls_back_to_default_for_unresolvable_persisted_id(tmp_path, monkeypatch):
    # assistant_store.AssistantEngineStore.selected() deliberately never validates
    # the persisted id against the live catalog (its own docstring: "a config
    # change never raises here, only degrades to an honest 'needs setup' state
    # upstream"). Prove selected_engine()'s documented "NEVER raises" fallback to
    # cfg.assistant_engine_default actually holds for a row referencing an id
    # outside the fixed 6-id set -- e.g. a downgrade after a future build added a
    # 7th engine and an admin selected it, or a hand-edited/corrupted DB row.
    from wavr.config import load_config
    monkeypatch.delenv("WAVR_ASSISTANT_ENGINE", raising=False)
    s = AssistantEngineStore(":memory:")
    s.select("some-legacy-engine-id-not-in-ENGINE_IDS")   # bypasses route validation
    cn = ConnectorStore(":memory:")
    cfg = load_config()
    d = engine_mod.selected_engine(cfg, s, cn)
    assert d["id"] == cfg.assistant_engine_default == "wavr_assistant"


def test_ask_with_corrupted_selected_engine_id_falls_back_honestly(tmp_path, monkeypatch):
    # Same scenario as above, but through the real HTTP /ask route end-to-end --
    # a corrupted/unresolvable persisted selection must never 500; it silently
    # resolves to the safe local default and answers normally.
    calls = _fake_generate(monkeypatch, ["ANSWER: fallback works"])
    c, a, _cn = _client(tmp_path, monkeypatch)
    a.select("some-legacy-engine-id-not-in-ENGINE_IDS")
    r = c.post("/api/assistant/ask", json={"question": "hi"})
    assert r.status_code == 200
    assert r.json()["answer"] == "fallback works"
    assert len(calls["prompts"]) == 1


def test_ask_manual_selected_but_unconfigured_returns_503(tmp_path, monkeypatch):
    # The route's manual-config validation (POST /engine) makes this unreachable
    # through the API alone, but the SAME "needs" check the ask route relies on
    # must also hold for a manual selection with no persisted config (e.g. a
    # store pre-seeded outside this app, or a future caller of store.select()
    # that skips the route) -- exercises the same code path openai's
    # unconfigured-503 test does, for the other "needs=config" engine kind.
    c, a, _cn = _client(tmp_path, monkeypatch)
    a.select("manual")   # no manual config ever persisted
    r = c.post("/api/assistant/ask", json={"question": "hi"})
    assert r.status_code == 503
    assert "not configured" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# POST /api/assistant/engine: select + the manual engine's config.
# --------------------------------------------------------------------------- #

def test_select_engine_unknown_id_404(tmp_path, monkeypatch):
    c, _a, _cn = _client(tmp_path, monkeypatch)
    assert c.post("/api/assistant/engine", json={"engine_id": "nope"}).status_code == 404


def test_select_local_llm_persists(tmp_path, monkeypatch):
    c, a, _cn = _client(tmp_path, monkeypatch)
    r = c.post("/api/assistant/engine", json={"engine_id": "local_llm"})
    assert r.status_code == 200
    assert r.json()["engine"]["id"] == "local_llm"
    assert a.selected("wavr_assistant") == "local_llm"


def test_select_manual_requires_base_url_and_model(tmp_path, monkeypatch):
    c, _a, _cn = _client(tmp_path, monkeypatch)
    r = c.post("/api/assistant/engine", json={"engine_id": "manual"})
    assert r.status_code == 422
    r = c.post("/api/assistant/engine",
               json={"engine_id": "manual", "base_url": "not-a-url", "model": "x"})
    assert r.status_code == 422


def test_select_manual_rejects_bad_key_env_var(tmp_path, monkeypatch):
    c, _a, _cn = _client(tmp_path, monkeypatch)
    r = c.post("/api/assistant/engine",
               json={"engine_id": "manual", "base_url": "http://127.0.0.1:1234",
                     "model": "llama3", "key_env_var": "lowercase-not-allowed"})
    assert r.status_code == 422


def test_select_manual_persists_config_no_secret_field_accepted(tmp_path, monkeypatch):
    c, a, _cn = _client(tmp_path, monkeypatch)
    body = {"engine_id": "manual", "base_url": "http://127.0.0.1:9999/v1",
           "model": "llama3", "key_env_var": "MY_KEY_NAME",
           "api_key": "sk-should-be-ignored-if-somehow-sent"}
    r = c.post("/api/assistant/engine", json=body)
    assert r.status_code == 200
    stored = a.get_manual_config()
    assert stored["base_url"] == "http://127.0.0.1:9999/v1"
    assert stored["key_env_var"] == "MY_KEY_NAME"
    # The extra "api_key" field the caller sent is simply not a body parameter
    # this route reads -- prove nothing resembling a secret value landed in the store.
    assert "sk-should-be-ignored-if-somehow-sent" not in str(stored)


def test_select_route_requires_csrf_header(tmp_path, monkeypatch):
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "w.db"))
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(tmp_path / "house.json"))
    app = create_app(sources=[], storage=Storage(":memory:"),
                     assistant_store=AssistantEngineStore(":memory:"),
                     connector_store=ConnectorStore(":memory:"))
    with TestClient(app) as c:   # no X-Wavr-Local header
        assert c.post("/api/assistant/engine",
                      json={"engine_id": "local_llm"}).status_code == 403


# --------------------------------------------------------------------------- #
# POST /api/assistant/ask: the wired end-to-end happy path + honest refusals.
# --------------------------------------------------------------------------- #

def test_ask_empty_question_422(tmp_path, monkeypatch):
    c, _a, _cn = _client(tmp_path, monkeypatch)
    assert c.post("/api/assistant/ask", json={"question": "   "}).status_code == 422


def test_ask_too_long_question_422(tmp_path, monkeypatch):
    c, _a, _cn = _client(tmp_path, monkeypatch)
    r = c.post("/api/assistant/ask", json={"question": "x" * 2001})
    assert r.status_code == 422


def test_ask_default_engine_happy_path_and_audit_log(tmp_path, monkeypatch):
    calls = _fake_generate(monkeypatch, ["ANSWER: nobody is home right now"])
    c, a, _cn = _client(tmp_path, monkeypatch)
    r = c.post("/api/assistant/ask", json={"question": "is anyone home?"})
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "nobody is home right now"
    assert body["trace"] == [{"step": 1, "final": True}]
    # B5 audit log recorded engine/question/tool_names/answer.
    log = a.recent_log(10)
    assert len(log) == 1
    assert log[0]["engine_id"] == "wavr_assistant"
    assert log[0]["question"] == "is anyone home?"
    assert log[0]["answer"] == "nobody is home right now"
    assert log[0]["tool_names_called"] == []
    got = c.get("/api/assistant/log").json()["log"]
    assert got[0]["question"] == "is anyone home?"


def test_ask_local_engine_can_reach_broad_scope_tool(tmp_path, monkeypatch):
    _fake_generate(monkeypatch, [
        'TOOL: get_network_inventory {}',
        'ANSWER: nothing unusual on the network',
    ])
    c, _a, _cn = _client(tmp_path, monkeypatch)
    r = c.post("/api/assistant/ask", json={"question": "any new devices?"})
    assert r.status_code == 200
    trace = r.json()["trace"]
    assert trace[0]["tool"] == "get_network_inventory"
    assert trace[0]["ok"] is True    # LOCAL default engine -- broad scope, allowed


def test_ask_default_max_steps_bounds_via_http_route_without_env_override(tmp_path, monkeypatch):
    # test_run_ask_max_steps_bounds_total_generate_calls proves the LOOP itself is
    # bounded when max_steps is passed explicitly; this proves the WIRING holds
    # too -- cfg.assistant_max_tool_steps's actual config DEFAULT (4, no
    # WAVR_ASSISTANT_MAX_STEPS override) really reaches run_ask through the real
    # route, not just through a unit call.
    monkeypatch.delenv("WAVR_ASSISTANT_MAX_STEPS", raising=False)
    call_count = {"n": 0}

    def fake_make_generate_for_engine(cfg, engine_id, manual_cfg=None):
        def generate(prompt: str) -> str:
            call_count["n"] += 1
            return "TOOL: list_rooms {}"   # never answers -- would loop forever unbounded
        return generate

    monkeypatch.setattr(engine_mod, "make_generate_for_engine", fake_make_generate_for_engine)
    c, _a, _cn = _client(tmp_path, monkeypatch)
    r = c.post("/api/assistant/ask", json={"question": "loop forever?"})
    assert r.status_code == 200
    # 4 (the config default) tool-step calls + exactly ONE forced final call = 5.
    assert call_count["n"] == 5
    assert r.json()["trace"][-1] == {"step": 5, "final": True, "forced": True}


def test_adversarial_audit_log_records_refused_tool_attempts_not_just_allowed(tmp_path, monkeypatch):
    # B5's stated intent (assistant_store.log_ask docstring): "every attempt,
    # allowed or refused" -- an operator must be able to confirm a refusal
    # actually held from the PERSISTED audit trail, not only from the one
    # response body that happened to be returned live. Prove tool_names_called in
    # the STORED row includes a refused attempt, not just successful tool calls.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.setenv("WAVR_ASSISTANT_MAX_STEPS", "3")
    _fake_generate(monkeypatch, [
        "TOOL: get_network_inventory {}",   # sensitive -- refused for a cloud engine
        "TOOL: list_rooms {}",              # coarse -- allowed
        "ANSWER: current occupancy only",
    ])
    c, a, _cn = _client(tmp_path, monkeypatch)
    a.select("openai")
    r = c.post("/api/connectors/assistant-cloud/enable", json={"enabled": True})
    assert r.status_code == 200
    r = c.post("/api/assistant/ask", json={"question": "tell me everything"})
    assert r.status_code == 200
    log = a.recent_log(10)
    assert len(log) == 1
    assert log[0]["tool_names_called"] == ["get_network_inventory", "list_rooms"]


def test_ask_unconfigured_engine_selected_returns_503(tmp_path, monkeypatch):
    c, a, _cn = _client(tmp_path, monkeypatch)
    a.select("openai")   # no OPENAI_API_KEY set anywhere
    r = c.post("/api/assistant/ask", json={"question": "hi"})
    assert r.status_code == 503
    assert "not configured" in r.json()["detail"]


def test_ask_route_requires_csrf_header(tmp_path, monkeypatch):
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "w.db"))
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(tmp_path / "house.json"))
    app = create_app(sources=[], storage=Storage(":memory:"),
                     assistant_store=AssistantEngineStore(":memory:"),
                     connector_store=ConnectorStore(":memory:"))
    with TestClient(app) as c:   # no X-Wavr-Local header
        assert c.post("/api/assistant/ask", json={"question": "hi"}).status_code == 403


# --------------------------------------------------------------------------- #
# M1 (appsec re-audit, 2026-07, HIGH): the assistant's EGRESS-CONFIG plane (POST
# /api/assistant/engine) must be loopback-root-only. A paired peer is minted
# role=central with the FULL central DEFAULT_SCOPES (incl. "admin"), so it
# legitimately clears this router's router-level central+admin gate -- proving
# that gate alone was not enough: without the route-level require_root
# tightening, a malicious/compromised peer could select the "manual" engine,
# point its base_url at an attacker host, and name a REAL secret env var
# (key_env_var) for /ask -- still peer-reachable, unchanged -- to resolve and
# leak, alongside coarse house state.
# --------------------------------------------------------------------------- #
def test_multidevice_central_peer_forbidden_from_engine_config_but_ask_unchanged(
        tmp_path, monkeypatch):
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "md.db"))
    monkeypatch.setenv("WAVR_HOUSE_MAP", str(tmp_path / "house.json"))
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    _fake_generate(monkeypatch, ["ANSWER: nobody is home"])
    astore = AssistantEngineStore(":memory:")
    cstore = ConnectorStore(":memory:")
    app = create_app(sources=[], storage=Storage(":memory:"),
                     assistant_store=astore, connector_store=cstore)
    central_loopback = TestClient(app)
    code = central_loopback.post("/api/pair-code", json={"role": "central"},
                                 headers=CSRF).json()["code"]
    peer = TestClient(app, client=("192.168.1.50", 12345))
    token = peer.post("/api/pair",
                      json={"code": code, "device_name": "peer"}).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}
    # Router-level gate alone would have let this through -- the READ still does.
    assert peer.get("/api/assistant/engines", headers=auth).status_code == 200
    # The exfil attempt: point "manual" at an attacker host + name a real secret
    # env var. Must be refused, loopback-root-only.
    r = peer.post(
        "/api/assistant/engine",
        json={"engine_id": "manual", "base_url": "https://attacker.example.com",
             "model": "x", "key_env_var": "OPENAI_API_KEY"},
        headers=auth)
    assert r.status_code == 403
    assert astore.get_manual_config() is None                       # nothing persisted
    assert astore.selected("wavr_assistant") == "wavr_assistant"    # selection unchanged
    # /ask is UNCHANGED (read-shaped, bounded by the engine's own tool scope) --
    # a central peer may still ask a bounded question against whatever engine IS
    # configured (still the safe default here, since the write above was refused).
    r = peer.post("/api/assistant/ask", json={"question": "is anyone home?"}, headers=auth)
    assert r.status_code == 200
    # The loopback operator (true root) still can reconfigure the engine.
    r = central_loopback.post("/api/assistant/engine", json={"engine_id": "local_llm"},
                              headers=CSRF)
    assert r.status_code == 200
    assert astore.selected("wavr_assistant") == "local_llm"


# --------------------------------------------------------------------------- #
# ADVERSARIAL: the thesis this feature exists to prove.
# --------------------------------------------------------------------------- #

def test_adversarial_cloud_engine_cannot_call_sensitive_tool_even_if_asked(tmp_path, monkeypatch):
    # A cloud engine, fully configured AND connector-enabled, whose underlying
    # model tries every sensitive tool anyway -- every one must be REFUSED
    # (never executed), and only the coarse tools may succeed. 4 refusals + 1
    # successful coarse call before the answer needs a budget > the 4-step default.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.setenv("WAVR_ASSISTANT_MAX_STEPS", "6")
    _fake_generate(monkeypatch, [
        'TOOL: get_network_inventory {}',
        'TOOL: get_alerts {}',
        'TOOL: query_occupancy_history {}',
        'TOOL: get_ha_entities {}',
        'TOOL: list_rooms {}',
        'ANSWER: current occupancy only',
    ])
    c, a, cn = _client(tmp_path, monkeypatch)
    a.select("openai")
    r = c.post("/api/connectors/assistant-cloud/enable", json={"enabled": True})
    assert r.status_code == 200
    r = c.post("/api/assistant/ask", json={"question": "tell me everything"})
    assert r.status_code == 200
    trace = r.json()["trace"]
    sensitive = {"get_network_inventory", "get_alerts", "query_occupancy_history",
                "get_ha_entities"}
    for entry in trace:
        if entry.get("tool") in sensitive:
            assert entry["ok"] is False, f"{entry['tool']} must be refused for a cloud engine"
    coarse_entries = [e for e in trace if e.get("tool") == "list_rooms"]
    assert coarse_entries and coarse_entries[0]["ok"] is True
    assert r.json()["answer"] == "current occupancy only"


def test_adversarial_call_ha_service_never_reachable_regardless_of_scope(tmp_path, monkeypatch):
    _fake_generate(monkeypatch, [
        'TOOL: call_ha_service {"domain": "light", "service": "turn_on", "entity_id": "light.x"}',
        'ANSWER: refused as expected',
    ])
    c, a, _cn = _client(tmp_path, monkeypatch)
    a.select("wavr_assistant")   # the BROADEST local scope this feature ever grants
    r = c.post("/api/assistant/ask", json={"question": "turn on the light"})
    assert r.status_code == 200
    trace = r.json()["trace"]
    assert trace[0] == {"step": 1, "tool": "call_ha_service", "ok": False}


def test_adversarial_disabled_cloud_connector_refuses_ask_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    calls = _fake_generate(monkeypatch, ["ANSWER: should never run"])
    c, a, _cn = _client(tmp_path, monkeypatch)
    a.select("openai")                       # configured...
    # ...but the assistant-cloud connector is left at its DEFAULT-OFF state.
    r = c.post("/api/assistant/ask", json={"question": "hi"})
    assert r.status_code == 503
    assert "Connectors" in r.json()["detail"]
    assert calls["prompts"] == []            # the engine was NEVER actually called


def test_adversarial_no_secret_value_persisted_in_manual_config(tmp_path, monkeypatch):
    c, a, _cn = _client(tmp_path, monkeypatch)
    secret_looking_value = "sk-realsecretvalue-should-never-be-stored"
    monkeypatch.setenv("MY_MANUAL_KEY", secret_looking_value)
    r = c.post("/api/assistant/engine",
               json={"engine_id": "manual", "base_url": "http://127.0.0.1:4000/v1",
                     "model": "llama3", "key_env_var": "MY_MANUAL_KEY"})
    assert r.status_code == 200
    stored = a.get_manual_config()
    assert stored["key_env_var"] == "MY_MANUAL_KEY"
    assert secret_looking_value not in str(stored)
    assert secret_looking_value not in str(r.json())


def test_adversarial_no_secret_value_in_ask_log_or_response(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-realsecretvalue-should-never-leak")
    _fake_generate(monkeypatch, ["ANSWER: fine"])
    c, a, _cn = _client(tmp_path, monkeypatch)
    a.select("openai")
    c.post("/api/connectors/assistant-cloud/enable", json={"enabled": True})
    r = c.post("/api/assistant/ask", json={"question": "how's the house?"})
    assert r.status_code == 200
    assert "sk-realsecretvalue-should-never-leak" not in str(r.json())
    log = a.recent_log(10)
    assert "sk-realsecretvalue-should-never-leak" not in str(log)


def test_adversarial_manual_loopback_gets_broad_scope_without_connector_gate(tmp_path, monkeypatch):
    # A loopback "manual" engine is LOCAL -- it must NOT need the cloud connector
    # gate, and it MAY reach the broad tool scope (same treatment as wavr_assistant).
    _fake_generate(monkeypatch, ['TOOL: get_alerts {}', 'ANSWER: no active alerts'])
    c, a, _cn = _client(tmp_path, monkeypatch)
    r = c.post("/api/assistant/engine",
               json={"engine_id": "manual", "base_url": "http://127.0.0.1:5555/v1",
                     "model": "llama3"})
    assert r.status_code == 200
    assert r.json()["engine"]["local"] is True
    assert r.json()["engine"]["needs"] is None    # no connector gate needed
    r = c.post("/api/assistant/ask", json={"question": "any alerts?"})
    assert r.status_code == 200
    assert r.json()["trace"][0] == {"step": 1, "tool": "get_alerts", "ok": True}


def test_adversarial_manual_non_loopback_is_cloud_and_gated(tmp_path, monkeypatch):
    c, a, cn = _client(tmp_path, monkeypatch)
    r = c.post("/api/assistant/engine",
               json={"engine_id": "manual", "base_url": "https://api.example.com/v1",
                     "model": "llama3"})
    assert r.status_code == 200
    assert r.json()["engine"]["local"] is False
    assert r.json()["engine"]["needs"] == "connector"   # cloud gate not yet enabled
    r = c.post("/api/assistant/ask", json={"question": "hi"})
    assert r.status_code == 503


# --------------------------------------------------------------------------- #
# GET /api/assistant/log: bounded limit clamp.
# --------------------------------------------------------------------------- #

def test_log_limit_is_clamped(tmp_path, monkeypatch):
    c, a, _cn = _client(tmp_path, monkeypatch)
    for i in range(5):
        a.log_ask("wavr_assistant", f"q{i}", [], f"a{i}")
    got = c.get("/api/assistant/log?limit=2").json()["log"]
    assert len(got) == 2
    got_neg = c.get("/api/assistant/log?limit=-5").json()["log"]
    assert len(got_neg) == 1   # clamped to at least 1, never "no limit"


def test_log_limit_upper_bound_is_clamped_to_500(tmp_path, monkeypatch):
    # The route's own upper clamp (max(1, min(limit, 500))) -- proved directly
    # against the actual `limit` value store.recent_log() is called with, rather
    # than requiring 500+ real rows to observe the same effect indirectly.
    c, a, _cn = _client(tmp_path, monkeypatch)
    seen = {}
    orig_recent_log = a.recent_log

    def _spy(limit=50):
        seen["limit"] = limit
        return orig_recent_log(limit)

    monkeypatch.setattr(a, "recent_log", _spy)
    r = c.get("/api/assistant/log?limit=999999")
    assert r.status_code == 200
    assert seen["limit"] == 500
