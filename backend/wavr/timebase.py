"""Making timestamps from different clocks comparable — and being honest about
the part that cannot be solved.

## Why this has to exist before any external provider

Every first-party source here stamps events with `datetime.now(timezone.utc)`,
so they all share one clock and `fusion` can subtract them freely. The moment
Wavr ingests something it did not write, that stops being true. A Home Assistant
box, a positioning platform, an ESP32 that never reached an NTP server — each has
its own idea of the time, and fusion's freshness decay is pure subtraction
against it:

    a provider whose clock is two minutes SLOW   -> every event looks two
                                                    minutes stale, and its
                                                    evidence is discarded
    a provider whose clock is two minutes FAST   -> every event looks eternally
                                                    fresh, and a reading from
                                                    the middle of last night
                                                    keeps voting

Neither shows up as an error. The first looks like a sensor that never
contributes; the second looks like a sensor that is always sure.

## The part that genuinely cannot be solved

From one-way messages you can measure exactly one thing:

    apparent_lag = received_at - stated_at
                 = clock_offset + transit_time

Wavr cannot separate those two terms. A provider whose clock is two seconds slow
and a provider that takes two seconds to deliver produce identical observations,
and no amount of arithmetic on one-way samples will tell them apart — that is
what a round trip is for, and Wavr does not get one from a source that merely
pushes events at it.

So the estimate here is the MINIMUM apparent lag over a recent window, on the
reasoning that the fastest delivery observed had close to zero transit, leaving
mostly offset. It is an estimate, it is labelled as one, and it is deliberately
conservative: `min` can only ever under-correct, and under-correcting leaves a
source looking slightly stale — which costs it a little trust — while
over-correcting would make stale evidence look current, which is the failure
that matters.

## The rules

**Nothing is rewritten silently.** A normalized stamp carries the original, the
correction applied and why. A pipeline that quietly moved timestamps around would
make every downstream "why did it think that?" unanswerable.

**A big skew is not corrected, it is reported.** Beyond a couple of minutes this
is not drift, it is a misconfiguration — usually a timezone. Correcting it would
paper over something the operator needs to fix, and the correction would rest on
an estimate far outside the range that reasoning supports. Those events are
stamped with their RECEIPT time instead, which is wrong by at most the transit
time and never by an unknown amount.

**A timestamp is never in the future.** A clock running ahead would otherwise
produce evidence that stays fresh forever, because the decay window never starts.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# How many recent samples the offset estimate is drawn from. Enough that one
# unusually slow delivery cannot set the floor for long, small enough that a
# provider whose clock is corrected by NTP is believed again within seconds.
WINDOW = 32

# Past this, treat the source's clock as unusable rather than correcting it. Two
# minutes is comfortably beyond any real drift on a box that syncs at all, and
# comfortably below the smallest timezone error (fifteen minutes).
MAX_CORRECTION_S = 120.0

# A skew this close to a whole number of hours (or the odd 30/45-minute zone) is
# almost certainly a timezone bug, and saying so turns an inscrutable number into
# something an operator can act on in one step.
TZ_TOLERANCE_S = 90.0
_TZ_STEPS_S = (900.0, 1800.0, 2700.0, 3600.0)   # 15m, 30m, 45m, 1h

# Why a stamp reads the way it does.
SRC_STATED = "stated"              # the provider's own time, trusted as-is
SRC_CORRECTED = "corrected"        # the provider's time, shifted by the estimate
SRC_RECEIVED = "received"          # the provider's clock could not be used
SRC_ASSUMED_UTC = "assumed_utc"    # no timezone given; read as UTC and flagged


class TimeError(ValueError):
    """A timestamp that cannot be turned into a moment."""


def parse(value, *, default_tz=timezone.utc) -> datetime:
    """An ISO-8601 string or datetime as an aware UTC datetime.

    A NAIVE timestamp is read in `default_tz` — the same thing `fusion._as_utc`
    has always done — but callers that care get told, through `normalize`'s
    `assumed_utc` flag, rather than having the assumption disappear into the
    value. For a first-party source the assumption is correct by construction;
    for an external one it is a guess, and an hour-shaped error downstream is
    much easier to diagnose when something recorded that a guess was made.
    """
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise TimeError(f"not a timestamp: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    return dt.astimezone(timezone.utc)


def looks_like_a_timezone(skew_s: float) -> str:
    """A plain-language read on a skew that is suspiciously round.

    Empty string when it is not, so a caller can `if` on it. Worth the special
    case because "your provider's clock is 3600 seconds off" and "your provider
    is set to the wrong timezone" send somebody to completely different places,
    and only one of them is right roughly every time this fires.
    """
    mag = abs(skew_s)
    for step in _TZ_STEPS_S:
        # Multiples of each step, so 2h and 5h30 are caught as well as 1h.
        if step and abs(mag - round(mag / step) * step) <= TZ_TOLERANCE_S and mag >= step - TZ_TOLERANCE_S:
            hours = mag / 3600.0
            direction = "behind" if skew_s > 0 else "ahead of"
            return (f"about {hours:.2g}h {direction} this Core — that is a "
                    f"timezone setting, not clock drift")
    return ""


@dataclass
class Stamp:
    """One timestamp, and the full story of how it got that way."""

    at: datetime                 # what Wavr will use
    stated: datetime             # what the provider said
    received: datetime           # when this Core saw it
    source: str = SRC_STATED
    correction_s: float = 0.0
    assumed_utc: bool = False
    note: str = ""

    @property
    def corrected(self) -> bool:
        return self.source != SRC_STATED

    def to_dict(self) -> dict:
        out = {"at": self.at.isoformat(), "source": self.source}
        if self.corrected:
            out["stated"] = self.stated.isoformat()
            out["correction_s"] = round(self.correction_s, 3)
        if self.assumed_utc:
            out["assumed_utc"] = True
        if self.note:
            out["note"] = self.note
        return out


@dataclass
class ClockEstimate:
    """What Wavr believes about one source's clock, and how sure it is.

    `samples` is published because the estimate from three observations is worth
    much less than the one from thirty, and a screen that showed the number
    without the sample count would invite more confidence than it has earned.
    """

    source_id: str
    offset_s: float = 0.0
    samples: int = 0
    last_lag_s: float = 0.0
    usable: bool = True
    note: str = ""
    lags: deque = field(default_factory=lambda: deque(maxlen=WINDOW), repr=False)

    def to_dict(self) -> dict:
        out = {"source_id": self.source_id, "offset_s": round(self.offset_s, 3),
               "samples": self.samples, "usable": self.usable}
        if self.note:
            out["note"] = self.note
        return out


class TimeBase:
    """Per-source clock estimates, and the normalization built on them.

    One instance per Core. Holds a bounded window of lags per source and nothing
    else — no history, no events. A component that accumulated timing history
    here would be a second copy of the trace recorder with none of its
    sanitisation.
    """

    def __init__(self, *, now_fn=None, max_correction_s: float = MAX_CORRECTION_S):
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._max = max_correction_s
        self._clocks: dict[str, ClockEstimate] = {}

    def estimate(self, source_id: str) -> ClockEstimate:
        est = self._clocks.get(source_id)
        if est is None:
            est = ClockEstimate(source_id=source_id)
            self._clocks[source_id] = est
        return est

    def normalize(self, value, *, source_id: str = "",
                  received=None, trusted: bool = False) -> Stamp:
        """Turn one provider timestamp into a moment Wavr can compare.

        `trusted=True` is for first-party sources that demonstrably share this
        Core's clock — they call `datetime.now(timezone.utc)` in this very
        process — so measuring an offset for them would be measuring nothing but
        the cost of the function call, and correcting by it would add noise to a
        stamp that was already exact.
        """
        received = parse(received) if received is not None else self._now()
        naive = isinstance(value, str) and not _has_offset(value)
        stated = parse(value)

        if trusted or not source_id:
            # No source identity means no per-source estimate is possible: two
            # providers' samples would be pooled into one meaningless average.
            # Taking the timestamp as given is the honest fallback — and it is
            # exactly what happened before this module existed.
            return Stamp(at=min(stated, received), stated=stated,
                         received=received, assumed_utc=naive)

        est = self.estimate(source_id)
        lag = (received - stated).total_seconds()
        est.lags.append(lag)
        est.last_lag_s = lag
        est.samples = len(est.lags)
        # The minimum, for the reason in the module docstring: the fastest
        # delivery seen had the least transit mixed in, so it is the sample
        # closest to the offset alone.
        est.offset_s = min(est.lags)

        if abs(est.offset_s) > self._max:
            est.usable = False
            est.note = (looks_like_a_timezone(est.offset_s)
                        or f"clock is {abs(est.offset_s):.0f}s "
                           f"{'behind' if est.offset_s > 0 else 'ahead of'} this "
                           f"Core — too far out to correct, so Wavr is using the "
                           f"time it received each reading instead")
            return Stamp(at=received, stated=stated, received=received,
                         source=SRC_RECEIVED, correction_s=lag,
                         assumed_utc=naive, note=est.note)

        est.usable = True
        est.note = ""
        at = stated + timedelta(seconds=est.offset_s)
        # Never ahead of the moment it arrived. A clock running fast would
        # otherwise produce evidence whose decay window has not started, so it
        # would stay fresh forever and keep voting long after the room changed.
        at = min(at, received)
        return Stamp(
            at=at, stated=stated, received=received,
            source=SRC_ASSUMED_UTC if naive else (
                SRC_CORRECTED if est.offset_s else SRC_STATED),
            correction_s=(at - stated).total_seconds(),
            assumed_utc=naive)

    def forget(self, source_id: str) -> None:
        self._clocks.pop(source_id, None)

    def to_dict(self) -> dict:
        rows = [self._clocks[k].to_dict() for k in sorted(self._clocks)]
        return {
            "clocks": rows,
            "unusable": [r["source_id"] for r in rows if not r["usable"]],
            "note": ("Wavr cannot tell a slow clock from a slow network — both "
                     "look like delay on a one-way message. These offsets are "
                     "estimates from the fastest delivery seen, which can only "
                     "under-correct, never over-correct."),
        }


def _has_offset(text: str) -> bool:
    """Whether an ISO string carries a timezone.

    Only the tail matters, and only after the time part: a date's own hyphens
    would otherwise read as a negative offset, so `2026-09-04` would look
    zone-aware when it carries no time at all.
    """
    s = text.strip()
    if s.endswith(("Z", "z")):
        return True
    tail = s.split("T")[-1] if "T" in s else (s.split(" ")[-1] if " " in s else "")
    return "+" in tail or "-" in tail
