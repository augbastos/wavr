"""Per-person arrived/left edges, so a routine's "when I arrive" is a real edge and
not a stateless snapshot.

`compose_known_presence` already tells you WHO is currently present, but as a pull
snapshot with no edge/debounce -- it can't tell "Augusto just walked in" from "Augusto
has been home". PersonPresence adds exactly that: fed the CURRENT set of present known
persons each cycle, it emits on_edge(person, home) when a person's DEBOUNCED state
flips, mirroring AwayMonitor's house-level grace so a phone briefly dropping off ARP
doesn't flap.

Consent is inherited, not re-checked here: the caller feeds only persons who are
present AND named under the current consent (known_present_persons already applies the
identity/consent gate), so an anonymous ("yellow") or withdrawn ("red") device never
produces a named person edge.
"""
from __future__ import annotations

from typing import Callable


class PersonPresence:
    """Fed a set of present person names per cycle; emits on_edge(person, home: bool)
    on a debounced flip. The FIRST update establishes the baseline (whoever is already
    home at boot) WITHOUT firing -- so a person present when the Core starts does not
    spuriously read as "just arrived", same discipline as AwayMonitor's first-determination
    guard. `grace` departures in a row are required before a left edge, so a one-cycle ARP
    miss doesn't fire a false "left"."""

    def __init__(self, on_edge: Callable[[str, bool], None] | None = None, grace: int = 2):
        self._on_edge = on_edge
        self._grace = grace
        self._home: dict[str, bool] = {}          # person -> currently home (debounced)
        self._absent_streak: dict[str, int] = {}
        self._primed = False

    def home_persons(self) -> set[str]:
        """The set currently considered home (debounced) -- for a routine CONDITION or a
        glance view."""
        return {p for p, home in self._home.items() if home}

    def update(self, present: set[str]) -> None:
        if not self._primed:
            # Baseline: whoever is already present at boot is home, no edge.
            self._primed = True
            for p in present:
                self._home[p] = True
                self._absent_streak[p] = 0
            return
        # Arrivals: present now and not currently home -> arrived edge.
        for p in present:
            self._absent_streak[p] = 0
            if not self._home.get(p, False):
                self._home[p] = True
                self._emit(p, True)
        # Departures: currently home but not present now -> debounce, then left edge.
        for p in list(self._home):
            if self._home[p] and p not in present:
                self._absent_streak[p] = self._absent_streak.get(p, 0) + 1
                if self._absent_streak[p] >= self._grace:
                    self._home[p] = False
                    self._emit(p, False)

    def _emit(self, person: str, home: bool) -> None:
        if self._on_edge:
            self._on_edge(person, home)


class RoomPresence:
    """Per-room occupied/empty edges, fed each room's fused occupancy on the ingest.
    The fusion layer already debounces a room's occupancy, so this only tracks the last
    value and fires on_edge(room, occupied) on a flip. The FIRST determination per room
    fires nothing (a room already occupied at boot is not a fresh 'filled' edge)."""

    def __init__(self, on_edge: Callable[[str, bool], None] | None = None):
        self._on_edge = on_edge
        self._last: dict[str, bool] = {}

    def handle(self, room: str, occupied: bool) -> None:
        prev = self._last.get(room)
        if prev is not None and prev != occupied and self._on_edge:
            self._on_edge(room, occupied)
        self._last[room] = occupied
