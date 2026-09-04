"""Handing two devices the parameters they need to range with each other.

## The gap this fills

Android's UWB API (Android 12+) has one device act as **Controller** and the
others as **Controlee**. Before they can range, they must agree on a local
address, a complex channel and a session key — and the specification is explicit
that this exchange is left to the application, over a secure out-of-band channel
of its choosing.

Every UWB sample application invents its own: a BLE characteristic, a QR code,
a hand-typed hex string. All of them are worse than what a household already
has, because Wavr is already the authenticated authority both devices trust. It
paired them, it knows they belong to the same Space, and it holds a channel to
each. Brokering the session is the thing it is uniquely placed to do.

## The rules, and every one of them is a security rule

**A session key is generated here, and handed out once.** Sixteen random bytes
per session, never derived from anything, never stored in a form that can be
read back. A key that could be re-fetched is a key that leaks with any single
credential replay.

**Both devices must be in the Space, and both must have SAID they have UWB.**
A device whose capability manifest never mentioned UWB is refused rather than
assumed — the tristate rule, applied where guessing would mean handing a session
key to something that cannot use it and might not have asked.

**Sessions are short.** Ranging is a foreground activity; a session that
outlived the interaction would be a key sitting in memory for an afternoon.

**Nothing here places anybody.** The broker sets up a measurement between two
devices. What they do with the resulting distances comes back through the
ordinary provider path, where it is subject to the same precision ceiling as
anything else — a range is a sphere, and Wavr does not turn one into a point.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

# UWB session keys are 128-bit under the STS scheme these APIs use.
SESSION_KEY_BYTES = 16

# Android's UWB channels. The Controller picks one; Wavr picks FOR it, so two
# sessions in one house do not collide by chance.
UWB_CHANNELS = (5, 9)
# Preamble indices that pair with those channels in the common configurations.
UWB_PREAMBLES = (9, 10, 11, 12)

# How long a brokered session is valid before it must be re-requested. Ranging
# is a foreground activity; a session that outlived the interaction would be a
# key sitting in memory for an afternoon.
SESSION_TTL_S = 300.0

# A cap on concurrent sessions. Reached only by something misbehaving, and the
# stalest goes first.
MAX_SESSIONS = 32

ROLE_CONTROLLER = "controller"
ROLE_CONTROLEE = "controlee"


class UwbError(ValueError):
    """A ranging session Wavr will not broker."""


@dataclass
class RangingSession:
    """Parameters two devices need to range with each other."""

    session_id: str
    controller: str
    controlees: tuple[str, ...]
    channel: int
    preamble_index: int
    created_ts: float
    # Delivered once per device and then dropped. See `claim`.
    _key: bytes = field(repr=False, default=b"")
    _claimed: set = field(default_factory=set, repr=False)

    def to_dict(self) -> dict:
        """The session WITHOUT its key.

        The key is never part of the ordinary representation, so no logging,
        diagnostics bundle or debug print can carry it by accident. It is
        returned exactly once, by `claim`, to the device that asked.
        """
        return {
            "session_id": self.session_id,
            "controller": self.controller,
            "controlees": list(self.controlees),
            "channel": self.channel,
            "preamble_index": self.preamble_index,
            "claimed_by": sorted(self._claimed),
            "note": ("The session key is delivered once per device and cannot "
                     "be fetched again. Ask for a new session rather than "
                     "trying to recover this one."),
        }


class UwbBroker:
    """Brokers UWB ranging sessions between devices in one Space.

    Holds sessions in memory only. A brokered session is a live arrangement
    between two devices; persisting one would mean a session key survived a
    restart neither device knows about.
    """

    def __init__(self, *, clock=time.monotonic, rand=secrets.token_bytes):
        self._sessions: dict[str, RangingSession] = {}
        self._clock = clock
        self._rand = rand

    def open(self, controller: str, controlees, *, capable) -> RangingSession:
        """Set up a session, or refuse it with the reason.

        `capable(device_id) -> True | False | None` is the caller's read of a
        device's capability manifest. `None` means the device never said, and it
        is treated as a REFUSAL rather than a maybe: handing a session key to a
        device that cannot use it wastes a key, and assuming a capability is how
        a tristate quietly becomes a boolean.
        """
        controller = str(controller or "").strip()
        peers = tuple(str(d).strip() for d in (controlees or ()) if str(d).strip())
        if not controller or not peers:
            raise UwbError("a session needs a controller and at least one controlee")
        if controller in peers:
            raise UwbError("a device cannot range with itself")

        for device in (controller,) + peers:
            answer = capable(device)
            if answer is not True:
                raise UwbError(
                    f"{device} has not reported UWB support"
                    + ("" if answer is False
                       else " — its capability manifest is silent on it, and "
                            "Wavr does not assume a radio it was never told "
                            "about"))

        self._expire()
        if len(self._sessions) >= MAX_SESSIONS:
            oldest = min(self._sessions.values(), key=lambda s: s.created_ts)
            self._sessions.pop(oldest.session_id, None)

        session = RangingSession(
            session_id=f"uwb_{secrets.token_hex(8)}",
            controller=controller,
            controlees=peers,
            # Chosen by Wavr rather than by the Controller, so two sessions in
            # one house do not land on the same channel by coincidence.
            channel=UWB_CHANNELS[len(self._sessions) % len(UWB_CHANNELS)],
            preamble_index=UWB_PREAMBLES[len(self._sessions) % len(UWB_PREAMBLES)],
            created_ts=self._clock(),
            _key=self._rand(SESSION_KEY_BYTES),
        )
        self._sessions[session.session_id] = session
        return session

    def claim(self, session_id: str, device_id: str) -> dict:
        """A device collects its parameters. Once.

        The key is returned to each participant exactly once. A second attempt
        is refused rather than served, because a key that can be re-fetched
        leaks with any single replayed credential — and because the honest
        recovery from a lost key is a new session, not a second copy of an old
        one.
        """
        self._expire()
        session = self._sessions.get(session_id)
        if session is None:
            raise UwbError("no such session, or it has expired")
        if device_id != session.controller and device_id not in session.controlees:
            raise UwbError(f"{device_id} is not part of this session")
        if device_id in session._claimed:
            raise UwbError(
                f"{device_id} has already collected these parameters. A session "
                f"key is delivered once; open a new session instead.")
        session._claimed.add(device_id)
        return {
            "session_id": session.session_id,
            "role": (ROLE_CONTROLLER if device_id == session.controller
                     else ROLE_CONTROLEE),
            "channel": session.channel,
            "preamble_index": session.preamble_index,
            "session_key": session._key.hex(),
            "peers": ([*session.controlees] if device_id == session.controller
                      else [session.controller]),
            "expires_in_s": round(
                max(0.0, SESSION_TTL_S - (self._clock() - session.created_ts)), 1),
        }

    def get(self, session_id: str) -> RangingSession | None:
        self._expire()
        return self._sessions.get(session_id)

    def close(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None

    def list(self) -> list[RangingSession]:
        self._expire()
        return [self._sessions[k] for k in sorted(self._sessions)]

    def _expire(self) -> None:
        now = self._clock()
        for sid in [s.session_id for s in self._sessions.values()
                    if now - s.created_ts > SESSION_TTL_S]:
            self._sessions.pop(sid, None)
