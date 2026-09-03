"""How much this particular sensor, in this particular room, has earned.

## Why per-sensor and not per-modality

`fusion.DEFAULT_WEIGHTS` says a camera is worth 1.0 and a PIR 0.6. That is a
statement about the *physics of a modality*, and it is the right starting point.
It cannot express the thing that actually decides whether a household trusts
Wavr:

    the hall camera false-positives on the mirror after dark
    the kitchen camera does not

Both are cameras. One earned its weight; the other has not. A single constant
has no way to say so, and averaging them makes the good sensor worse and the bad
one better.

## Deterministic, auditable, and boring on purpose

Every number here is counted, not learned. No model, no training, no opaque
score. Three reasons:

1. **Explainability is the product.** Wavr's differentiator is answering *why*.
   A weight the operator cannot interrogate destroys that, and it is exactly the
   "86% that means nothing" a skeptic rejects.
2. **The data volume is tiny.** One household produces dozens of validated
   transitions, not thousands. Anything fitted to that is fitting noise.
3. **It has to run on a Raspberry Pi** next to the sensing loop.

So: counts in, a bounded multiplier out, and a sentence saying which counts
produced it.

## Capability-specific, because a sensor is not uniformly good

A PIR can be excellent at noticing someone walk in and useless at knowing they
are still there. Collapsing that into one number loses the only part that
matters. Reliability is therefore tracked per `(sensor, capability)`:

  * `presence`      — does it notice someone is there?
  * `absence`       — is its "empty" believable?
  * `count`         — is its headcount right?
  * `still_person`  — does it hold a person who stopped moving?

## The honesty rule this module must not break

**An unmeasured sensor is not a suspect sensor.** With no measurements the
factor is exactly 1.0 and the reason says so. A fresh install must behave
byte-identically to one with no reliability at all — otherwise every new sensor
is quietly penalised for being new, and the operator sees confidence sag for no
reason they can discover.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

# What a sensor can be separately good or bad at. Deliberately few: each one has
# to be measurable by the guided validation walk, or it is a number nobody can
# ever check.
CAP_PRESENCE = "presence"
CAP_ABSENCE = "absence"
CAP_COUNT = "count"
CAP_STILL = "still_person"

CAPABILITIES: frozenset[str] = frozenset({
    CAP_PRESENCE, CAP_ABSENCE, CAP_COUNT, CAP_STILL})

# Below this many observations a sensor is UNMEASURED, not unreliable. The
# factor is 1.0 and the reason says why. Set at 8 because the guided walk
# produces roughly that many per room per session: one session earns a verdict,
# and nothing earns one by accident.
MIN_SAMPLES = 8

# The multiplier never leaves this range. A floor above zero because a sensor
# that is wrong most of the time still carries information — and because a
# factor of 0 would silently delete a source from the merge, which is a
# configuration decision (disable it) rather than something reliability should
# do behind the operator's back. A ceiling of exactly 1.0 because reliability
# may only ever REDUCE trust: a sensor cannot earn more than its modality's
# physical ceiling by behaving well, or a lucky PIR would out-vote a camera.
FACTOR_FLOOR = 0.35
FACTOR_CEIL = 1.0


@dataclass(frozen=True)
class ReliabilityProfile:
    """What one sensor has earned for one capability, and why.

    `reason` is not decoration. It is the sentence the UI shows and the MCP tool
    returns, and it is what makes the factor auditable rather than magic.
    """

    sensor_id: str
    capability: str
    room: str = ""
    samples: int = 0
    correct: int = 0
    factor: float = 1.0
    measured: bool = False
    reason: str = "Not enough measurements yet — treated at full trust."
    detail: dict = field(default_factory=dict)

    @property
    def accuracy(self) -> float | None:
        """Fraction correct, or None when nothing has been measured.

        None rather than 0.0: an unmeasured sensor has not scored zero, and a
        UI that renders 0% for "we never checked" is the exact confusion this
        codebase spends its effort avoiding.
        """
        return (self.correct / self.samples) if self.samples else None

    def to_dict(self) -> dict:
        return {
            "sensor_id": self.sensor_id,
            "capability": self.capability,
            "room": self.room,
            "samples": self.samples,
            "accuracy": self.accuracy,
            "factor": round(self.factor, 3),
            "measured": self.measured,
            "reason": self.reason,
            **({"detail": self.detail} if self.detail else {}),
        }


def _factor_from(correct: int, samples: int) -> float:
    """Accuracy mapped to a bounded multiplier.

    Linear on purpose. A curve would be a modelling choice nobody could defend
    from a few dozen samples, and the operator could not predict its effect.
    Perfect accuracy earns 1.0 (never more); total failure floors at
    FACTOR_FLOOR rather than zero.
    """
    if samples <= 0:
        return FACTOR_CEIL
    acc = max(0.0, min(1.0, correct / samples))
    return FACTOR_FLOOR + (FACTOR_CEIL - FACTOR_FLOOR) * acc


def _reason_for(cap: str, correct: int, samples: int, factor: float) -> str:
    """The sentence a human reads. Concrete counts, never adjectives alone."""
    pct = round(100 * correct / samples)
    noun = {
        CAP_PRESENCE: "noticed someone was there",
        CAP_ABSENCE: "correctly reported the room empty",
        CAP_COUNT: "got the headcount right",
        CAP_STILL: "kept a still person",
    }.get(cap, "was right")
    if factor >= 0.97:
        return f"{noun.capitalize()} in {correct} of {samples} checks."
    return (f"{noun.capitalize()} in {correct} of {samples} checks ({pct}%), "
            f"so Wavr gives it less weight here.")


class ReliabilityStore:
    """Counted evidence about sensors, and the profiles derived from it.

    Derived state, never a system of record: it can be deleted and rebuilt by
    running validation again. That is why there is no migration story here — a
    schema change may simply drop the table.
    """

    def __init__(self, path: str = "wavr.db"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS sensor_reliability (
                sensor_id   TEXT NOT NULL,
                capability  TEXT NOT NULL,
                room        TEXT NOT NULL DEFAULT '',
                samples     INTEGER NOT NULL DEFAULT 0,
                correct     INTEGER NOT NULL DEFAULT 0,
                updated_ts  TEXT,
                PRIMARY KEY (sensor_id, capability, room)
            )""")
        self._conn.commit()

    # -- writing ------------------------------------------------------------

    def record(self, sensor_id: str, capability: str, correct: bool,
               room: str = "", now: datetime | None = None) -> None:
        """One measured check.

        Silently ignores an unknown capability rather than raising: this is
        called from the validation loop while a person is walking around a
        house, and a typo in a caller must not abort their session. The
        capability set is small and closed, so a wrong one is a bug that shows
        up as a profile that never appears, not as data corruption.
        """
        if capability not in CAPABILITIES or not sensor_id:
            return
        ts = (now or datetime.now(timezone.utc)).isoformat()
        with self._lock:
            self._conn.execute("""
                INSERT INTO sensor_reliability
                       (sensor_id, capability, room, samples, correct, updated_ts)
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(sensor_id, capability, room) DO UPDATE SET
                    samples    = samples + 1,
                    correct    = correct + excluded.correct,
                    updated_ts = excluded.updated_ts
            """, (sensor_id, capability, room, 1 if correct else 0, ts))
            self._conn.commit()

    def forget(self, sensor_id: str) -> int:
        """Drop everything measured about one sensor.

        Needed when a sensor is physically moved: a camera that earned its
        reliability in the hall has earned nothing in the kitchen, and carrying
        the old numbers over would be a confident claim about a place it has
        never seen.
        """
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM sensor_reliability WHERE sensor_id = ?", (sensor_id,))
            self._conn.commit()
            return cur.rowcount

    # -- reading ------------------------------------------------------------

    def profile(self, sensor_id: str, capability: str,
                room: str = "") -> ReliabilityProfile:
        """What this sensor earned — or an honest "not measured yet".

        Never returns None. A caller that has to handle absence separately will
        eventually forget to, and the failure mode is a sensor silently dropped
        from the merge.
        """
        base = ReliabilityProfile(sensor_id=sensor_id, capability=capability,
                                  room=room)
        if not sensor_id or capability not in CAPABILITIES:
            return base
        with self._lock:
            row = self._conn.execute("""
                SELECT samples, correct FROM sensor_reliability
                 WHERE sensor_id = ? AND capability = ? AND room = ?
            """, (sensor_id, capability, room)).fetchone()
        if row is None or row["samples"] < MIN_SAMPLES:
            seen = row["samples"] if row else 0
            return ReliabilityProfile(
                sensor_id=sensor_id, capability=capability, room=room,
                samples=seen, correct=(row["correct"] if row else 0),
                factor=FACTOR_CEIL, measured=False,
                reason=(f"Only {seen} of {MIN_SAMPLES} checks so far — "
                        f"treated at full trust until there is enough to judge."
                        if seen else
                        "Not measured yet — treated at full trust."))
        factor = _factor_from(row["correct"], row["samples"])
        return ReliabilityProfile(
            sensor_id=sensor_id, capability=capability, room=room,
            samples=row["samples"], correct=row["correct"], factor=factor,
            measured=True,
            reason=_reason_for(capability, row["correct"], row["samples"], factor))

    def factor(self, sensor_id: str, capability: str = CAP_PRESENCE,
               room: str = "") -> float:
        """Just the multiplier, for the fusion hot path."""
        return self.profile(sensor_id, capability, room).factor

    def list_profiles(self, sensor_id: str = "") -> list[ReliabilityProfile]:
        """Everything measured, or everything about one sensor."""
        sql = ("SELECT sensor_id, capability, room FROM sensor_reliability"
               + (" WHERE sensor_id = ?" if sensor_id else "")
               + " ORDER BY sensor_id, capability, room")
        with self._lock:
            rows = self._conn.execute(
                sql, (sensor_id,) if sensor_id else ()).fetchall()
        return [self.profile(r["sensor_id"], r["capability"], r["room"])
                for r in rows]

    def close(self) -> None:
        self._conn.close()
