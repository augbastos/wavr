"""User-authored routines: the "when THIS happens, do THAT" spine.

A layperson builds routines ("when I arrive -> turn on the living-room light"); the
engine reacts to the SAME presence edges AwayMonitor / RulesEngine already emit (it
never re-derives presence), plus time/deadline/device triggers, and runs each
routine's action list through the existing sinks (Home Assistant via the gated
call_ha_service, the notifier, discreet mode).

Three pure, injectable pieces so the whole spine is testable with stubs and no real
HA/notifier/loop:
  * RoutineStore   -- sqlite persistence (camera_store.py convention), JSON-validated.
  * RoutinesEngine -- matching only: given an edge/tick + live state, return the
    routines that should fire. No I/O, no execution.
  * ActionExecutor -- runs an action list through injected sinks, failure-tolerant
    per action; structurally cannot re-arm sensing or a camera (no such sink exists).

INVARIANTS: routines start DISABLED (mirroring camera boot-off). An empty store is
byte-identical to today. A presence CONDITION that can't be confirmed (sensing off)
resolves to UNKNOWN and the routine does NOT fire -- never assert "nobody home" when
the house can't sense. Actions reach only the existing gated sinks; the ONE sanctioned
egress is Home Assistant via call_ha_service, with all its gates intact.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import date, datetime, time as dtime

_LOG = logging.getLogger(__name__)

# Trigger kinds. Presence edges reuse the monitors' existing edges; schedule /
# away_by_time run on the routines tick; device_seen is consent-gated upstream.
VALID_TRIGGERS = frozenset({
    "house_arrived", "house_left", "person_arrived", "person_left",
    "room_occupied", "room_empty", "schedule", "house_away_by_time", "device_seen",
})
# The required trigger_params key per kind (absent = no params required).
_TRIGGER_PARAM_KEY = {
    "room_occupied": "room", "room_empty": "room",
    "person_arrived": "person", "person_left": "person",
    "schedule": "at", "house_away_by_time": "by", "device_seen": "mac",
}
# Action kinds. Deliberately NO sensing-on / camera-on kind exists, so a routine can
# never re-arm the master kill-switch or a camera -- the exclusion is structural, not
# a runtime check that could be bypassed. `media` is added later behind a security
# sign-off (the media_player carve-out).
VALID_ACTIONS = frozenset({"ha_service", "notify", "set_watch"})
_ACTION_REQUIRED = {
    "ha_service": ("domain", "service"),
    "notify": ("message",),
    "set_watch": ("on",),
}

MAX_NAME = 80


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
_SCHEMA = """
CREATE TABLE IF NOT EXISTS routines (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    enabled        INTEGER NOT NULL DEFAULT 0,
    trigger_kind   TEXT NOT NULL,
    trigger_params TEXT NOT NULL DEFAULT '{}',
    condition      TEXT,
    actions        TEXT NOT NULL,
    last_fired     TEXT,
    last_status    TEXT
);
"""


def _validate(name, trigger_kind, trigger_params, condition, actions):
    """Raise ValueError on any malformed field BEFORE it reaches SQL, so a junk
    payload becomes a 400 rather than a row that later breaks the engine."""
    nm = (name or "").strip()
    if not nm:
        raise ValueError("name must not be empty")
    if len(nm) > MAX_NAME:
        raise ValueError(f"name must be at most {MAX_NAME} characters")
    if trigger_kind not in VALID_TRIGGERS:
        raise ValueError(f"invalid trigger: {trigger_kind!r}")
    if not isinstance(trigger_params, dict):
        raise ValueError("trigger_params must be an object")
    req = _TRIGGER_PARAM_KEY.get(trigger_kind)
    if req and not str(trigger_params.get(req, "")).strip():
        raise ValueError(f"trigger {trigger_kind!r} requires param {req!r}")
    if trigger_kind in ("schedule", "house_away_by_time"):
        _parse_hhmm(trigger_params[req])   # raises on a bad HH:MM
    if condition is not None:
        # Only the shapes the engine actually evaluates are accepted, so a condition the
        # user authored to GATE a routine can NEVER be silently ignored (fail-open). Today
        # that is exactly {"house": "home"|"away"}; anything else is rejected at write time.
        if not isinstance(condition, dict):
            raise ValueError("condition must be an object or null")
        extra = set(condition) - {"house"}
        if extra:
            raise ValueError(f"unsupported condition key(s): {sorted(extra)} "
                             "(only {'house': 'home'|'away'} is supported)")
        if "house" in condition and condition["house"] not in ("home", "away"):
            raise ValueError("condition 'house' must be 'home' or 'away'")
    if not isinstance(actions, list) or not actions:
        raise ValueError("actions must be a non-empty list")
    for a in actions:
        if not isinstance(a, dict):
            raise ValueError("each action must be an object")
        kind = a.get("kind")
        if kind not in VALID_ACTIONS:
            raise ValueError(f"invalid action kind: {kind!r}")
        params = a.get("params", {})
        if not isinstance(params, dict):
            raise ValueError("action params must be an object")
        for k in _ACTION_REQUIRED[kind]:
            if k not in params:
                raise ValueError(f"action {kind!r} requires param {k!r}")
    return nm


def _parse_hhmm(s: str) -> dtime:
    parts = str(s).split(":")
    if len(parts) != 2:
        raise ValueError(f"time must be HH:MM, got {s!r}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"time out of range: {s!r}")
    return dtime(h, m)


class RoutineStore:
    """SQLite-backed routine registry (shares the git-ignored wavr.db, owns its table;
    ':memory:' for tests; lock-guarded so it can be driven from a thread pool).
    JSON columns are parsed back to dict/list on the way out so callers never see raw
    text. `add` validates before any write. New routines start DISABLED."""

    def __init__(self, path: str = "wavr.db"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["enabled"] = bool(d["enabled"])
        d["trigger_params"] = json.loads(d["trigger_params"] or "{}")
        d["condition"] = json.loads(d["condition"]) if d["condition"] else None
        d["actions"] = json.loads(d["actions"])
        return d

    def add(self, name, trigger_kind, trigger_params=None, actions=None,
            condition=None, enabled=False) -> dict:
        trigger_params = trigger_params or {}
        nm = _validate(name, trigger_kind, trigger_params, condition, actions or [])
        rid = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                "INSERT INTO routines"
                " (id, name, enabled, trigger_kind, trigger_params, condition, actions)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (rid, nm, 1 if enabled else 0, trigger_kind,
                 json.dumps(trigger_params),
                 json.dumps(condition) if condition is not None else None,
                 json.dumps(actions)),
            )
            self._conn.commit()
        return self.get(rid)

    def update(self, rid, name, trigger_kind, trigger_params, actions,
               condition=None) -> dict | None:
        trigger_params = trigger_params or {}
        nm = _validate(name, trigger_kind, trigger_params, condition, actions or [])
        with self._lock:
            cur = self._conn.execute(
                "UPDATE routines SET name=?, trigger_kind=?, trigger_params=?,"
                " condition=?, actions=? WHERE id=?",
                (nm, trigger_kind, json.dumps(trigger_params),
                 json.dumps(condition) if condition is not None else None,
                 json.dumps(actions), rid),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get(rid)

    def set_enabled(self, rid, on: bool) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE routines SET enabled=? WHERE id=?", (1 if on else 0, rid))
            self._conn.commit()
            return cur.rowcount > 0

    def mark_fired(self, rid, when: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE routines SET last_fired=?, last_status=? WHERE id=?",
                (when, status, rid))
            self._conn.commit()

    def get(self, rid) -> dict | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM routines WHERE id=?", (rid,)).fetchone()
        return self._row(r) if r else None

    def list(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM routines ORDER BY name, id").fetchall()
        return [self._row(r) for r in rows]

    def delete(self, rid) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM routines WHERE id=?", (rid,))
            self._conn.commit()
            return cur.rowcount > 0

    def close(self) -> None:
        self._conn.close()


# --------------------------------------------------------------------------- #
# Engine (matching only -- no I/O, no execution)
# --------------------------------------------------------------------------- #
class RoutinesEngine:
    """Given an edge or a tick + live house state, return the enabled routines that
    should fire. Pure: reads the store live (so enable/disable/edit are instant) and
    the injected state callables, returns a list of routine dicts. Execution + the
    last-fired write are the caller's job (kept separate so blocking sinks run
    off the event loop).

    `sensing_on()` -> bool and `house_home()` -> bool|None gate any CONDITION: a
    routine with a presence condition does NOT fire while sensing is off (UNKNOWN),
    the fail-safe against acting on a house that can't currently sense."""

    def __init__(self, store: RoutineStore, sensing_on, house_home=None):
        self._store = store
        self._sensing_on = sensing_on
        self._house_home = house_home
        self._fired_day: dict[str, date] = {}   # once-per-day guard for time triggers

    def _enabled(self):
        return [r for r in self._store.list() if r["enabled"]]

    def _condition_ok(self, routine) -> bool | None:
        """True = fire, False = condition not met (skip), None = UNKNOWN (skip, and
        the reason we skip is that the house can't confirm the state)."""
        cond = routine.get("condition")
        if not cond:
            return True
        if "house" in cond:
            if not self._sensing_on():
                return None
            home = self._house_home() if self._house_home else None
            if home is None:
                return None
            return home == (cond["house"] == "home")
        return True

    def _matches(self, pred) -> list[dict]:
        return [r for r in self._enabled()
                if pred(r) and self._condition_ok(r) is True]

    def on_house_edge(self, home: bool) -> list[dict]:
        kind = "house_arrived" if home else "house_left"
        return self._matches(lambda r: r["trigger_kind"] == kind)

    def on_room_edge(self, room: str, occupied: bool) -> list[dict]:
        kind = "room_occupied" if occupied else "room_empty"
        return self._matches(
            lambda r: r["trigger_kind"] == kind
            and r["trigger_params"].get("room") == room)

    def on_person_edge(self, person: str, home: bool) -> list[dict]:
        kind = "person_arrived" if home else "person_left"
        return self._matches(
            lambda r: r["trigger_kind"] == kind
            and r["trigger_params"].get("person") == person)

    def on_device_edge(self, mac: str, online: bool) -> list[dict]:
        if not online:                      # device_seen fires on APPEARANCE only
            return []
        return self._matches(
            lambda r: r["trigger_kind"] == "device_seen"
            and r["trigger_params"].get("mac", "").lower() == mac.lower())

    def tick(self, now: datetime, house_home: bool | None) -> list[dict]:
        """Time + deadline triggers, evaluated on the routines loop. Fires each such
        routine at most once per calendar day (a periodic tick would otherwise
        re-fire every cycle after the target time). Applies the SAME condition gate
        the edge paths use, and _once() is checked LAST (short-circuit) so a routine
        whose condition/time fails is never marked fired."""
        out = []
        for r in self._enabled():
            kind = r["trigger_kind"]
            if kind == "schedule":
                if (self._reached(now, r["trigger_params"]["at"])
                        and self._condition_ok(r) is True
                        and self._once(r, now)):
                    out.append(r)
            elif kind == "house_away_by_time":
                # "nobody home by HH:MM" ASSERTS the house is away -> it must not fire on a
                # stale/unknowable state: require sensing ON right now (else the latched
                # home state may be stale after a kill-switch toggle) plus the confirmed
                # away, plus any explicit condition, plus once/day.
                if (self._sensing_on()
                        and house_home is False
                        and self._reached(now, r["trigger_params"]["by"])
                        and self._condition_ok(r) is True
                        and self._once(r, now)):
                    out.append(r)
        return out

    @staticmethod
    def _reached(now: datetime, hhmm: str) -> bool:
        return now.time() >= _parse_hhmm(hhmm)

    def _once(self, routine, now: datetime) -> bool:
        """At most once per calendar day, ACROSS restarts. The in-memory guard is the
        fast path; the persisted last_fired (written by mark_fired) is the source of
        truth after a reboot/kiosk-relaunch/update -- without it a schedule that fired
        at 23:00 would fire AGAIN on the first tick after a 23:30 restart."""
        rid = routine["id"]
        today = now.date()
        if self._fired_day.get(rid) == today:
            return False
        last = routine.get("last_fired")
        if last:
            try:
                if datetime.fromisoformat(last).date() == today:
                    self._fired_day[rid] = today   # seed the in-memory guard from the store
                    return False
            except ValueError:
                pass
        self._fired_day[rid] = today
        return True


