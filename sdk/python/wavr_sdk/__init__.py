"""Wavr Spatial SDK — Python.

For local tools, automation, research and agents. Deliberately NOT part of the
`wavr` server package: an SDK that requires installing a Core is not an SDK, and
somebody writing a script to log occupancy should not end up with FastAPI,
OpenCV and a fusion engine on their machine.

    from wavr_sdk import Wavr

    wavr = Wavr("https://192.168.1.10:8443", token="...")
    wavr.connect()

    kitchen = wavr.context("kitchen")
    if kitchen.can("count"):
        print(kitchen.occupancy)
    for why in kitchen.limitations:
        print("but:", why)

    for event in wavr.events():          # blocks, reconnects, resumes
        print(event["event"], event["room"])

## What this does that `requests.get` would not

**It keeps `None` from becoming `0`.** `occupancy` is `None` when nothing in the
room can count, and `occupancy_known` says so outright. Every script that has to
decide for itself what a missing number means decides `0`, because `0` is what
makes the next line of code work.

**It resumes.** `events()` reconnects and asks for everything after the last
event it saw, so a dropped link costs latency rather than information.

**Its failures are typed.** `WavrAuthError` and `WavrUnreachable` are different
problems with different fixes, and a script that retries the first one forever is
a script nobody can debug.

Standard library only. `pip install wavr-sdk` pulls in nothing at all.

SPDX-License-Identifier: AGPL-3.0-or-later
"""
from __future__ import annotations

import json
import random
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

__all__ = [
    "Wavr", "RoomContext", "WavrError", "WavrAuthError", "WavrNotFound",
    "WavrUnreachable", "WavrProtocolError", "PROTOCOL_VERSION",
]

#: The context and event shape this SDK was written against.
PROTOCOL_VERSION = 1

# Reconnect backoff, jittered so a machine running several tools does not have
# them all reconnect in lockstep after a Core restart.
_BASE_BACKOFF_S = 0.5
_CAP_BACKOFF_S = 30.0
_JITTER = 0.25

# How often the event loop asks for new events. Well inside the Core's
# 200-event tail, so a poll at this rate cannot miss one.
_POLL_INTERVAL_S = 2.0


class WavrError(Exception):
    """Anything this SDK could not do."""


class WavrUnreachable(WavrError):
    """The Core did not answer. Retrying may work."""


class WavrAuthError(WavrError):
    """The credential was refused. Retrying will not help."""


class WavrNotFound(WavrError):
    """No such room, anchor or route."""


class WavrProtocolError(WavrError):
    """The Core answered with something this SDK cannot read."""


@dataclass(frozen=True)
class RoomContext:
    """One room, as an application sees it."""

    room: str
    precision: str = "none"
    confidence: float = 0.0
    occupied: bool | None = None
    #: ``None`` when nothing here can count. Never 0 for "unknown".
    occupancy: int | None = None
    occupancy_known: bool = False
    capabilities: tuple[str, ...] = ()
    anchors: tuple[dict, ...] = ()
    devices: tuple[dict, ...] = ()
    sensors: tuple[dict, ...] = ()
    #: Plain sentences naming what this room cannot answer right now.
    limitations: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, raw: dict) -> "RoomContext":
        raw = raw or {}
        return cls(
            room=str(raw.get("room") or ""),
            precision=str(raw.get("precision") or "none"),
            confidence=float(raw.get("confidence") or 0.0),
            occupied=raw.get("occupied"),
            occupancy=raw.get("occupancy"),
            occupancy_known=bool(raw.get("occupancy_known")),
            capabilities=tuple(raw.get("capabilities") or ()),
            anchors=tuple(raw.get("anchors") or ()),
            devices=tuple(raw.get("devices") or ()),
            sensors=tuple(raw.get("sensors") or ()),
            limitations=tuple(raw.get("limitations") or ()),
            raw=raw)

    def can(self, capability: str) -> bool:
        return capability in self.capabilities

    def anchor(self, name: str) -> dict | None:
        for a in self.anchors:
            if a.get("name") == name:
                return a
        return None

    def displays(self) -> list[dict]:
        """Devices here that SAID they have a screen.

        One that never reported is not counted: "did not say" and "has no
        screen" send a script to different places.
        """
        return [d for d in self.devices if d.get("display") is True]


