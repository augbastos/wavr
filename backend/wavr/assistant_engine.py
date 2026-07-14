"""Wavr Assistant: engine resolution + the BOUNDED MCP tool-use loop (Phase 2B).

Reuses two existing seams rather than inventing new ones (design spec
2026-07-10, DESIGN-assistant-engine-picker.md):

  * `wavr.narrator` -- every engine's underlying LLM call is a plain
    `generate(prompt) -> str` closure, exactly the provider-agnostic seam
    narrator.py already built for gemini/ollama/openai/anthropic.
  * `wavr.mcp` -- the loop calls ONLY the Phase-2A plain, injectable READ
    functions (list_rooms/get_room_context/get_house_map/get_house_status +
    the explicit-grant get_network_inventory/get_alerts/
    query_occupancy_history/get_ha_entities), in-process, the SAME functions
    the MCP server wraps -- no [mcp] extra, no second transport, no network
    hop for a same-process tool call. `call_ha_service` (the one WRITE tool)
    is STRUCTURALLY ABSENT from the dispatch table below -- see
    `_build_tools` -- so it can never be reached from this loop, regardless of
    WAVR_MCP_CONTROL or any tool-scope grant.

Six fixed engine ids (ENGINE_IDS). ALL SIX run a question through the SAME
bounded loop (`run_ask`) -- only two things differ per engine:

  1. which `generate` closure answers (make_generate_for_engine), and
  2. which MCP-tool-name scope it is allowed to reach (tool_scope_for) --
     reusing wavr.auth's EXISTING scope model (AGENT_DEFAULT_TOOL_SCOPE /
     AGENT_READ_TOOL_SCOPE) rather than inventing a parallel one:

       * wavr_assistant, local_llm, and "manual" reach the full
         AGENT_READ_TOOL_SCOPE (every read tool except call_ha_service) ONLY
         when their ACTUAL configured endpoint (cfg.ollama_url /
         cfg.assistant_local_llm_base_url / the manual base_url) is a genuine
         loopback host (`is_loopback_url`) -- a live config check, not a
         fixed-by-id assumption (verify FIX A): WAVR_OLLAMA_URL or
         WAVR_ASSISTANT_LOCAL_LLM_URL pointed at a remote host makes that
         engine CLOUD too, same as a non-loopback "manual" endpoint.
       * openai/anthropic/gemini (unconditionally) and any of the above three
         pointed at a non-loopback endpoint -- CLOUD egress -- are bound to
         AGENT_DEFAULT_TOOL_SCOPE only: current room/house occupancy plus the
         house-status verdict, via list_rooms/get_room_context/
         get_house_status. Re-threat (MEDIUM): get_house_map is EXCLUDED from
         this default too, not just network inventory/occupancy history/
         alerts/HA entity names -- its room `id` encodes the room name (every
         real house.json) and it ships polygon geometry, i.e. the floor plan
         itself; a cloud Q&A assistant doesn't need it. See auth.
         AGENT_DEFAULT_TOOL_SCOPE's own docstring for the full rationale.

Stopping conditions for the loop (non-negotiable, per agent-loop design
discipline -- see `run_ask`):
  * `max_steps` (cfg.assistant_max_tool_steps, default 4) -- a hard cap on
    tool-call iterations. After the cap, ONE forced final generate() call asks
    for an answer from whatever was already gathered -- never a silent hang,
    never an unbounded loop.
  * `tool_timeout` (cfg.assistant_tool_timeout, default 10s) per tool call.
  * NO cross-query memory -- each `run_ask` call is a fresh loop; nothing here
    persists a conversation transcript (the audit log persists only the
    question/answer/tool-names, at the route layer, not a full transcript).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Awaitable, Callable
from urllib.parse import urlparse

from wavr import mcp, narrator
from wavr.auth import AGENT_DEFAULT_TOOL_SCOPE, AGENT_READ_TOOL_SCOPE
from wavr.house_status import DEFAULT_NETWORK_WINDOW_MINUTES

_log = logging.getLogger("wavr.assistant")

# The picker's fixed registry -- see the module docstring for what each id means.
ENGINE_IDS: tuple[str, ...] = (
    "wavr_assistant", "local_llm", "openai", "anthropic", "gemini", "manual",
)
# Unconditionally CLOUD regardless of config (verify FIX A): their base_url is
# never consulted for classification. wavr_assistant/local_llm/manual are
# data-dependent instead -- see `_endpoint_for`.
_CLOUD_FIXED_IDS = frozenset({"openai", "anthropic", "gemini"})

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# Response bytes kept in the tool-call "Observation" fed back to the model. A
# generous-but-bounded cap so one huge tool result (e.g. a large network
# inventory) can't blow up the prompt or the audit trail indefinitely.
_OBSERVATION_MAX_CHARS = 4000


class AssistantError(RuntimeError):
    """The underlying LLM call itself failed (network/API error). The route
    layer maps this to a 502, mirroring /api/narrate's `except Exception: 502`
    pattern. The message NEVER includes `str(original_exception)` -- only the
    exception's type name -- so a urllib/provider error string can never echo
    a header/URL fragment into a log or an HTTP response body."""


# --------------------------------------------------------------------------- #
# Classification: local vs cloud, and the tool-name scope that follows from it.
# --------------------------------------------------------------------------- #

def is_loopback_url(base_url: str | None) -> bool:
    """True only if `base_url`'s host is a LITERAL loopback name/address
    (localhost / 127.0.0.1 / ::1). Deliberately narrower than
    `wavr.netaddr.is_lan_ip` (which also allows the rest of the private LAN
    ranges) -- narrator.py's own docstring draws this exact line for the
    OpenAI-compatible provider ("LOCAL when pointed at a loopback server"), so
    the manual engine's local/cloud classification matches that precedent
    byte-for-byte rather than inventing a broader one. Never raises."""
    if not base_url:
        return False
    try:
        host = (urlparse(base_url).hostname or "").strip().lower()
    except ValueError:
        return False
    return host in _LOOPBACK_HOSTS


def _endpoint_for(engine_id: str, cfg, manual_base_url: str | None = None) -> str | None:
    """The one URL that determines `engine_id`'s local/cloud classification --
    verify FIX A's single source of truth, shared by `engine_is_cloud` and
    `_descriptor` so they can never disagree. `None` for the three
    unconditionally-cloud ids (their own base_url override is never consulted
    for classification -- see `_CLOUD_FIXED_IDS`)."""
    if engine_id == "wavr_assistant":
        return cfg.ollama_url
    if engine_id == "local_llm":
        return cfg.assistant_local_llm_base_url
    if engine_id == "manual":
        return manual_base_url
    return None


def engine_is_cloud(engine_id: str, cfg, manual_base_url: str | None = None) -> bool:
    """True iff using `engine_id` is EXTERNAL EGRESS. `cfg` is REQUIRED (verify
    FIX A): wavr_assistant/local_llm are classified from their OWN actual
    configured endpoint via `is_loopback_url`, not unconditionally local by id
    (the pre-fix bypass: a remote WAVR_OLLAMA_URL/WAVR_ASSISTANT_LOCAL_LLM_URL
    still read as LOCAL -> broad scope + no connector gate). `manual_base_url`
    is only consulted for engine_id == "manual". Raises ValueError for an id
    outside ENGINE_IDS (the route 404s first)."""
    if engine_id in _CLOUD_FIXED_IDS:
        return True
    if engine_id in ("wavr_assistant", "local_llm", "manual"):
        return not is_loopback_url(_endpoint_for(engine_id, cfg, manual_base_url))
    raise ValueError(f"unknown engine id: {engine_id!r}")


def tool_scope_for(engine_id: str, cfg, manual_base_url: str | None = None) -> frozenset[str]:
    """The MCP tool-name allow-list this engine's loop may reach. Reuses
    wavr.auth's EXISTING Phase-2A scope model verbatim (no parallel scope
    system): CLOUD -> AGENT_DEFAULT_TOOL_SCOPE (coarse, current-state only);
    LOCAL -> AGENT_READ_TOOL_SCOPE (every read tool, still EXCLUDING
    call_ha_service structurally, per AGENT_READ_TOOL_SCOPE's own definition).
    `cfg` is REQUIRED -- see `engine_is_cloud`'s docstring (verify FIX A):
    the scope this returns is only as trustworthy as that live endpoint check."""
    if engine_is_cloud(engine_id, cfg, manual_base_url):
        return AGENT_DEFAULT_TOOL_SCOPE
    return AGENT_READ_TOOL_SCOPE


# --------------------------------------------------------------------------- #
# Descriptor catalog (for the picker UI; no I/O, no egress).
# --------------------------------------------------------------------------- #

_LABELS = {
    "wavr_assistant": "Wavr Assistant (built-in)",
    "local_llm": "Local LLM",
    "openai": "OpenAI",
    "anthropic": "Anthropic (Claude)",
    "gemini": "Google Gemini",
    "manual": "+ Add manually",
}

# verify FIX D (LOW) + Phase-2B re-threat FIX 2 (LOW, honesty): the coarse-scope
# summary text is ONE constant so every card that mentions it (openai/anthropic/
# gemini, plus wavr_assistant/local_llm when their live endpoint turns out to be
# non-loopback -- see `_description_for`) says the identical, currently-accurate
# thing. "your floor-plan geometry" was DROPPED here (previously listed
# alongside occupancy/house-status) once get_house_map left AGENT_DEFAULT_TOOL_
# SCOPE (auth.py) -- a cloud engine literally cannot reach it any more, so
# naming it here would be a false claim again, the same class of bug FIX D
# itself closed.
_CLOUD_SCOPE_SUMMARY = "current room/house occupancy and the house-status verdict"

# verify FIX 2 (LOW, re-threat honesty): wavr_assistant/local_llm used to
# hardcode "No external egress" -- but FIX A (engine_is_cloud) means either one
# IS external egress whenever its ACTUAL configured endpoint
# (cfg.ollama_url / cfg.assistant_local_llm_base_url) is not loopback. Both
# entries are now a `{is_cloud: text}` pair instead of a flat string, resolved
# in `_descriptor` against the SAME `cloud` boolean `engine_is_cloud` already
# computed there -- so the description can never disagree with the actual
# tool-scope/egress classification the rest of the descriptor reports.
# openai/anthropic/gemini are unconditionally cloud (see `_CLOUD_FIXED_IDS`)
# so they stay plain strings; "manual" already worded its own text
# conditionally (a single sentence covering both cases) and is left as-is.
_DESCRIPTIONS = {
    "wavr_assistant": {
        False: ("Runs entirely on this box via the local Ollama model, with tool access "
               "to your home's current room/house state. No external egress."),
        True: ("WAVR_OLLAMA_URL points at a non-loopback host, so this is external "
              f"egress: sends a coarse summary -- {_CLOUD_SCOPE_SUMMARY} -- to that "
              "endpoint to answer."),
    },
    "local_llm": {
        False: ("A local OpenAI-compatible server (e.g. Ollama's /v1 endpoint). "
               "No external egress."),
        True: ("WAVR_ASSISTANT_LOCAL_LLM_URL points at a non-loopback host, so this is "
              f"external egress: sends a coarse summary -- {_CLOUD_SCOPE_SUMMARY} -- to "
              "that endpoint to answer."),
    },
    "openai": f"Sends a coarse summary -- {_CLOUD_SCOPE_SUMMARY} -- to OpenAI to answer.",
    "anthropic": (f"Sends a coarse summary -- {_CLOUD_SCOPE_SUMMARY} -- to Anthropic "
                 "Claude to answer."),
    "gemini": f"Sends a coarse summary -- {_CLOUD_SCOPE_SUMMARY} -- to Google Gemini to answer.",
    "manual": ("Your own OpenAI-compatible endpoint. Local (loopback) stays zero-egress; "
              "anything else is treated as cloud egress (coarse tool scope only)."),
}


def _description_for(engine_id: str, cloud: bool) -> str:
    """Resolve `_DESCRIPTIONS[engine_id]` against the descriptor's own `cloud`
    classification. wavr_assistant/local_llm store a `{bool: text}` pair (FIX 2
    above); every other id is a plain string, unaffected by `cloud`."""
    entry = _DESCRIPTIONS[engine_id]
    return entry[cloud] if isinstance(entry, dict) else entry


def _descriptor(engine_id: str, cfg, manual_cfg: dict | None, selected: bool,
                cloud_gate_on: bool) -> dict:
    manual_base_url = manual_cfg["base_url"] if manual_cfg else None
    cloud = engine_is_cloud(engine_id, cfg, manual_base_url)
    scope = "coarse" if cloud else "broad"

    if engine_id == "wavr_assistant":
        available, model, base_url, key_env_var = True, cfg.ollama_model, cfg.ollama_url, None
    elif engine_id == "local_llm":
        available = True
        model, base_url, key_env_var = (cfg.assistant_local_llm_model,
                                        cfg.assistant_local_llm_base_url, None)
    elif engine_id == "openai":
        available = bool(cfg.openai_api_key)
        model, base_url, key_env_var = cfg.openai_model, cfg.openai_base_url, "OPENAI_API_KEY"
    elif engine_id == "anthropic":
        available = bool(cfg.anthropic_api_key)
        model, base_url, key_env_var = cfg.anthropic_model, None, "ANTHROPIC_API_KEY"
    elif engine_id == "gemini":
        available = bool(cfg.gemini_api_key)
        model, base_url, key_env_var = cfg.gemini_model, None, "GEMINI_API_KEY"
    elif engine_id == "manual":
        available = bool(manual_cfg and manual_cfg.get("base_url") and manual_cfg.get("model"))
        model = manual_cfg["model"] if manual_cfg else None
        base_url = manual_cfg["base_url"] if manual_cfg else None
        key_env_var = manual_cfg.get("key_env_var") if manual_cfg else None
    else:
        raise ValueError(f"unknown engine id: {engine_id!r}")

    # Honest `needs`: "config" wins (nothing to call yet) over "connector" (a
    # configured-but-gated cloud engine) -- mirrors _connector_catalog's
    # config-vs-restart precedence for `narr_needs`.
    if not available:
        needs = "config"
    elif cloud and not cloud_gate_on:
        needs = "connector"
    else:
        needs = None

    return {
        "id": engine_id,
        "label": _LABELS[engine_id],
        "local": not cloud,
        "egress": cloud,
        "available": available,
        "selected": selected,
        "needs": needs,
        "model": model,
        "base_url": base_url,
        "key_env_var": key_env_var,
        "tool_scope": scope,
        "description": _description_for(engine_id, cloud),
    }


def engine_catalog(cfg, store, connectors) -> list[dict]:
    """Live descriptor list for all 6 fixed engines. Never touches the network.
    `store` -- AssistantEngineStore. `connectors` -- ConnectorStore (read-only
    here; `is_enabled("assistant-cloud")` -- the assistant's own cloud-egress
    kill switch -- see wavr.api_assistant / app.py wiring) ANDed with the
    system-toggles egress master (`egress_allowed()`, see /api/system/toggles):
    an operator-level egress block also disables cloud engines even when the
    assistant-cloud connector itself stays enabled."""
    selected_id = store.selected(cfg.assistant_engine_default)
    manual_cfg = store.get_manual_config()
    cloud_gate_on = connectors.is_enabled("assistant-cloud") and connectors.egress_allowed()
    return [_descriptor(eid, cfg, manual_cfg, eid == selected_id, cloud_gate_on)
            for eid in ENGINE_IDS]


def selected_engine(cfg, store, connectors) -> dict:
    """The one descriptor whose id == store.selected(...), or the default
    engine's descriptor if the persisted id somehow doesn't resolve (should
    not happen -- `select()` only ever persists a validated id -- but this
    NEVER raises; the caller checks `needs` before using it)."""
    catalog = engine_catalog(cfg, store, connectors)
    sel_id = store.selected(cfg.assistant_engine_default)
    for d in catalog:
        if d["id"] == sel_id:
            return d
    return next(d for d in catalog if d["id"] == cfg.assistant_engine_default)


# --------------------------------------------------------------------------- #
# Execution: resolve the underlying generate() closure for the selected engine.
# --------------------------------------------------------------------------- #

def make_generate_for_engine(cfg, engine_id: str,
                             manual_cfg: dict | None = None) -> Callable[[str], str]:
    """Builds the single-shot `generate` closure for `engine_id`, reusing
    narrator.py's existing provider factories verbatim (no second credential/
    provider system). Raises ValueError for an unresolvable engine (manual with
    no stored config, or an id outside ENGINE_IDS) -- both are caller bugs (the
    route already checked `engine["needs"]`), not user-facing states."""
    if engine_id == "wavr_assistant":
        # Deliberately ALWAYS Ollama (cfg.ollama_*), NOT whatever cfg.narrate_provider
        # happens to be selected for the dashboard's single-shot narrator -- an
        # existing install may have narrate_provider set to a cloud provider. Wavr
        # Assistant stays local when ollama_url is loopback; if it is pointed at a
        # remote host, engine_is_cloud() reclassifies it as egress (coarse-scoped + gated).
        return narrator.make_ollama_generate(cfg.ollama_model, cfg.ollama_url)
    if engine_id == "local_llm":
        return narrator.make_openai_generate(
            cfg.assistant_local_llm_base_url, "", cfg.assistant_local_llm_model)
    if engine_id == "openai":
        return narrator.make_openai_generate(
            cfg.openai_base_url, cfg.openai_api_key, cfg.openai_model)
    if engine_id == "anthropic":
        return narrator.make_anthropic_generate(cfg.anthropic_api_key, cfg.anthropic_model)
    if engine_id == "gemini":
        return narrator.make_gemini_generate(cfg.gemini_api_key, cfg.gemini_model)
    if engine_id == "manual":
        if not manual_cfg or not manual_cfg.get("base_url") or not manual_cfg.get("model"):
            raise ValueError("manual engine is not configured")
        key_env_var = manual_cfg.get("key_env_var")
        # Resolved at CALL time, by NAME -- the stored row never held the value.
        key = os.environ.get(key_env_var, "") if key_env_var else ""
        return narrator.make_openai_generate(manual_cfg["base_url"], key, manual_cfg["model"])
    raise ValueError(f"unknown engine id: {engine_id!r}")


# --------------------------------------------------------------------------- #
# The tool runtime: wraps the Phase-2A plain mcp.py functions for the loop.
# --------------------------------------------------------------------------- #

def build_tool_runtime(fusion, house_map: dict | None = None, *,
                       ha_client=None, network_inventory_fn=None, alerts_fn=None,
                       occupancy_provider=None,
                       house_status_fn=None) -> dict[str, Callable[[dict], Awaitable]]:
    """Builds the {tool_name: async_callable(args_dict) -> JSON-serializable}
    dispatch table the loop calls by name. Mirrors `wavr.mcp.
    make_server_from_app_state`'s convenience wiring (same params, same
    graceful-None-degrades-honestly discipline for every optional source) but
    targets an IN-PROCESS caller instead of the MCP SDK/HTTP transport -- built
    unconditionally (no cfg.multidevice gate, no [mcp] extra needed): only
    `build_mcp_server`/`build_mcp_http_mount` need the optional SDK; the plain
    functions this wraps do not.

    `call_ha_service` IS STRUCTURALLY ABSENT from the returned table -- there is
    no code path here that could register it, by construction (mirrors mcp.py's
    own "PERMANENT EXCLUSION" precedent for device-blocking). The assistant loop
    can NEVER actuate anything, regardless of WAVR_MCP_CONTROL or any tool-scope
    grant; a control-capable assistant action is explicitly out of scope for
    this feature (design spec §4)."""
    provider = mcp.FusionStateProvider(fusion, house_map)

    async def _list_rooms(_args: dict):
        return await asyncio.to_thread(mcp.list_rooms, provider)

    async def _get_room_context(args: dict):
        room = args.get("room") if isinstance(args, dict) else None
        if not room:
            return {"error": "missing required 'room' argument"}
        return await asyncio.to_thread(mcp.get_room_context, provider, room)

    async def _get_house_map(_args: dict):
        return await asyncio.to_thread(mcp.get_house_map, provider)

    async def _get_house_status(args: dict):
        window = args.get("window_minutes") if isinstance(args, dict) else None
        try:
            window = float(window) if window is not None else DEFAULT_NETWORK_WINDOW_MINUTES
        except (TypeError, ValueError):
            window = DEFAULT_NETWORK_WINDOW_MINUTES
        return await mcp.get_house_status(house_status_fn, window)

    async def _get_network_inventory(_args: dict):
        return await asyncio.to_thread(mcp.get_network_inventory, network_inventory_fn)

    async def _get_alerts(_args: dict):
        return await asyncio.to_thread(mcp.get_alerts, alerts_fn)

    async def _query_occupancy_history(args: dict):
        room = args.get("room") if isinstance(args, dict) else None
        hours = args.get("hours", 24) if isinstance(args, dict) else 24
        try:
            hours = int(hours)
        except (TypeError, ValueError):
            hours = 24
        return await asyncio.to_thread(
            mcp.query_occupancy_history, provider, occupancy_provider, room, hours)

    async def _get_ha_entities(_args: dict):
        return await asyncio.to_thread(mcp.get_ha_entities, ha_client)

    # NOTE: do not add "call_ha_service" here. See the docstring above.
    return {
        "list_rooms": _list_rooms,
        "get_room_context": _get_room_context,
        "get_house_map": _get_house_map,
        "get_house_status": _get_house_status,
        "get_network_inventory": _get_network_inventory,
        "get_alerts": _get_alerts,
        "query_occupancy_history": _query_occupancy_history,
        "get_ha_entities": _get_ha_entities,
    }


# --------------------------------------------------------------------------- #
# The bounded ReAct-style loop. Provider-agnostic: it drives ANY narrator
# `generate(prompt) -> str` closure via plain-text tool-call prompting (no
# native function-calling wire format assumed -- narrator.py's seam is
# text-in/text-out for all 4 providers, so the protocol below is implemented
# entirely in the prompt, not in a provider-specific JSON schema).
# --------------------------------------------------------------------------- #

# Matched PER LINE (see _find_tool_call/_find_answer below), not against the
# whole raw completion -- a real LLM very commonly wraps a directive in a
# sentence of prose ("Let me check that.\nTOOL: list_rooms {}", or a trailing
# "I'll wait for the result." after it) even when told not to. The original
# whole-string-anchored version of this regex silently missed both cases: no
# crash, no error, just the prose itself treated as a premature, ungrounded
# final answer (a real tool call never made). Scanning line-by-line finds the
# directive wherever the model put it without having to parse free-form prose,
# and without weakening any scope/allowlist check downstream -- a matched name
# still must pass `name in tool_scope and name in tools` before anything runs.
_TOOL_LINE_RE = re.compile(r"^\s*TOOL:\s*([A-Za-z_][A-Za-z0-9_]*)\s*(\{.*\})?\s*$",
                           re.IGNORECASE)
_ANSWER_LINE_RE = re.compile(r"^\s*ANSWER:\s*(.*)$", re.IGNORECASE)


def _find_tool_call(raw: str) -> tuple[str, dict] | None:
    """The FIRST line in `raw` that is a well-formed `TOOL: name {json}`
    directive, ignoring any other prose lines around it. `None` if no line
    matches. Malformed/absent JSON degrades to `{}` (same permissive default
    the old parser used) rather than dropping the call entirely -- honoring a
    slightly-malformed but clearly-intended tool call is strictly safer than
    falling through to raw-text-as-answer, the failure mode this exists to
    close. KNOWN TRADE-OFF: the tool-call JSON must still be on the SAME line
    as `TOOL:` (matches the prompt's own "EXACTLY one line" instruction) --
    pretty-printed multi-line JSON args are not tolerated. Prose-wrapping is
    the far more common real-LLM failure mode than multi-line JSON, so that is
    the one this hardening targets."""
    for line in raw.splitlines():
        m = _TOOL_LINE_RE.match(line)
        if m:
            name = m.group(1)
            args_raw = m.group(2)
            try:
                args = json.loads(args_raw) if args_raw else {}
                if not isinstance(args, dict):
                    args = {}
            except (json.JSONDecodeError, TypeError):
                args = {}
            return name, args
    return None


def _find_answer(raw: str) -> str | None:
    """The first `ANSWER:` line in `raw`, PLUS every line after it (a genuine
    final answer is frequently multi-line prose) -- so leading commentary
    BEFORE the ANSWER: line is dropped, but nothing of the answer body itself
    is. `None` if no line matches (caller falls back to the full raw text,
    same degrade-honestly behavior as before this hardening)."""
    lines = raw.splitlines()
    for i, line in enumerate(lines):
        m = _ANSWER_LINE_RE.match(line)
        if m:
            return "\n".join([m.group(1)] + lines[i + 1:]).strip()
    return None


def _build_prompt(question: str, tool_scope, transcript: list[str]) -> str:
    tool_lines = "\n".join(f"- {name}" for name in sorted(tool_scope)) or "(none available)"
    lines = [
        "You are Wavr's home assistant. Answer ONLY using tool results below; "
        "never invent data about the house.",
        "Available tools (call at most one per reply):",
        tool_lines,
        "",
        'To call a tool, reply with EXACTLY one line: TOOL: <tool_name> {"arg": "value"}',
        "When you have enough information, reply with: ANSWER: <your final answer>",
        "If the answer cannot be found in the tool results, say so plainly -- never guess.",
        "",
        f"User question: {question}",
    ]
    if transcript:
        lines.append("")
        lines.append("Steps so far:")
        lines.extend(transcript)
    return "\n".join(lines)


async def _call_generate(generate: Callable[[str], str], prompt: str) -> str:
    try:
        raw = await asyncio.to_thread(generate, prompt)
    except Exception as exc:
        # Never str(exc): a urllib/provider error can embed request-URL/response
        # fragments; the type name alone is enough to diagnose without risking a
        # credential/PII leak into a log or an HTTP response.
        raise AssistantError(f"engine call failed: {type(exc).__name__}") from exc
    return (raw or "").strip()


async def run_ask(question: str, generate: Callable[[str], str], tool_scope,
                  tools: dict[str, Callable[[dict], Awaitable]], *,
                  max_steps: int, tool_timeout: float,
                  ) -> tuple[str, list[dict], list[str]]:
    """The bounded loop. Returns (answer, trace, tool_names_called).

    `trace` is the API-response-facing shape: `[{"step", "tool"?, "ok"?,
    "final"?}]` -- tool NAMES and step outcomes only, never args or results
    (same restraint as the audit log; the response is at least as exposed as
    the log, arguably more since a frontend may itself log/render it).
    `tool_names_called` is the audit-log-facing list of every tool name the
    model ATTEMPTED (allowed or refused) -- lets an operator later confirm a
    refusal actually held (e.g. a cloud engine that tried to call a
    sensitive tool and was denied)."""
    transcript: list[str] = []
    trace: list[dict] = []
    tool_names_called: list[str] = []

    for step in range(1, max_steps + 1):
        prompt = _build_prompt(question, tool_scope, transcript)
        raw = await _call_generate(generate, prompt)
        # A TOOL: directive found anywhere wins over an ANSWER: directive found
        # anywhere -- deliberate precedence: a reply that hedges with both
        # ("ANSWER: I don't have enough info... TOOL: get_room_context {...}")
        # almost always means the model wants to keep gathering grounding, and
        # erring toward one more tool call is strictly safer than erring toward
        # a premature, possibly-ungrounded refusal.
        tool_call = _find_tool_call(raw)

        if tool_call is not None:
            name, args = tool_call
            tool_names_called.append(name)

            if name not in tool_scope or name not in tools:
                obs = f"refused: '{name}' is not permitted for this engine"
                trace.append({"step": step, "tool": name, "ok": False})
            else:
                try:
                    result = await asyncio.wait_for(tools[name](args), timeout=tool_timeout)
                    obs = json.dumps(result, default=str)[:_OBSERVATION_MAX_CHARS]
                    trace.append({"step": step, "tool": name, "ok": True})
                except asyncio.TimeoutError:
                    obs = f"tool '{name}' timed out"
                    trace.append({"step": step, "tool": name, "ok": False})
                except Exception:
                    _log.exception("assistant tool call failed: %s", name)
                    obs = f"tool '{name}' failed"
                    trace.append({"step": step, "tool": name, "ok": False})

            transcript.append(f"TOOL: {name} {json.dumps(args)}")
            transcript.append(f"Observation: {obs}")
            continue

        answer = _find_answer(raw)
        if answer is None:
            answer = raw
        trace.append({"step": step, "final": True})
        return answer, trace, tool_names_called

    # Budget exhausted without an ANSWER -- ONE forced final call. Not counted as
    # an extra tool step (no tool executes), so total generate() calls is bounded
    # by max_steps + 1, deterministically -- never an unbounded loop.
    prompt = _build_prompt(question, tool_scope, transcript) + (
        "\n\nYou are out of tool calls. Answer now using only the information "
        "above; if it is not enough, say plainly that you don't have enough "
        "information -- never guess.")
    raw = await _call_generate(generate, prompt)
    answer = _find_answer(raw)
    if answer is None:
        answer = raw
    trace.append({"step": max_steps + 1, "final": True, "forced": True})
    return answer, trace, tool_names_called
