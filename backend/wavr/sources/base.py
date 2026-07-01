from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable

from wavr.events import SensingEvent


@runtime_checkable
class SensorSource(Protocol):
    """A source of canonical sensing events. Each implementation emits one modality
    (or, for the simulator, several) tagged on every SensingEvent."""

    def events(self) -> AsyncIterator[SensingEvent]:
        ...
