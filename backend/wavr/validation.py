"""Guided validation: the operator tells Wavr the truth, Wavr grades itself.

## The objection this answers

"I don't trust what it says." A confidence of 86% is worthless if nobody can say
what 86% was measured against. This module is what turns it into a number with a
provenance: a person walks their own home, states the truth at each step, and
every sensor is scored against it.

## How a session works

    start(room)                     a window opens, truth unknown
    declare(occupied=True)          "I am in the kitchen, now"
      sample() ... sample()         the UI polls; each sample grades every sensor
    declare(occupied=False)         "I have left"
      sample() ... sample()
    finish()                        per-sensor verdicts + plain sentences

Each `declare` closes the previous window and opens a new one. Samples are
attributed to the window they land in, which is what makes two different things
measurable from the same walk:

  * **accuracy** — did this sensor agree with the truth at all?
  * **latency** — how long until it first agreed?

Latency is the number a household actually feels ("the light takes three seconds
to notice me"), and it cannot be derived from a single reading at the moment of
declaration. That is why sampling is a loop and not one call.

## What it refuses to do

**It will not grade a sensor that could not have known.** A sensor that is
offline, stale, or not reporting on that room at all is excluded from the
window — scoring it as wrong would build a reputation out of its absence, and
the operator would see a healthy sensor demoted for having been unplugged during
a test.

**It stores no personal data.** A window records a boolean, an optional count,
and per-sensor agreement counts. Not who walked, not where they stood, not a
frame. The whole point is to be able to publish "the hall camera is right 11
times in 12" without publishing anything about the person who proved it.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from wavr.reliability import (
    CAP_ABSENCE, CAP_COUNT, CAP_PRESENCE, CAP_STILL,
)

STATE_ACTIVE = "active"
STATE_FINISHED = "finished"
STATE_ABANDONED = "abandoned"

# A window this old is stale: the person wandered off mid-session and whatever
# the sensors say now has nothing to do with the truth they last declared.
# Grading against it would be worse than not grading at all.
WINDOW_MAX_S = 300.0


class ValidationError(Exception):
    """A session-level problem worth telling the operator about."""


@dataclass
class Window:
    """One declared truth, and what the sensors did about it."""

    truth_occupied: bool
    truth_count: int | None
    opened_ts: datetime
    # sensor_id -> {"agreed": int, "checked": int, "first_agreed_s": float|None,
    #               "modality": str, "count_right": int, "count_checked": int}
    sensors: dict = field(default_factory=dict)
    samples: int = 0

    def note(self, sensor_id: str, modality: str, agreed: bool,
             elapsed_s: float, count_right: bool | None) -> None:
        row = self.sensors.setdefault(sensor_id, {
            "modality": modality, "agreed": 0, "checked": 0,
            "first_agreed_s": None, "count_right": 0, "count_checked": 0})
        row["checked"] += 1
        if agreed:
            row["agreed"] += 1
            if row["first_agreed_s"] is None:
                # The FIRST time it agreed, which is the latency a household
                # feels. Later agreement says nothing new about how quick it is.
                row["first_agreed_s"] = round(elapsed_s, 2)
        if count_right is not None:
            row["count_checked"] += 1
            row["count_right"] += 1 if count_right else 0


class ValidationStore:
    """Sessions and their windows.

    Kept because a session survives a page reload and a Core restart: somebody
    walking their house should not lose the work because a browser tab closed.
    """

    def __init__(self, path: str = "wavr.db"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS validation_sessions (
                session_id  TEXT PRIMARY KEY,
                room        TEXT NOT NULL,
                state       TEXT NOT NULL,
                started_ts  TEXT NOT NULL,
                ended_ts    TEXT,
                windows     TEXT NOT NULL DEFAULT '[]',
                summary     TEXT
            )""")
        self._conn.commit()

    def create(self, room: str, now: datetime | None = None) -> str:
        sid = uuid.uuid4().hex
        ts = (now or datetime.now(timezone.utc)).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT INTO validation_sessions (session_id, room, state, started_ts)"
                " VALUES (?, ?, ?, ?)", (sid, room, STATE_ACTIVE, ts))
            self._conn.commit()
        return sid

    def get(self, session_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM validation_sessions WHERE session_id = ?",
                (session_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["windows"] = json.loads(out["windows"] or "[]")
        out["summary"] = json.loads(out["summary"]) if out["summary"] else None
        return out

    def save_windows(self, session_id: str, windows: list[dict]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE validation_sessions SET windows = ? WHERE session_id = ?",
                (json.dumps(windows), session_id))
            self._conn.commit()

    def close_session(self, session_id: str, state: str, summary: dict | None,
                      now: datetime | None = None) -> None:
        ts = (now or datetime.now(timezone.utc)).isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE validation_sessions SET state = ?, ended_ts = ?, summary = ?"
                " WHERE session_id = ?",
                (state, ts, json.dumps(summary) if summary else None, session_id))
            self._conn.commit()

    def active_for_room(self, room: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT session_id FROM validation_sessions"
                " WHERE room = ? AND state = ? ORDER BY started_ts DESC LIMIT 1",
                (room, STATE_ACTIVE)).fetchone()
        return row["session_id"] if row else None

    def history(self, room: str = "", limit: int = 20) -> list[dict]:
        sql = ("SELECT session_id, room, state, started_ts, ended_ts, summary"
               " FROM validation_sessions"
               + (" WHERE room = ?" if room else "")
               + " ORDER BY started_ts DESC LIMIT ?")
        args = (room, limit) if room else (limit,)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["summary"] = json.loads(d["summary"]) if d["summary"] else None
            out.append(d)
        return out

    def close(self) -> None:
        self._conn.close()


def _sensor_agreed(src: dict, truth_occupied: bool) -> bool | None:
    """Did this sensor agree with the declared truth? None = it could not know.

    A source that is stale, dead or carrying an invalid timestamp is EXCLUDED
    rather than scored wrong. Grading a sensor for being unplugged during a test
    builds a reputation out of its absence, and the operator would later find a
    healthy sensor demoted with no way to see why.
    """
    if src.get("health") not in ("fresh",):
        return None
    return bool(src.get("presence")) == truth_occupied


def _count_right(src: dict, truth_count: int | None) -> bool | None:
    """Only counting-capable sources that actually reported a number are graded.

    `count is None` means "this source does not count", which is not a wrong
    answer — it is the honest silence the whole product is built on.
    """
    if truth_count is None or src.get("count") is None:
        return None
    return int(src["count"]) == int(truth_count)


class ValidationSession:
    """The live half: one walk, graded as it happens.

    `room_state_fn(room) -> dict | None` returns the CURRENT fused state with its
    `sources[]`. Injected so this module needs no fusion engine, no app, and can
    be driven from a test with a list of dicts.
    """

    def __init__(self, store: ValidationStore, reliability, room_state_fn,
                 now_fn=None):
        self._store = store
        self._reliability = reliability
        self._room_state_fn = room_state_fn
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._windows: dict[str, list[Window]] = {}

    # -- lifecycle ----------------------------------------------------------

    def start(self, room: str) -> dict:
        if not room:
            raise ValidationError("a room is required")
        existing = self._store.active_for_room(room)
        if existing:
            # Resuming beats silently starting a second one: two live sessions
            # for one room would each grade half the walk.
            return {"session_id": existing, "room": room, "resumed": True}
        sid = self._store.create(room, now=self._now())
        self._windows[sid] = []
        return {"session_id": sid, "room": room, "resumed": False}

    def declare(self, session_id: str, occupied: bool,
                count: int | None = None) -> dict:
        """The operator states the truth. Closes the previous window, opens one."""
        session = self._require_active(session_id)
        self._windows.setdefault(session_id, [])
        self._windows[session_id].append(Window(
            truth_occupied=bool(occupied),
            truth_count=(int(count) if count is not None else None),
            opened_ts=self._now()))
        self._persist(session_id)
        return {"session_id": session_id, "room": session["room"],
                "declared": {"occupied": bool(occupied), "count": count},
                "windows": len(self._windows[session_id])}

    def sample(self, session_id: str) -> dict:
        """Grade every sensor against the currently declared truth.

        Called repeatedly by the UI while the operator holds a position. Each
        call is one check per sensor, which is what makes both accuracy and
        first-agreement latency measurable.
        """
        session = self._require_active(session_id)
        windows = self._windows.get(session_id) or []
        if not windows:
            raise ValidationError("declare what is true before sampling")
        window = windows[-1]

        now = self._now()
        elapsed = (now - window.opened_ts).total_seconds()
        if elapsed > WINDOW_MAX_S:
            # The person wandered off. Whatever the sensors say now has nothing
            # to do with the truth they last declared, and grading it would
            # manufacture failures.
            return {"sampled": False, "reason": "window_expired",
                    "elapsed_s": round(elapsed, 1)}

        state = self._room_state_fn(session["room"]) or {}
        graded = 0
        for src in state.get("sources") or []:
            sensor_id = src.get("sensor_id") or ""
            if not sensor_id:
                continue          # anonymous sources cannot build a reputation
            agreed = _sensor_agreed(src, window.truth_occupied)
            if agreed is None:
                continue          # could not have known; not its fault
            window.note(sensor_id, src.get("modality", ""), agreed, elapsed,
                        _count_right(src, window.truth_count))
            graded += 1
        window.samples += 1
        self._persist(session_id)
        return {"sampled": True, "graded_sensors": graded,
                "elapsed_s": round(elapsed, 1),
                "windows": len(windows)}

    def finish(self, session_id: str) -> dict:
        """Close the session, write what was learned, and say it in sentences."""
        session = self._require_active(session_id)
        windows = self._windows.get(session_id) or []
        summary = self._summarize(session["room"], windows)
        self._commit_to_reliability(session["room"], windows)
        self._store.close_session(session_id, STATE_FINISHED, summary,
                                  now=self._now())
        self._windows.pop(session_id, None)
        return summary

    def abandon(self, session_id: str) -> dict:
        """Walk away without recording anything.

        Deliberately writes NOTHING to reliability: a half-finished walk is not
        evidence, and letting it count would mean a session the operator gave up
        on could quietly demote a sensor.
        """
        self._require_active(session_id)
        self._store.close_session(session_id, STATE_ABANDONED, None,
                                  now=self._now())
        self._windows.pop(session_id, None)
        return {"session_id": session_id, "state": STATE_ABANDONED,
                "recorded": False}

    # -- internals ----------------------------------------------------------

    def _require_active(self, session_id: str) -> dict:
        session = self._store.get(session_id)
        if session is None:
            raise ValidationError("unknown session")
        if session["state"] != STATE_ACTIVE:
            raise ValidationError(f"this session is already {session['state']}")
        if session_id not in self._windows:
            # Rehydrate after a Core restart: a person walking their house must
            # not lose the session because the process bounced.
            self._windows[session_id] = [
                Window(truth_occupied=w["truth_occupied"],
                       truth_count=w["truth_count"],
                       opened_ts=datetime.fromisoformat(w["opened_ts"]),
                       sensors=w["sensors"], samples=w["samples"])
                for w in session["windows"]]
        return session

    def _persist(self, session_id: str) -> None:
        self._store.save_windows(session_id, [
            {"truth_occupied": w.truth_occupied, "truth_count": w.truth_count,
             "opened_ts": w.opened_ts.isoformat(), "sensors": w.sensors,
             "samples": w.samples}
            for w in self._windows.get(session_id, [])])

    def _commit_to_reliability(self, room: str, windows: list[Window]) -> None:
        """Turn the walk into counted evidence.

        One `record` per CHECK, not per session: ten samples of a sensor being
        right is ten pieces of evidence, and collapsing them to one would make a
        long careful walk worth the same as a glance.
        """
        if self._reliability is None:
            return
        for w in windows:
            # Presence and absence are separate capabilities: a sensor can be
            # excellent at noticing arrival and hopeless at confirming an empty
            # room, and that difference is the useful part.
            cap = CAP_PRESENCE if w.truth_occupied else CAP_ABSENCE
            for sensor_id, row in w.sensors.items():
                for _ in range(row["agreed"]):
                    self._reliability.record(sensor_id, cap, True, room=room)
                for _ in range(row["checked"] - row["agreed"]):
                    self._reliability.record(sensor_id, cap, False, room=room)
                for _ in range(row["count_right"]):
                    self._reliability.record(sensor_id, CAP_COUNT, True, room=room)
                for _ in range(row["count_checked"] - row["count_right"]):
                    self._reliability.record(sensor_id, CAP_COUNT, False, room=room)

    def _summarize(self, room: str, windows: list[Window]) -> dict:
        """Conclusions first; the numbers behind them stay available.

        A normal user should read a sentence, not a table. An advanced one
        should be able to check the sentence against the counts.
        """
        per_sensor: dict[str, dict] = {}
        for w in windows:
            for sensor_id, row in w.sensors.items():
                agg = per_sensor.setdefault(sensor_id, {
                    "sensor_id": sensor_id, "modality": row["modality"],
                    "checked": 0, "agreed": 0,
                    "count_checked": 0, "count_right": 0,
                    "latencies": []})
                agg["checked"] += row["checked"]
                agg["agreed"] += row["agreed"]
                agg["count_checked"] += row["count_checked"]
                agg["count_right"] += row["count_right"]
                if row["first_agreed_s"] is not None:
                    agg["latencies"].append(row["first_agreed_s"])

        sensors = []
        for agg in per_sensor.values():
            checked, agreed = agg["checked"], agg["agreed"]
            acc = (agreed / checked) if checked else None
            lat = (round(sum(agg["latencies"]) / len(agg["latencies"]), 1)
                   if agg["latencies"] else None)
            sensors.append({
                "sensor_id": agg["sensor_id"],
                "modality": agg["modality"],
                "checks": checked,
                "agreed": agreed,
                "accuracy": acc,
                "typical_response_s": lat,
                "count_checks": agg["count_checked"],
                "count_right": agg["count_right"],
                "verdict": _verdict(acc, checked),
                "note": _sensor_note(acc, lat, agg),
            })
        sensors.sort(key=lambda s: s["sensor_id"])

        return {
            "room": room,
            "windows": len(windows),
            "total_checks": sum(s["checks"] for s in sensors),
            "sensors": sensors,
            "conclusion": _room_conclusion(room, sensors),
        }


def _verdict(accuracy: float | None, checks: int) -> str:
    """A word, not a number, and an honest one when there is nothing to say."""
    if accuracy is None or checks == 0:
        return "not measured"
    if checks < 4:
        return "too few checks"
    if accuracy >= 0.95:
        return "excellent"
    if accuracy >= 0.8:
        return "good"
    if accuracy >= 0.6:
        return "mixed"
    return "poor"


def _sensor_note(accuracy: float | None, latency: float | None,
                 agg: dict) -> str:
    """The sentence under the verdict. Concrete, and silent when unsure."""
    if accuracy is None:
        return "Never answered during this walk."
    bits = [f"Agreed {agg['agreed']} of {agg['checked']} times."]
    if latency is not None:
        bits.append(f"Typically noticed within {latency:g}s.")
    if agg["count_checked"]:
        bits.append(f"Headcount right {agg['count_right']} of "
                    f"{agg['count_checked']} times.")
    return " ".join(bits)


def _room_conclusion(room: str, sensors: list[dict]) -> str:
    """What the operator came for: is this room trustworthy?"""
    if not sensors:
        return (f"Nothing was measured in {room}. No sensor was reporting on it "
                f"during the walk.")
    measured = [s for s in sensors if s["accuracy"] is not None]
    if not measured:
        return f"No sensor in {room} answered during the walk."
    best = max(measured, key=lambda s: s["accuracy"])
    worst = min(measured, key=lambda s: s["accuracy"])
    if worst["accuracy"] >= 0.8:
        return (f"{room} is well covered — every sensor tracked the walk "
                f"closely.")
    if best["accuracy"] >= 0.8:
        return (f"{room} is covered, but {worst['sensor_id']} disagreed often; "
                f"Wavr now gives it less weight here.")
    return (f"{room} is unreliable: no sensor tracked the walk closely. "
            f"Adding or repositioning a sensor would help more than tuning.")
