"""The native client view model: what a person needs to know, in one snapshot.

A native client (Android, Android TV, a desktop window, a CLI) reads three
answers the Core already gives -- `/api/runtime`, `/api/attention`,
`/api/state` -- and needs them as one typed, display-ready shape. This module
is the canonical definition of that shape; `native/` implements the same
function and is held to it by `conformance/client_view.json`, generated from
this file.

It PROJECTS; it never decides. Every verdict in the snapshot -- the runtime
state, the attention count, a room's occupancy and confidence, a source's
health -- is copied from the Core's own answer. The only things computed here
are presentation facts that cannot disagree with the Core: the exit code
`wavr status` already defines, a stable room order, and whether any room is
under Watch. A field of the wrong type is dropped to its "unknown" form (None,
an empty list), never coerced into a plausible value: an untrusted or garbled
answer must not render as a confident one.

Schema (version 1):

  SpaceSnapshot   {schema, reachable, error, runtime, attention, rooms,
                   rooms_readable, privacy, exit_code}
  RuntimeView     {state, headline, space, role, uptime_s, last_state_age_s,
                   findings: [FindingView {key, state, text, detail}]}
  AttentionView   {total, blocking, degraded, info, headline, could_not_check,
                   items: [AttentionItem {key, band, title, detail, where,
                   action, since, count}]}                 (None = could not read)
  RoomView        {room, occupied, confidence, person_count, precision_level,
                   explanation, ts, watch, unrecognized,
                   sources: [SourceView {modality, sensor_id, presence,
                   confidence, age_s, health, count}]}
  PrivacyView     {watch}
"""
from __future__ import annotations

import math

SCHEMA = 1

# Mirrors wavr.status: 0 nothing needs you, 1 something does (or could not be
# checked), 2 the Core did not answer.
OK, ATTENTION, UNREACHABLE = 0, 1, 2


def _str(v):
    return v if isinstance(v, str) else None


def _num(v):
    # bool is an int in Python and never a measurement here; NaN/inf are not numbers
    # a person can be shown.
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _bool(v):
    return v if isinstance(v, bool) else None


def _dicts(v):
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _strs(v):
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def runtime_view(runtime) -> dict | None:
    # No textual state is no answer: a verdict cannot be read out of garbage,
    # and a missing one must never fall through to "healthy".
    if not isinstance(runtime, dict) or not isinstance(runtime.get("state"), str):
        return None
    return {
        "state": _str(runtime.get("state")),
        "headline": _str(runtime.get("headline")),
        "space": _str(runtime.get("space")),
        "role": _str(runtime.get("role")),
        "uptime_s": _num(runtime.get("uptime_s")),
        "last_state_age_s": _num(runtime.get("last_state_age_s")),
        "findings": [{"key": _str(f.get("key")), "state": _str(f.get("state")),
                      "text": _str(f.get("text")), "detail": _str(f.get("detail"))}
                     for f in _dicts(runtime.get("findings"))],
    }


def attention_view(attention) -> dict | None:
    # Same rule as wavr.status: an answer of the wrong shape is no answer -- and
    # an inbox whose count cannot be read cannot say "nothing needs you".
    if not isinstance(attention, dict) or _int(attention.get("total")) is None:
        return None
    return {
        "total": _int(attention.get("total")),
        "blocking": _int(attention.get("blocking")),
        "degraded": _int(attention.get("degraded")),
        "info": _int(attention.get("info")),
        "headline": _str(attention.get("headline")),
        "could_not_check": _strs(attention.get("could_not_check")),
        "items": [{"key": _str(i.get("key")), "band": _str(i.get("band")),
                   "title": _str(i.get("title")), "detail": _str(i.get("detail")),
                   "where": _str(i.get("where")), "action": _str(i.get("action")),
                   "since": _str(i.get("since")), "count": _int(i.get("count"))}
                  for i in _dicts(attention.get("items"))],
    }


def room_view(name: str, room: dict) -> dict:
    return {
        "room": name,
        "occupied": _bool(room.get("occupied")),
        "confidence": _num(room.get("confidence")),
        "person_count": _int(room.get("person_count")),
        "precision_level": _str(room.get("precision_level")),
        "explanation": _str(room.get("explanation")),
        "ts": _str(room.get("ts")),
        "watch": room.get("watch") is True,
        "unrecognized": room.get("unrecognized") is True,
        "sources": [{"modality": _str(s.get("modality")),
                     "sensor_id": _str(s.get("sensor_id")),
                     "presence": _bool(s.get("presence")),
                     "confidence": _num(s.get("confidence")),
                     "age_s": _num(s.get("age_s")),
                     "health": _str(s.get("health")),
                     "count": _int(s.get("count"))}
                    for s in _dicts(room.get("sources"))],
    }


def exit_code(runtime: dict | None, attention: dict | None) -> int:
    """wavr.status.exit_code, over the views: the one verdict a script reads."""
    from wavr.status import exit_code as status_exit
    if runtime is None:
        return UNREACHABLE
    raw_att = None if attention is None else {
        "total": attention["total"], "could_not_check": attention["could_not_check"]}
    return status_exit({"state": runtime["state"]}, raw_att)


def snapshot(runtime, attention, state, *, reachable: bool = True,
             error: str | None = None) -> dict:
    """One SpaceSnapshot from the three raw answers (any of which may be None,
    garbled, or missing because the Core did not answer)."""
    rt = runtime_view(runtime) if reachable else None
    att = attention_view(attention) if reachable else None
    rooms_readable = reachable and isinstance(state, dict)
    rooms = ([room_view(k, v) for k, v in sorted(state.items())
              if isinstance(k, str) and isinstance(v, dict)]
             if rooms_readable else [])
    return {
        "schema": SCHEMA,
        "reachable": bool(reachable and rt is not None),
        "error": error if isinstance(error, str) and error else None,
        "runtime": rt,
        "attention": att,
        "rooms": rooms,
        "rooms_readable": rooms_readable,
        "privacy": {"watch": any(r["watch"] for r in rooms)},
        "exit_code": exit_code(rt, att),
    }
