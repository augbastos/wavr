"""Persistent store for the Wavr Assistant engine picker (Phase 2B).

Three small responsibilities, three tables -- kept separate because they have
different lifecycles (a selection changes often via one click; the manual
engine's config is set rarely via a form; the audit log grows one row per ask):

  * `assistant_selection` -- singleton row (id=1) naming the currently active
    engine id. Absent row => cfg.assistant_engine_default decides ("absent row
    = env decides", the same idiom as ConnectorStore.effective_active).
  * `assistant_manual_config` -- singleton row (id=1) for the ONE "manual"
    (add-your-own OpenAI-compatible endpoint) engine slot. There is exactly one
    manual engine in this picker (unlike the multi-custom-engine design sketch),
    matching the fixed 6-id registry in assistant_engine.ENGINE_IDS.
  * `assistant_log` -- append-only audit trail (B5): one row per POST
    /api/assistant/ask, feeding GET /api/assistant/log.

SECRETS: `assistant_manual_config.key_env_var` holds the NAME of an environment
variable (e.g. "GROQ_API_KEY"), never the key's value. The API layer (see
wavr.api_assistant) never accepts a raw key/token/secret field, so a secret
cannot reach this table even by caller mistake -- the admin sets the real value
in `.env` (git-ignored) themselves. `assistant_log` stores the QUESTION and the
final ANSWER text plus tool NAMES only -- never a raw tool payload (occupancy
history rows, inventory devices, alert details, HA entity lists) and never a
secret value, so a leaked/exported log can't hand over a household's crown
jewels or a credential.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS assistant_selection (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    engine_id   TEXT NOT NULL,
    updated_ts  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assistant_manual_config (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    base_url     TEXT NOT NULL,
    model        TEXT NOT NULL,
    key_env_var  TEXT,
    updated_ts   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assistant_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    engine_id          TEXT NOT NULL,
    question           TEXT NOT NULL,
    tool_names_called  TEXT NOT NULL,
    answer             TEXT NOT NULL,
    ts                 TEXT NOT NULL
);
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AssistantEngineStore:
    """SQLite-backed store, shares wavr.db (git-ignored) like connector_store.py /
    pin_store.py: injectable path (":memory:" for tests), lock-guarded for
    thread-pool use."""

    def __init__(self, path: str = "wavr.db", now_fn=_utcnow_iso):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._now = now_fn
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- selection ------------------------------------------------------------

    def selected(self, default: str) -> str:
        """The persisted engine id, or `default` (cfg.assistant_engine_default)
        if no row exists yet. Never validates the id against the live catalog --
        the CALLER (assistant_engine.engine_catalog/selected_engine, mirroring
        _connector_catalog's `available` computation) decides whether a
        previously-selected engine still resolves, so a config change never
        raises here, only degrades to an honest 'needs setup' state upstream."""
        with self._lock:
            row = self._conn.execute(
                "SELECT engine_id FROM assistant_selection WHERE id = 1"
            ).fetchone()
        return row["engine_id"] if row else default

    def select(self, engine_id: str) -> None:
        """Upsert the singleton row. Called ONLY from the gated POST route
        (require_local + require_scope('control'), router-level admin) -- this
        method itself does no authorization, same separation as
        ConnectorStore.set_enabled()."""
        ts = self._now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO assistant_selection (id, engine_id, updated_ts)"
                " VALUES (1, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET"
                "   engine_id = excluded.engine_id,"
                "   updated_ts = excluded.updated_ts",
                (engine_id, ts),
            )
            self._conn.commit()

    # -- the one "manual" engine slot ------------------------------------------

    def set_manual_config(self, base_url: str, model: str,
                          key_env_var: str | None) -> dict:
        """Persist the manual engine's NON-SECRET config. `key_env_var` is a
        NAME only (validated by the route layer before this is ever called) --
        this method stores whatever string it is given verbatim, so the
        structural guarantee lives in the caller never passing a raw key."""
        ts = self._now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO assistant_manual_config"
                " (id, base_url, model, key_env_var, updated_ts)"
                " VALUES (1, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET"
                "   base_url = excluded.base_url,"
                "   model = excluded.model,"
                "   key_env_var = excluded.key_env_var,"
                "   updated_ts = excluded.updated_ts",
                (base_url, model, key_env_var, ts),
            )
            self._conn.commit()
        return self.get_manual_config()

    def get_manual_config(self) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT base_url, model, key_env_var, updated_ts"
                " FROM assistant_manual_config WHERE id = 1"
            ).fetchone()
        return dict(row) if row else None

    # -- audit log (B5) ---------------------------------------------------------

    def log_ask(self, engine_id: str, question: str,
               tool_names_called: list[str], answer: str) -> None:
        """One row per ask: {engine, question, tool_names_called, answer, ts}.
        `tool_names_called` is a plain list of tool NAME strings (every attempt,
        allowed or refused) -- NEVER a raw tool payload/result. Never raises on
        a hostile/huge question or answer string (sqlite TEXT has no practical
        length limit here; the route layer already bounds `question`'s length
        before this is called)."""
        ts = self._now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO assistant_log"
                " (engine_id, question, tool_names_called, answer, ts)"
                " VALUES (?, ?, ?, ?, ?)",
                (engine_id, question, json.dumps(list(tool_names_called)), answer, ts),
            )
            self._conn.commit()

    def recent_log(self, limit: int = 50) -> list[dict]:
        """Most-recent-first. `limit` is NOT re-clamped here -- the route layer
        (api_assistant.get_log) owns the defensive clamp, same split as
        GET /api/history's route-level `max(1, min(limit, 1000))`."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, engine_id, question, tool_names_called, answer, ts"
                " FROM assistant_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["tool_names_called"] = json.loads(d["tool_names_called"])
            except (TypeError, ValueError):
                d["tool_names_called"] = []
            out.append(d)
        return out

    def close(self) -> None:
        self._conn.close()
