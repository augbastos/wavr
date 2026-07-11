"""FastAPI router for the Wavr Assistant engine picker + bounded ask (Phase 2B).

Mirrors api_connectors.py's injectable-factory shape: `store`/`connectors` are
passed in so this stays testable without touching a real DB, and `tool_deps` is
a zero-arg callable returning the CURRENT tool-dependency bundle so the router
always sees live, freshly-wired dependencies (mirrors _connector_catalog's
live-read-from-cfg discipline) rather than a snapshot captured at router-build
time.

Routes (all gated in app.py -- router-level central+admin scope, same tier as
the identity/peers-admin routers; the state-changing engine-select and ask
additionally carry require_local CSRF + control scope, mirroring /api/narrate):

  * GET  /api/assistant/engines          -> the 6-engine catalog + which is selected
  * POST /api/assistant/engine {engine_id, base_url?, model?, key_env_var?}
        -> select the active engine (manual additionally persists its config)
  * POST /api/assistant/ask {question}   -> {answer, trace} -- runs the bounded
        MCP tool loop against the SELECTED engine. Fulfils C1's voice_ask text MVP.
  * GET  /api/assistant/log              -> the audit trail (B5)
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from fastapi import APIRouter, Body, HTTPException

from wavr import assistant_engine as engine_mod

_log = logging.getLogger("wavr.api_assistant")

_QUESTION_MAX_LEN = 2000
# A plain POSIX-ish environment-variable name: uppercase, digits, underscores,
# starting with a letter. Deliberately does NOT require a "WAVR_" prefix (the
# codebase's own built-in keys -- OPENAI_API_KEY/ANTHROPIC_API_KEY/GEMINI_API_KEY
# -- don't carry one either), so a user pointing this at e.g. their own
# GROQ_API_KEY isn't forced into a nonstandard name.
_KEY_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _validate_manual_body(base_url: str | None, model: str | None,
                          key_env_var: str | None) -> None:
    if not base_url or not model:
        raise HTTPException(status_code=422,
                            detail="manual engine requires 'base_url' and 'model'")
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=422,
                            detail="base_url must be an absolute http(s) URL")
    if key_env_var and not _KEY_ENV_RE.match(key_env_var):
        raise HTTPException(
            status_code=422,
            detail="key_env_var must look like an ENV_VAR name (uppercase letters/digits/underscore)")


def build_assistant_router(cfg, store, connectors, *, tool_deps, write_deps=None) -> APIRouter:
    """`store` -- AssistantEngineStore. `connectors` -- ConnectorStore (only
    `is_enabled("assistant-cloud")` is read here -- the single egress surface's
    cloud-assistant kill switch, seeded/upserted by app.py at wiring time).
    `tool_deps` -- zero-arg callable -> kwargs for `assistant_engine.
    build_tool_runtime` (fusion/house_map/ha_client/network_inventory_fn/
    alerts_fn/occupancy_provider/house_status_fn)."""
    router = APIRouter()
    wdeps = list(write_deps or [])

    @router.get("/api/assistant/engines")
    async def list_engines():
        return {"engines": engine_mod.engine_catalog(cfg, store, connectors),
                "selected": store.selected(cfg.assistant_engine_default)}

    @router.post("/api/assistant/engine", dependencies=wdeps)
    async def select_engine(engine_id: str = Body(..., embed=True),
                            base_url: str | None = Body(None, embed=True),
                            model: str | None = Body(None, embed=True),
                            key_env_var: str | None = Body(None, embed=True)):
        if engine_id not in engine_mod.ENGINE_IDS:
            raise HTTPException(status_code=404, detail=f"unknown engine: {engine_id}")
        if engine_id == "manual":
            # Structural guarantee (design spec §7): this body has no key/api_key/
            # token/secret field at all -- a raw secret cannot reach the store even
            # by caller mistake. Only a NAME is ever accepted (key_env_var).
            _validate_manual_body(base_url, model, key_env_var)
            store.set_manual_config(base_url.strip(), model.strip(),
                                    key_env_var.strip() if key_env_var else None)
        store.select(engine_id)
        updated = next(d for d in engine_mod.engine_catalog(cfg, store, connectors)
                       if d["id"] == engine_id)
        return {"engine": updated}

    @router.post("/api/assistant/ask", dependencies=wdeps)
    async def ask(question: str = Body(..., embed=True)):
        q = (question or "").strip()
        if not q:
            raise HTTPException(status_code=422, detail="question must not be empty")
        if len(q) > _QUESTION_MAX_LEN:
            raise HTTPException(status_code=422,
                                detail=f"question too long (max {_QUESTION_MAX_LEN} chars)")

        engine = engine_mod.selected_engine(cfg, store, connectors)
        if engine["needs"] == "config":
            raise HTTPException(
                status_code=503,
                detail=f"assistant engine '{engine['id']}' is not configured yet")
        if engine["needs"] == "connector":
            raise HTTPException(
                status_code=503,
                detail=("cloud assistant engine disabled in Connectors — enable "
                        "'Wavr Assistant (cloud engine)' to use it"))
        # Defense in depth: re-check the gate directly (not just via `needs`) so a
        # future descriptor bug can never silently skip the fail-closed egress gate.
        if engine["egress"] and not connectors.is_enabled("assistant-cloud"):
            raise HTTPException(
                status_code=503,
                detail=("cloud assistant engine disabled in Connectors — enable "
                        "'Wavr Assistant (cloud engine)' to use it"))

        manual_cfg = store.get_manual_config() if engine["id"] == "manual" else None
        try:
            generate = engine_mod.make_generate_for_engine(cfg, engine["id"], manual_cfg)
        except ValueError as exc:
            raise HTTPException(status_code=503, detail=str(exc))

        deps = tool_deps()
        tools = engine_mod.build_tool_runtime(**deps)
        manual_base_url = manual_cfg["base_url"] if manual_cfg else None
        # `cfg` REQUIRED (verify FIX A): tool_scope_for's own local/cloud
        # classification for wavr_assistant/local_llm now reads their ACTUAL
        # configured endpoint (cfg.ollama_url / cfg.assistant_local_llm_base_url),
        # not a fixed-by-id assumption -- see assistant_engine.engine_is_cloud.
        tool_scope = engine_mod.tool_scope_for(engine["id"], cfg, manual_base_url)

        try:
            answer, trace, tool_names_called = await engine_mod.run_ask(
                q, generate, tool_scope, tools,
                max_steps=cfg.assistant_max_tool_steps,
                tool_timeout=cfg.assistant_tool_timeout)
        except engine_mod.AssistantError:
            _log.exception("assistant ask failed")
            raise HTTPException(status_code=502, detail="assistant engine error")

        # B5 audit: engine/question/tool NAMES/answer only -- never a raw tool
        # payload (see assistant_store.AssistantEngineStore.log_ask's docstring).
        store.log_ask(engine["id"], q, tool_names_called, answer)
        return {"answer": answer, "trace": trace}

    @router.get("/api/assistant/log")
    async def get_log(limit: int = 50):
        limit = max(1, min(int(limit), 500))
        return {"log": store.recent_log(limit)}

    return router
