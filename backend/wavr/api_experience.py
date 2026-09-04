"""The Experience API: what an application is told about where it is running.

`presence:read`, not admin. An experience asking "what room am I in and what can
you tell me about it" is doing the ordinary thing this feature exists for, and
requiring an admin credential would mean every app in the house ran with the
power to reconfigure the Space.

Nothing here can change anything. There is no write route in this file, and that
is the design: an application consumes context. The moment one of them can also
adjust the Space, the read gate above stops being a safe place to stand.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request

from wavr.auth import parse_bearer
from wavr.experience import build_context, build_space_context, redact
from wavr.experience_manifest import (
    ManifestError, NO_IDENTITY_SCOPE, SPATIAL_SCOPES, evaluate, evaluate_space,
    parse, validate,
)
from wavr.contracts import version
from wavr.experience_grants import DEFAULT_SCOPES, GrantError
from wavr.experience_session import SessionError

# From `contracts`, not defined here: eight shapes carried their own copy of a
# version number and two carried none, which is fine until somebody adds a ninth.
EXPERIENCE_PROTOCOL_VERSION = version("experience_context")


def build_router(*, space_fn, rooms_fn, room_state_fn, coverage_fn, anchors_fn,
                 devices_fn, sessions, grants, devices, profile_fn,
                 require_local, require_scope) -> APIRouter:
    """Routes over the context builders.

    Every dependency is a callable so this module reaches for nothing and so the
    three consumers of a context — HTTP, MCP and the SDKs — are assembled from
    the same functions rather than from three slightly different queries.
    """
    router = APIRouter()

    def _scopes(request, authorization, experience_id: str = ""):
        """What this caller was granted, or `None` for unscoped.

        `None` is loopback root only. Everything else — every paired device,
        every companion, every third-party experience — is filtered, and a
        caller with no grant gets presence and nothing else. Before this, a
        credential holding `presence:read` received the full room census
        whatever its manifest claimed to need.
        """
        if getattr(request.state, "role", None) == "root":
            return None
        token = parse_bearer(authorization)
        device = devices.verify(token) if (devices is not None and token) else None
        return grants.scopes_for(device.device_id if device else None,
                                 experience_id)

    def _pieces():
        return {
            "space": space_fn(),
            "coverage_rows": coverage_fn(),
            "anchors": anchors_fn(),
            "devices": devices_fn(),
            "profile_fn": profile_fn,
        }

    @router.get("/api/experience/context")
    async def space_context(request: Request, experience: str = "",
                            authorization: str | None = Header(default=None),
                            _=Depends(require_scope("presence:read"))):
        """Every room at once — the first call an application makes.

        It connects, asks what this Space looks like, then subscribes to the
        room it cares about.
        """
        rooms = rooms_fn()
        scopes = _scopes(request, authorization, experience)
        body = build_space_context(
            rooms=rooms,
            states={r: room_state_fn(r) for r in rooms},
            protocol_version=EXPERIENCE_PROTOCOL_VERSION, **_pieces())
        body["rooms"] = [redact(r, scopes) for r in body["rooms"]]
        return body

    @router.get("/api/experience/context/{room}")
    async def room_context(request: Request, room: str, experience: str = "",
                           authorization: str | None = Header(default=None),
                           _=Depends(require_scope("presence:read"))):
        if room not in set(rooms_fn()):
            # 404 rather than an empty context: an empty one reads as "this room
            # has no sensors", and an application would go looking for hardware
            # to fix instead of correcting a typo.
            raise HTTPException(status_code=404, detail=f"no room named {room!r}")
        return redact(
            build_context(room=room, room_state=room_state_fn(room),
                          protocol_version=EXPERIENCE_PROTOCOL_VERSION,
                          **_pieces()).to_dict(),
            _scopes(request, authorization, experience))

    @router.post("/api/experience/compatibility")
    async def compatibility(manifest: dict = Body(...), room: str = Body(""),
                            _=Depends(require_scope("presence:read"))):
        """Whether this Space can support an experience.

        A POST because the manifest is the input, not because anything changes:
        nothing is stored, nothing is installed, and calling this twice is
        indistinguishable from calling it once. Wavr has no notion of an
        installed experience and this route does not create one.
        """
        try:
            spec = parse(manifest)
        except ManifestError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        warnings = validate(spec)
        rooms = rooms_fn()
        if room:
            if room not in set(rooms):
                raise HTTPException(status_code=404,
                                    detail=f"no room named {room!r}")
            verdict = evaluate(spec, build_context(
                room=room, room_state=room_state_fn(room),
                protocol_version=EXPERIENCE_PROTOCOL_VERSION, **_pieces()))
            return {"experience": spec.to_dict(), **verdict.to_dict(),
                    "warnings": warnings}
        space = build_space_context(
            rooms=rooms, states={r: room_state_fn(r) for r in rooms},
            protocol_version=EXPERIENCE_PROTOCOL_VERSION, **_pieces())
        return {**evaluate_space(spec, space), "warnings": warnings}

    # -- Sessions ----------------------------------------------------------
    #
    # `presence:read`, like the rest of this file. A session is a name plus a
    # subscription; it grants nothing, so it needs no more than the context it
    # reports on.

    @router.post("/api/experience/sessions")
    async def open_session(experience_id: str = Body(...), room: str = Body(""),
                           metadata: dict = Body(None),
                           _=Depends(require_scope("presence:read"))):
        try:
            session = sessions.open(experience_id, room=room,
                                    metadata=metadata)
        except SessionError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return session.to_dict()

    @router.get("/api/experience/sessions/{session_id}")
    async def read_session(session_id: str,
                           _=Depends(require_scope("presence:read"))):
        session = sessions.touch(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="no such session")
        return session.to_dict()

    @router.post("/api/experience/sessions/{session_id}/observe")
    async def observe_session(session_id: str, room: str = Body("", embed=True),
                              _=Depends(require_scope("presence:read"))):
        """Re-evaluate a session and get back what changed.

        The handoff primitive. It reports that a display became available; it
        does not decide the experience should move there, because that depends
        on what the experience IS — a recipe follows you to a screen, a private
        message does not.
        """
        session = sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="no such session")
        try:
            events = sessions.observe(session_id, room=room or session.room,
                                      devices=devices_fn())
        except SessionError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {**sessions.get(session_id).to_dict(), "events": events}

    @router.post("/api/experience/sessions/{session_id}/devices")
    async def join_session(session_id: str, device_id: str = Body(..., embed=True),
                           _=Depends(require_scope("presence:read"))):
        try:
            return sessions.join(session_id, device_id).to_dict()
        except SessionError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @router.delete("/api/experience/sessions/{session_id}")
    async def close_session(session_id: str,
                            _=Depends(require_scope("presence:read"))):
        if not sessions.close(session_id):
            raise HTTPException(status_code=404, detail="no such session")
        return {"session_id": session_id, "closed": True}

    # -- Grants ------------------------------------------------------------
    #
    # Local + admin, and nothing below it. Deciding what an experience may read
    # about a household is the same class of act as configuring the Space, and
    # an experience asking for a scope must never be anywhere near the thing
    # that awards one — that is the whole "a manifest is a request, never a
    # grant" line, and it is only true because these two live apart.

    @router.get("/api/experience/grants")
    async def list_grants(device_id: str = "", _=Depends(require_local),
                          __=Depends(require_scope("admin"))):
        return {
            "grants": grants.list(device_id or None),
            "default": sorted(DEFAULT_SCOPES),
            "note": ("A device with no grant gets presence and nothing else — "
                     "enough for an application to be worth running while it "
                     "waits to be trusted."),
        }

    @router.put("/api/experience/grants/{device_id}/{experience_id}")
    async def set_grant(device_id: str, experience_id: str,
                        scopes: list = Body(..., embed=True),
                        _=Depends(require_local),
                        __=Depends(require_scope("admin"))):
        try:
            return grants.grant(device_id, experience_id, scopes)
        except GrantError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.delete("/api/experience/grants/{device_id}/{experience_id}")
    async def revoke_grant(device_id: str, experience_id: str,
                           _=Depends(require_local),
                           __=Depends(require_scope("admin"))):
        if not grants.revoke(device_id, experience_id):
            raise HTTPException(status_code=404, detail="no such grant")
        return {"device_id": device_id, "experience_id": experience_id,
                "revoked": True}

    @router.get("/api/experience/scopes")
    async def scopes(_=Depends(require_scope("presence:read"))):
        """The spatial permissions an experience may ask for.

        Published so a developer writing a manifest can see the whole vocabulary
        rather than discovering it one rejection at a time — and so the absence
        of an identity scope is visible rather than merely true.
        """
        return {"scopes": sorted(SPATIAL_SCOPES),
                "identity": NO_IDENTITY_SCOPE,
                "note": ("A manifest asking for a scope does not receive it. "
                         "These are what an experience may REQUEST; a person "
                         "decides what it gets."),
                # Said to the developer who would otherwise assume it. The
                # device half of a grant is authenticated by the bearer token;
                # the experience half is a query parameter this Core takes on
                # trust, because no per-experience credential exists yet.
                "experience_id_is_self_asserted": True,
                "limit": ("Grants are per (device, experience), but only the "
                          "DEVICE half is authenticated. The experience id is "
                          "sent by the caller and not verified, so two "
                          "applications sharing one device's token share that "
                          "device's most-privileged grant. This axis keeps an "
                          "operator's grants legible; it is not a security "
                          "boundary between applications on the same device.")}

    return router
