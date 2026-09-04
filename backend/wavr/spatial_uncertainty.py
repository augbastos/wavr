"""How wrong a spatial answer might be — and whether Wavr measured that or not.

## The distinction this module exists to keep

Wavr already publishes two numbers that sound like uncertainty and are not:

  * `confidence` — how much fused mass says somebody is present. It is about
    PRESENCE, not about how precise the answer is.
  * `precision_level` — which rung of the ladder the room reached. It bounds
    the GRANULARITY of the answer and says nothing about how often that answer
    is right.

Neither tells an application "how wrong could this be", which is the question
behind every decision it makes with a spatial fact. So this module answers it —
and answers it in two parts, because the honest answer has two parts.

**Granularity** is what the rung permits, and it is always knowable: a `room`
answer cannot be more precise than a room, whatever else is true.

**Reliability** is how often that answer has actually been right, and it is
usually NOT knowable, because measuring it requires somebody to have walked the
guided validation. So it is reported as measured-or-not, never as a number Wavr
invented.

## The rule

**Wavr never publishes an error figure it did not measure.**

There is a strong pull the other way. Every competing product prints a metres
figure, and one here would be easy: pick a plausible radius per modality, and
nobody could tell. But a camera's positional error depends on its calibration,
its mounting height and the geometry of the room; a radar's on its aim and the
furniture. Wavr does not know any of those, so a number would be decoration
shaped like rigour, and the applications built on it would inherit a confidence
nobody earned.

The one place a real measured figure exists is `spatial_align.Alignment.
residual_m` — the worst per-point error after fitting an XR runtime's frame onto
a room, which IS measured, from correspondences a person supplied. That one is
carried through verbatim.
"""
from __future__ import annotations

from dataclasses import dataclass

from wavr.fusion import _SCOPE_RANK

# What each rung BOUNDS the answer to. Not an error figure — a statement of what
# the answer is about. A `house` reading cannot tell rooms apart no matter how
# confident it is, and that is a fact about the technology rather than a guess.
GRANULARITY: dict[str, str] = {
    "none": "nothing — no reading at all",
    "house": "the whole house; this cannot tell rooms apart",
    "room": "which room, and no finer",
    "count": "which room and how many, but not where in it",
    "position": "roughly where in the room",
}

# What Wavr knows about how OFTEN the answer is right.
BASIS_MEASURED = "measured"        # the guided walk has scored these sensors
BASIS_UNMEASURED = "unmeasured"    # nobody has checked; full trust, not proven
BASIS_NONE = "none"                # nothing is reporting, so there is no answer


@dataclass(frozen=True)
class Uncertainty:
    """What Wavr can and cannot say about how wrong one room's answer might be."""

    room: str
    precision: str = "none"
    basis: str = BASIS_NONE
    #: Sensors contributing, and how many have been validated.
    sensors: int = 0
    measured_sensors: int = 0
    #: Worst measured accuracy among the validated sensors, or None. A fraction,
    #: never a percentage: a percentage in a data field invites somebody to
    #: multiply it by something.
    worst_accuracy: float | None = None
    #: Metres, and ONLY when a real alignment measured it. See the module
    #: docstring on why there is no per-modality default here.
    residual_m: float | None = None

    def to_dict(self) -> dict:
        out = {
            "room": self.room,
            "granularity": GRANULARITY.get(self.precision, GRANULARITY["none"]),
            "precision": self.precision,
            "basis": self.basis,
            "sensors": self.sensors,
            "measured_sensors": self.measured_sensors,
            "worst_accuracy": self.worst_accuracy,
            "note": _note(self),
        }
        if self.residual_m is not None:
            out["residual_m"] = round(self.residual_m, 3)
        return out


def _note(u: "Uncertainty") -> str:
    """The sentence a person or an application reads.

    Written so that "we have not checked" is never mistaken for "it is fine".
    That confusion is the whole reason this module reports a basis at all.
    """
    if u.basis == BASIS_NONE:
        return (f"Nothing is reporting in {u.room}, so there is no answer to be "
                f"uncertain about.")
    granularity = GRANULARITY.get(u.precision, GRANULARITY["none"])
    if u.basis == BASIS_UNMEASURED:
        return (f"This answer is about {granularity}. How often it is RIGHT has "
                f"not been measured here — run a guided check to find out. "
                f"Wavr will not print an error figure it did not measure.")
    accuracy = "" if u.worst_accuracy is None else (
        f" Its least accurate sensor was right "
        f"{round(u.worst_accuracy * 100)}% of the time in the checks so far.")
    return (f"This answer is about {granularity}. "
            f"{u.measured_sensors} of {u.sensors} sensors here have been "
            f"checked against a guided walk.{accuracy}")


def assess(room: str, *, precision: str = "none", coverage_rows=(),
           profile_fn=None, residual_m: float | None = None) -> Uncertainty:
    """What Wavr can say about one room's uncertainty.

    `profile_fn(sensor_id, capability, room) -> ReliabilityProfile | None` is the
    caller's reliability read, injected so this module can be tested without a
    store — and so it holds no opinion about where measurements come from.

    A sensor with too few samples counts as UNMEASURED rather than as a low
    score. `reliability` already treats an unmeasured sensor at full trust
    because it has not earned a penalty; reporting that as an accuracy figure
    here would turn "we have not checked" into a number, which is the thing this
    module exists to prevent.
    """
    working = [r for r in (coverage_rows or ())
               if str(r.get("room") or "") == room and r.get("health") == "ok"]
    if not working or _SCOPE_RANK.get(precision, 0) <= 0:
        return Uncertainty(room=room, precision=precision or "none",
                           basis=BASIS_NONE)

    capability = "count" if _SCOPE_RANK.get(precision, 0) >= _SCOPE_RANK["count"] \
        else "presence"
    measured = 0
    worst: float | None = None
    for row in working:
        profile = None
        if profile_fn is not None:
            try:
                profile = profile_fn(str(row.get("sensor_id") or ""),
                                     capability, room)
            except Exception:      # noqa: BLE001 -- a reliability read must not
                profile = None     # decide an uncertainty by failing
        if profile is None or not getattr(profile, "measured", False):
            continue
        accuracy = getattr(profile, "accuracy", None)
        if accuracy is None:
            continue
        measured += 1
        worst = accuracy if worst is None else min(worst, accuracy)

    return Uncertainty(
        room=room, precision=precision,
        basis=BASIS_MEASURED if measured else BASIS_UNMEASURED,
        sensors=len(working), measured_sensors=measured,
        worst_accuracy=worst, residual_m=residual_m)