# --------------------------------------------------------------------------- #
# Executor (runs actions through injected sinks; failure-tolerant per action)
# --------------------------------------------------------------------------- #
class ActionExecutor:
    """Maps each atomic action to its existing sink. One action failing never breaks
    the loop or a sibling action (mirrors the on_rogue tolerance). There is NO sink
    for turning sensing or a camera ON, so a routine cannot re-arm either -- the
    exclusion is structural. Returns 'ok' / 'partial' / 'failed'."""

    def __init__(self, ha_call=None, notify=None, watch_set=None):
        self._ha = ha_call        # (domain, service, entity_id) -> None (raises on failure)
        self._notify = notify     # (message) -> None
        self._watch = watch_set   # (on: bool) -> None

    def run(self, actions: list) -> str:
        ok = fail = 0
        for a in actions:
            try:
                self._run_one(a)
                ok += 1
            except Exception:
                _LOG.warning("routine action failed: kind=%s", a.get("kind"), exc_info=True)
                fail += 1
        if fail == 0:
            return "ok"
        return "failed" if ok == 0 else "partial"

    def _run_one(self, a: dict) -> None:
        kind = a.get("kind")
        p = a.get("params", {})
        if kind == "ha_service":
            if self._ha is None:
                raise RuntimeError("HA control not enabled")
            self._ha(p["domain"], p["service"], p.get("entity_id"))
        elif kind == "notify":
            if self._notify is None:
                raise RuntimeError("notifier not configured")
            self._notify(p["message"])
        elif kind == "set_watch":
            if self._watch is None:
                raise RuntimeError("watch mode not available")
            self._watch(bool(p["on"]))
        else:
            raise ValueError(f"unknown action kind: {kind!r}")