def _backoff(attempt: int, rand=random.random) -> float:
    nominal = min(_BASE_BACKOFF_S * (2 ** max(0, attempt - 1)), _CAP_BACKOFF_S)
    return max(0.1, nominal * (1.0 + _JITTER * (2.0 * rand() - 1.0)))


class Wavr:
    """A connection to one Wavr Core."""

    def __init__(self, base_url: str = "http://127.0.0.1:8000", *, token: str = "",
                 timeout: float = 10.0, verify_tls: bool = True, opener=None):
        """
        Verification is ON by default and should stay on.

        `verify_tls=False` exists because a home Core serves a self-signed
        certificate over the LAN, and it is a named argument rather than a silent
        default so that turning it off is a decision somebody can find by
        grepping their own code. Understand what it costs: without verification,
        anything on the network can impersonate the Core, and this connection
        carries where people are in the house. The better fix is to trust the
        Core's certificate once -- `ssl.create_default_context(cafile=...)`,
        passed through your own opener -- rather than to trust everything
        forever.
        """
        self.base_url = str(base_url or "").rstrip("/")
        self.token = token
        self.timeout = float(timeout)
        self.space: dict | None = None
        self.protocol_version: int | None = None
        #: True when the Core speaks a newer contract than this SDK. Surfaced
        #: rather than raised: refusing would break every tool the day a Core
        #: updates, and ignoring it would let a changed field through.
        self.protocol_ahead = False
        self._ctx = None if verify_tls else ssl._create_unverified_context()
        self._opener = opener

    # -- transport -----------------------------------------------------------

    def _headers(self) -> dict:
        h = {"Accept": "application/json", "X-Wavr-Local": "1"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _request(self, path: str, payload=None):
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        headers = self._headers()
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            url, data=data, headers=headers,
            method="POST" if data is not None else "GET")
        try:
            if self._opener is not None:
                raw = self._opener(req)
            else:
                with urllib.request.urlopen(req, timeout=self.timeout,
                                            context=self._ctx) as r:
                    raw = r.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise WavrAuthError(f"not authorised for {path}") from exc
            if exc.code == 404:
                raise WavrNotFound(f"{path} not found") from exc
            raise WavrError(f"Wavr returned {exc.code} for {path}") from exc
        except Exception as exc:      # noqa: BLE001 -- any transport can fail
            raise WavrUnreachable(
                f"cannot reach Wavr at {self.base_url}: {exc}") from exc
        try:
            return json.loads(raw or b"{}")
        except (TypeError, ValueError) as exc:
            raise WavrProtocolError(f"{path} did not return JSON") from exc

    # -- the surface ---------------------------------------------------------

    def connect(self) -> "Wavr":
        """Reach the Core, learn the Space, and check the contract version.

        At startup rather than lazily, so a tool finds out while it can still
        decide what to do — not at the moment it reads a field whose meaning
        changed under it.
        """
        body = self._request("/api/experience/context")
        self.space = body.get("space")
        self.protocol_version = body.get("protocol_version")
        self.protocol_ahead = (isinstance(self.protocol_version, int)
                               and self.protocol_version > PROTOCOL_VERSION)
        return self

    def space_context(self) -> dict:
        return self._request("/api/experience/context")

    def rooms(self) -> list[str]:
        return [r.get("room", "") for r in self.space_context().get("rooms", [])]

    def context(self, room: str = ""):
        """One room's context, or every room's."""
        if room:
            quoted = urllib.parse.quote(room, safe="")
            return RoomContext.from_dict(
                self._request(f"/api/experience/context/{quoted}"))
        return [RoomContext.from_dict(r)
                for r in self.space_context().get("rooms", [])]

    def anchors(self, room: str = "") -> list[dict]:
        qs = f"?room={urllib.parse.quote(room, safe='')}" if room else ""
        return self._request(f"/api/anchors{qs}").get("anchors", [])

    def resolve_anchor(self, provider_id: str, external_id: str) -> list[dict]:
        """Which Wavr anchors an external system's id refers to.

        A list. Two runtimes can bind their own id to the same place, and
        returning the first would hide the collision behind a plausible answer.
        """
        p = urllib.parse.quote(provider_id, safe="")
        e = urllib.parse.quote(external_id, safe="")
        return self._request(f"/api/anchors/resolve/{p}/{e}").get("anchors", [])

    def coverage(self) -> dict:
        return self._request("/api/coverage")

    def compatibility(self, manifest: dict, room: str = "") -> dict:
        """Whether this Space can support an experience.

        Not permission — a person grants that. This says what a room can
        produce.
        """
        return self._request("/api/experience/compatibility",
                             {"manifest": manifest, "room": room})

    def scopes(self) -> dict:
        return self._request("/api/experience/scopes")

    # -- Sessions ------------------------------------------------------------

    def open_session(self, experience_id: str, *, room: str = "",
                     metadata: dict | None = None) -> dict:
        """Open a session: a name for "this experience is running, in this room".

        Wavr will tell you when that room changes or when a screen appears in
        it. It does NOT move your application's state anywhere — that is yours
        to carry.
        """
        return self._request("/api/experience/sessions",
                             {"experience_id": experience_id, "room": room,
                              "metadata": metadata})

    def observe_session(self, session_id: str, *, room: str = "") -> dict:
        """Re-evaluate a session and get back what changed.

        The handoff primitive. It reports that a display became available;
        whether to offer it is your decision, because it depends on what your
        experience IS — a recipe follows somebody to a screen, a private message
        does not.
        """
        sid = urllib.parse.quote(session_id, safe="")
        return self._request(f"/api/experience/sessions/{sid}/observe",
                             {"room": room})

    def join_session(self, session_id: str, device_id: str) -> dict:
        sid = urllib.parse.quote(session_id, safe="")
        return self._request(f"/api/experience/sessions/{sid}/devices",
                             {"device_id": device_id})

    def recent_events(self, since: str = "", limit: int = 50) -> list[dict]:
        q = urllib.parse.urlencode({"since": since, "limit": limit})
        return self._request(f"/api/events/recent?{q}").get("events", [])

    def events(self, *, room: str = "", since: str = "", reconnect: bool = True,
               sleep=time.sleep):
        """Yield semantic events forever, reconnecting on its own.

        Resumes from the last event seen rather than from "now", so a dropped
        link costs latency and not information. A generator rather than a
        callback because a Python tool's natural shape is a for-loop, and a
        callback API would push every user into writing one of these anyway.

        This POLLS the Core's event tail every couple of seconds rather than
        holding a WebSocket open. That is a deliberate trade: the tools this SDK
        serves -- scripts, automation, agents -- are not harmed by two seconds,
        and a stdlib-only install is worth more to them than the latency is. The
        Core does expose a real push stream at `/ws/events` for anything that
        needs it; the JavaScript SDK uses it, because an interactive experience
        genuinely does.

        The tail is bounded at 200 events, which no real house can outrun at
        this interval, so polling cannot silently skip one.
        """
        attempt = 0
        cursor = since
        while True:
            try:
                for ev in self._poll_events(cursor, sleep):
                    if ev.get("at"):
                        cursor = ev["at"]
                    if not room or ev.get("room") == room:
                        yield ev
                    attempt = 0
            except WavrAuthError:
                raise      # a credential does not fix itself by retrying
            except WavrError:
                if not reconnect:
                    raise
                attempt += 1
                sleep(_backoff(attempt))

    def _poll_events(self, since: str, sleep):
        """The dependency-free event source.

        Polls the Core's live tail, which is bounded at 200 events and therefore
        cannot be outrun at this interval by any real house.
        """
        while True:
            events = self.recent_events(since=since, limit=200)
            for ev in events:
                if ev.get("at"):
                    since = ev["at"]
                yield ev
            sleep(_POLL_INTERVAL_S)
