"""FastAPI routers for the Space model: first-run setup, people, device
functions, Core topology, operator settings and the Discovery Inbox.

Four routers with three different auth boundaries, following the split pattern
`api_peers.py` and `api_nodes.py` already established:

  * `build_setup_router`      — FIRST RUN. Loopback-root only. This is where a
    Space is created, so it is the most privileged surface in the file; it is
    also the only one that must work *before* a Space exists.
  * `build_space_router`      — administration. Loopback-root or an authed
    `central` with the `admin` scope, wired by app.py.
  * `build_settings_router`   — the knobs that replace `.env`. Same gate as
    administration, plus a per-key consent requirement enforced in the store.
  * `build_discovery_router`  — the inbox. Same gate as administration.

FAIL-CLOSED: every router takes its dependencies from app.py and substitutes a
403-raising stub when they are omitted, exactly like `_admin_deps_not_wired` in
`api_nodes.py`. Forgetting to wire a gate must never open a route.
"""
from __future__ import annotations

import sqlite3

from fastapi import (APIRouter, Body, Depends, Header, HTTPException, Query,
                     Request)

from wavr.auth import parse_bearer

from wavr.capabilities import (
    DEVICE_FUNCTIONS, ROLE_CORE, CapabilityManifest, recommend, scan_host)
from wavr.core_registry import CoreRegistryError, STATUS_PRIMARY
from contextlib import suppress

from wavr.discovery_inbox import (
    DiscoveryError, KIND_CAMERA_FOUND, STATUS_ACCEPTED, STATUS_DISMISSED)
from wavr.settings_store import SPECS_BY_KEY, SettingsError
from wavr.space_store import (
    DEFAULT_SPACE_KIND, PERSON_ROLES, ROLE_GUEST, ROLE_OWNER, SPACE_KINDS,
    SpaceError, device_role_for_person)


def _deps_not_wired():
    """Fail-closed default. If app.py forgets to pass a gate, the route answers
    403 rather than running unguarded."""
    def _dep():
        raise HTTPException(
            status_code=403, detail="space routes have no auth gate wired")
    return [Depends(_dep)]


def _bad(exc: Exception, status: int = 400):
    return HTTPException(status_code=status, detail=str(exc))


# -- First run ---------------------------------------------------------------

def build_setup_router(space_store, settings, cores, *, devices=None,
                       instance_name: str = "Wavr", port: int = 8000,
                       local_ip: str = "127.0.0.1", cert_fingerprint: str = "",
                       browse_peers=None, scheme: str = "http",
                       hostname: str = "wavr.local", seed_room=None,
                       deps=None) -> APIRouter:
    """The first-run flow. Every route here is loopback-root-gated by app.py.

    `browse_peers` is injected (defaults to a no-op) so the nearby-Space lookup
    is testable without zeroconf and cannot 500 a fresh install that lacks the
    `[mdns]` extra.

    `seed_room(name) -> bool` puts the room the operator names onto an empty
    floor plan. Injected for the same reason: this router owns no filesystem
    path, and a setup flow that writes files directly cannot be tested without
    one.

    `scheme` and `cert_fingerprint` each take a plain value OR a zero-arg
    provider. app.py passes providers, because both answers depend on the
    connection a request arrived on and this router is built at startup, before
    any connection exists. Passing `cert_fingerprint()`'s value here once is
    exactly how a Core that generated a certificate during a past multidevice
    run went on serving that fingerprint forever over plain HTTP -- to a screen
    that asks the operator to compare it against their phone's certificate
    warning, which a plain-HTTP page never shows."""
    router = APIRouter(dependencies=deps if deps is not None else _deps_not_wired())

    def _scheme() -> str:
        return scheme() if callable(scheme) else scheme

    def _cert_fp() -> str:
        return (cert_fingerprint() if callable(cert_fingerprint)
                else cert_fingerprint) or ""

    def _port() -> int:
        """The port, resolved per request for the same reason as the host: a
        launcher can state it on the command line where the configuration
        never sees it."""
        return (port() if callable(port) else port) or 8000

    def _host() -> str:
        """The address to hand another device, resolved per request.

        A provider for the same reason as `_scheme`: whether the LAN address is
        the truth depends on the interface the launcher bound, which is not
        known when this router is built. A plain string still works, which is
        what tests pass.
        """
        return (local_ip() if callable(local_ip) else local_ip) or "127.0.0.1"

    @router.get("/api/setup/status")
    async def setup_status():
        """Is this Core set up, and what is it? The one call the first-run UI
        makes before deciding whether to show a wizard or the dashboard."""
        space = space_store.get_space()
        me = cores.self_core()
        # An install that predates the Space model: it has paired devices but no
        # Space. The wizard must greet that operator differently -- "Wavr is
        # already running here, let's name it" rather than "welcome" -- and it
        # must NOT invent a name for the home they already had.
        legacy = 0
        if space is None and devices is not None:
            # `DeviceStore.list()`, NOT `list_devices()`. Narrow the guard to a
            # real storage fault: a bare `except Exception` here previously
            # turned a wrong method name into "you have no devices", which
            # silently hid the adopt path from every existing install.
            try:
                legacy = len(devices.list())
            except sqlite3.Error:
                legacy = 0
        return {
            "needs_setup": space is None,
            "existing_devices": legacy,
            "space": space.to_dict() if space else None,
            "this_core": me.to_dict() if me else None,
            "people": len(space_store.list_people()) if space else 0,
            "instance_name": instance_name,
            # The scheme is read off the connection rather than assumed. It is
            # not enough to read `WAVR_MULTIDEVICE`: that flag says TLS was
            # asked for, and only `serve.py` acts on it -- the Dockerfile and
            # scripts/wavr.ps1 launch uvicorn directly and serve plain HTTP with
            # the flag set. Printing an https:// URL that cannot connect is a
            # small lie that costs a first-time user ten confused minutes.
            "lan_url": f"{_scheme()}://{_host()}:{_port()}",
            # The name-based route. Resolvable wherever mDNS is (iOS, macOS, most
            # modern Androids, Linux with Avahi) and the reason a phone rarely
            # needs the numeric address. Offered as a convenience, never as the
            # only way in -- the QR on the pairing screen is the path that always
            # works and carries the certificate fingerprint with it.
            "lan_hostname_url": f"{_scheme()}://{hostname}:{_port()}",
            "cert_fingerprint": _cert_fp(),
        }

    @router.post("/api/setup/scan")
    async def capability_scan():
        """Probe this machine and propose what it should become.

        Read-only and egress-free by construction (see `capabilities.scan_host`)
        — it opens no camera, joins no network and sends nothing anywhere, so it
        is safe to run before the operator has agreed to anything."""
        manifest = scan_host()
        space = space_store.get_space()
        has_core = bool(space and any(
            c.status == STATUS_PRIMARY for c in cores.list_cores()))
        rec = recommend(manifest, space_has_core=has_core)
        return {"manifest": manifest.to_dict(), "recommendation": rec.to_dict()}

    @router.get("/api/setup/nearby")
    async def nearby_spaces():
        """Wavr Cores advertising themselves on this network.

        Returns the OPAQUE space id and the Core's own name — never a Space's
        human name. A Space is named by its owner for their own benefit; putting
        "Alex's Home" into an mDNS broadcast every device on the LAN can read
        is a disclosure nobody asked for. The name arrives after pairing."""
        if browse_peers is None:
            return {"cores": [], "available": False}
        try:
            found = browse_peers()
        except Exception:      # noqa: BLE001 — a missing extra must not 500
            return {"cores": [], "available": False}
        return {"available": True, "cores": [
            {"name": getattr(p, "name", ""), "host": getattr(p, "host", ""),
             "port": getattr(p, "port", 0), "role": getattr(p, "role", ""),
             "space_id": getattr(p, "space_id", "")}
            for p in (found or [])]}

    @router.post("/api/setup/create-space")
    async def create_space(name: str = Body(...), kind: str = Body("home"),
                           owner_name: str = Body("Owner"),
                           functions: list[str] = Body(None),
                           room: str = Body("")):
        """Create the Space this Core serves, mint its Owner, and register this
        machine as its first Core.

        One call rather than four, because these four facts are meaningless
        apart: a Space with no Owner cannot be administered, and an Owner with
        no Space has nothing to own. If any step fails the whole thing fails —
        there is no half-created Space to clean up."""
        try:
            space = space_store.create_space(name, kind)
            owner = space_store.add_person(owner_name, ROLE_OWNER)
        except SpaceError as exc:
            raise _bad(exc) from None

        # From here on, ANY failure must undo the Space. Without this, a single
        # "database is locked" mid-way left a Space with an Owner and no Core:
        # `needs_setup` flips to false so the wizard never returns, every retry
        # 400s with "already belongs to a Space", and there is no reset route.
        # The only recovery was deleting wavr.db by hand.
        try:
            manifest = scan_host()
            wanted = frozenset(functions or manifest.functions_supported)
            unknown = wanted - DEVICE_FUNCTIONS
            if unknown:
                raise HTTPException(
                    status_code=422,
                    detail=f"unknown device functions: {sorted(unknown)}")

            # The Core's own id is derived from the Space id, so a machine that
            # is re-registered keeps its identity across restarts without
            # persisting a separate uuid file that could go missing.
            core_id = f"core-{space.space_id[:16]}"
            battery = manifest.capability("battery")
            core = cores.register(
                core_id, space.space_id, instance_name,
                base_url=f"{_scheme()}://{_host()}:{_port()}",
                cert_fingerprint=_cert_fp(), platform=manifest.platform,
                # A battery-powered Core is portable until told otherwise (SS31).
                portable=bool(battery), room=room, is_self=True,
                health={"compute_tier": manifest.compute_tier})
            if ROLE_CORE in wanted:
                core = cores.promote(core_id, space_store.bump_epoch())
        except HTTPException:
            space_store.destroy_space()
            raise
        except Exception as exc:      # noqa: BLE001 -- roll back, then re-raise
            space_store.destroy_space()
            raise _bad(exc, 500) from None

        # The room the operator just named becomes the first room on the floor
        # plan. It was already being recorded against the Core; the map ignored
        # it and showed `DEFAULT_MAP` instead, which until now was a fictional
        # three-room house. AFTER the rollback block on purpose: a plan that
        # cannot be written is a map to draw later, not a reason to refuse to
        # create the Space.
        seeded = bool(seed_room and room and seed_room(room))

        return {"space": space.to_dict(), "owner": owner.to_dict(),
                "core": core.to_dict(), "manifest": manifest.to_dict(),
                "functions": sorted(wanted), "room_seeded": seeded}

    @router.post("/api/setup/join-space")
    async def join_space(space_id: str = Body(...), name: str = Body(...),
                         owner_name: str = Body("Owner"),
                         kind: str = Body(DEFAULT_SPACE_KIND),
                         functions: list[str] = Body(None)):
        """Adopt an EXISTING Space's identity on this Core.

        This is the second-Core path. It records the shared `space_id` and
        registers this machine as a **standby** — never a primary. Promotion is
        always a separate, explicit act (`POST /api/space/cores/{id}/promote`),
        so joining a Space can never take it over.

        Note what this does NOT do: it does not synchronise state from the other
        Core. Cross-Core state replication is not implemented, and a standby
        that has never synced is honestly reported as such rather than being
        presented as a warm spare it is not.

        `kind` is asked for, not assumed. It was hardcoded `"home"`, so a
        clinic's or an office's second Core recorded a DIFFERENT kind for the
        SAME Space, and the answer to "what is this place" then depended on
        which Core the phone happened to be paired to. The joiner is already
        being asked for the name for exactly this reason — the Core cannot
        discover it, because joining does not synchronise state — and the kind
        is the same class of fact.
        """
        try:
            space = space_store.create_space(name, kind, space_id=space_id)
            space_store.add_person(owner_name, ROLE_OWNER)
        except SpaceError as exc:
            raise _bad(exc) from None
        manifest = scan_host()
        core = cores.register(
            f"core-{space.space_id[:8]}-{instance_name[:8].lower()}",
            space.space_id, instance_name,
            base_url=f"{_scheme()}://{_host()}:{_port()}",
            cert_fingerprint=_cert_fp(), platform=manifest.platform,
            portable=bool(manifest.capability("battery")), is_self=True,
            health={"compute_tier": manifest.compute_tier, "synced": False})
        return {"space": space.to_dict(), "core": core.to_dict(),
                "status": "standby", "synced": False,
                "note": "This Core joined as a standby. It does not yet mirror "
                        "the primary Core's state."}

    @router.post("/api/setup/adopt")
    async def adopt_existing(name: str = Body(...), kind: str = Body("home"),
                             owner_name: str = Body("Owner")):
        """Give a PRE-EXISTING install a Space without disturbing it.

        Everything that already works keeps working: no credential is reissued,
        no device role changes, no token is invalidated. Devices that were
        paired as `central` are associated with the new Owner (they are, by
        construction, boxes the operator personally paired from the loopback
        screen); every other device is left unassociated, because we genuinely
        do not know whose it is and guessing would be exactly the mistake SS36
        exists to prevent."""
        if space_store.get_space() is not None:
            raise HTTPException(status_code=400,
                                detail="this Core already belongs to a Space")
        rows = []
        if devices is not None:
            try:
                rows = devices.list()
            except sqlite3.Error:
                rows = []
        try:
            space = space_store.adopt_legacy(rows, owner_name=owner_name,
                                             space_name=name)
            if kind and kind != space.kind:
                space = space_store.rename_space(space.name, kind)
        except SpaceError as exc:
            raise _bad(exc) from None

        # Complete the link that actually enforces. `adopt_legacy` writes the
        # Space's own record; `devices.person_id` is what `auth._apply_person_cap`
        # reads, and without it an adopted install starts with the cap inert on
        # every `central` device it just claimed for the Owner.
        if devices is not None:
            owner = next((p for p in space_store.list_people()
                          if p.role == ROLE_OWNER), None)
            if owner is not None:
                for did in space_store.devices_of(owner.person_id):
                    with suppress(Exception):
                        devices.set_person(did, owner.person_id)

        # Same rollback contract as create-space above: a half-adopted install
        # is worse than an un-adopted one, because the un-adopted one can retry.
        try:
            manifest = scan_host()
            core_id = f"core-{space.space_id[:16]}"
            cores.register(core_id, space.space_id, instance_name,
                           base_url=f"{_scheme()}://{_host()}:{_port()}",
                           cert_fingerprint=_cert_fp(),
                           platform=manifest.platform,
                           portable=bool(manifest.capability("battery")),
                           is_self=True,
                           health={"compute_tier": manifest.compute_tier})
            core = cores.promote(core_id, space_store.bump_epoch())
        except Exception as exc:      # noqa: BLE001 -- roll back, then re-raise
            space_store.destroy_space()
            raise _bad(exc, 500) from None
        people = space_store.list_people()
        return {"space": space.to_dict(), "core": core.to_dict(),
                "owner": people[0].to_dict() if people else None,
                "adopted_devices": len(rows)}

    return router


# -- Administration ----------------------------------------------------------

def build_self_manifest_router(space_store, devices, deps=None) -> APIRouter:
    """`PUT /api/devices/me/manifest` — a device describes ITSELF.

    The id is resolved from the bearer token, never taken from the path, which
    is what makes this safe to expose to an ordinary paired companion: it can
    write exactly one row, its own. The admin route
    (`/api/space/devices/{id}/manifest`) stays for correcting or clearing an
    entry on someone else's behalf.

    A manifest is EVIDENCE, not authority. It informs what Wavr suggests a device
    could be — see `capabilities.recommend` — and confers nothing. That is why a
    self-write is acceptable at all.
    """
    router = APIRouter(dependencies=deps if deps is not None else _deps_not_wired())

    @router.put("/api/devices/me/manifest")
    async def set_own_manifest(authorization: str | None = Header(default=None),
                               manifest: dict = Body(...)):
        # Resolved from the token exactly as `app._self_device` does, and never
        # from a body or query field: "a device can only ever act on itself" is
        # the rule those routes already established, and this one is the reason
        # it can be exposed below admin.
        token = parse_bearer(authorization)
        device = devices.verify(token) if (devices is not None and token) else None
        if device is None:
            # Loopback root is not a paired device and has no row to describe.
            # The Core's own capabilities come from `scan_host()` at setup.
            raise HTTPException(
                status_code=400,
                detail="only a paired device can describe itself")
        device_id = device.device_id
        try:
            space_store.set_manifest(device_id, manifest)
        except ValueError as exc:
            raise _bad(exc, 422) from None
        parsed = space_store.get_manifest(device_id)
        return {"device_id": device_id, "manifest": parsed.to_dict(),
                "recommendation": recommend(parsed).to_dict()}

    return router


def build_space_router(space_store, cores, *, devices=None,
                       deps=None, owner_deps=None) -> APIRouter:
    """`deps` gates ordinary administration (an Admin's paired device may pass).
    `owner_deps` gates the ONE act reserved to the Owner — see `transfer` below.
    Both fail closed when omitted."""
    router = APIRouter(dependencies=deps if deps is not None else _deps_not_wired())
    _owner = owner_deps if owner_deps is not None else _deps_not_wired()

    # -- Space ---------------------------------------------------------------

    @router.get("/api/space")
    async def get_space():
        space = space_store.get_space()
        if space is None:
            raise HTTPException(status_code=404, detail="this Core has no Space yet")
        return {**space.to_dict(),
                "people": [p.to_dict() for p in space_store.list_people()],
                "topology": cores.topology(),
                "kinds": list(SPACE_KINDS)}

    @router.put("/api/space")
    async def update_space(name: str = Body(...), kind: str = Body(None)):
        try:
            return space_store.rename_space(name, kind).to_dict()
        except SpaceError as exc:
            raise _bad(exc) from None

    @router.put("/api/space/policy")
    async def update_policy(policy: dict = Body(..., embed=True)):
        """Per-Space policy, which the API has been advertising and nothing
        could fill.

        `GET /api/space` returned `"policy": {}` on every install forever,
        because `SpaceStore.set_policy` existed with its own bounded-JSON
        validation and had no route and no caller. An integrator reading the
        API took it for a real configuration surface and built against a field
        the product could not populate — the sort of claim this codebase
        removes rather than leaves standing.

        Two ways to make it true: expose it, or drop the column, the method and
        the key. Exposing it is the smaller change and the honest one — the
        storage, the size cap and the round-trip were already written and
        tested; only the door was missing.
        """
        try:
            return space_store.set_policy(policy).to_dict()
        except SpaceError as exc:
            raise _bad(exc) from None

    # -- People --------------------------------------------------------------

    @router.get("/api/space/people")
    async def list_people():
        return {"people": [p.to_dict() for p in space_store.list_people()],
                "roles": sorted(PERSON_ROLES)}

    @router.post("/api/space/people")
    async def add_person(display_name: str = Body(...),
                         role: str = Body("user"),
                         expires_at: str = Body(None)):
        try:
            person = space_store.add_person(display_name, role, expires_at=expires_at)
        except SpaceError as exc:
            raise _bad(exc, 422) from None
        return {**person.to_dict(),
                # The credential role a device of theirs will receive. Surfaced
                # so the admin sees the consequence before pairing, not after.
                "device_role": device_role_for_person(person.role)}

    @router.post("/api/space/people/{person_id}/role")
    async def set_role(person_id: str, role: str = Body(..., embed=True)):
        try:
            person = space_store.set_person_role(person_id, role)
        except SpaceError as exc:
            raise _bad(exc, 422) from None
        # Honest disclosure: a live token keeps the reach it was issued with
        # until it is revoked. Saying so is the difference between an admin who
        # knows to revoke and one who thinks a demotion took effect.
        existing = space_store.devices_of(person_id)
        return {**person.to_dict(), "devices_needing_repair": existing,
                "note": ("Existing devices keep the access they were paired with "
                         "until you revoke them.") if existing else ""}

    @router.post("/api/space/people/{person_id}/profile")
    async def set_profile(person_id: str, profile: dict = Body(...)):
        """Personalization only — never authorization (§29)."""
        try:
            return space_store.set_person_profile(person_id, profile).to_dict()
        except SpaceError as exc:
            raise _bad(exc, 422) from None

    @router.delete("/api/space/people/{person_id}")
    async def remove_person(person_id: str):
        """Remove a person AND revoke every credential that was theirs. The two
        are one operation on purpose: removing the human while leaving their
        phone able to reach the API is the obvious footgun."""
        try:
            device_ids = space_store.remove_person(person_id)
        except SpaceError as exc:
            raise _bad(exc, 422) from None
        revoked = []
        for did in device_ids:
            if devices is not None and devices.revoke(did):
                revoked.append(did)
            space_store.forget_device(did)
        return {"removed": person_id, "revoked_devices": revoked}

    @router.post("/api/space/transfer", dependencies=_owner)
    async def transfer(to_person_id: str = Body(..., embed=True)):
        """Hand the Space to someone else. LOOPBACK-ROOT ONLY.

        `space_store` already reserves this to the Owner *role*, but the HTTP
        layer cannot tell WHICH PERSON is behind a credential — `request.state`
        carries a role and scopes, never a person id. Under the ordinary admin
        gate, any device holding the `admin` scope could therefore give the
        Space away and the store's Owner-only rule would never be consulted.

        Until requests are bound to a person, the substitute is physical
        access: this must be done at the Core itself. Deliberately stricter
        than every other route in this router."""
        try:
            new_owner, prev = space_store.transfer_ownership(to_person_id)
        except SpaceError as exc:
            raise _bad(exc, 422) from None
        return {"owner": new_owner.to_dict(), "previous_owner": prev.to_dict()}

    # -- Device functions ----------------------------------------------------

    @router.get("/api/space/devices")
    async def list_devices():
        """Devices with all three axes resolved side by side: the credential
        role, the job, and the person. Assembling them here rather than making
        the UI join three endpoints is what makes the separation legible."""
        funcs = {f.device_id: f.to_dict() for f in space_store.list_functions()}
        out = []
        rows = devices.list() if devices is not None else []
        for dev in rows:
            d = dev.to_dict() if hasattr(dev, "to_dict") else dict(dev)
            did = d.get("device_id")
            assoc = space_store.person_of_device(did) if did else None
            manifest = space_store.get_manifest(did) if did else None
            out.append({
                **d,
                "functions": funcs.get(did, {}).get("functions", []),
                "room": funcs.get(did, {}).get("room", ""),
                "portable": funcs.get(did, {}).get("portable", False),
                # Returned so an editor can send it BACK. `set_functions`
                # replaces the whole row, so a UI that never sees `platform`
                # blanks it on every save -- losing what the device reported
                # through its manifest.
                "platform": funcs.get(did, {}).get("platform", ""),
                "person_id": assoc[0] if assoc else None,
                "person_origin": assoc[1] if assoc else None,
                "capabilities": manifest.to_dict() if manifest else None,
            })
        return {"devices": out, "functions_available": sorted(DEVICE_FUNCTIONS)}

    @router.put("/api/space/devices/{device_id}/functions")
    async def set_functions(device_id: str, functions: list[str] = Body(...),
                            room: str = Body(""), portable: bool = Body(False),
                            platform: str = Body(None)):
        # `platform` omitted means "leave it alone", not "blank it". The store
        # replaces the whole row, so without this an editor that only knows
        # about functions/room/portable silently erases what the device itself
        # reported. Pass an explicit "" to actually clear it.
        if platform is None:
            existing = space_store.get_functions(device_id)
            platform = existing.platform if existing else ""
        try:
            return space_store.set_functions(
                device_id, functions, platform=platform, room=room,
                portable=portable).to_dict()
        except SpaceError as exc:
            raise _bad(exc, 422) from None

    @router.put("/api/space/devices/{device_id}/manifest")
    async def set_manifest(device_id: str, manifest: dict = Body(...)):
        """Accept a device's self-reported Capability Manifest.

        Self-reported and treated as such: a manifest informs recommendations
        and the UI, and grants nothing. A device claiming `"core": true` gains
        no authority from saying so — authority comes from `promote`, which an
        admin performs."""
        try:
            space_store.set_manifest(device_id, manifest)
        except ValueError as exc:
            raise _bad(exc, 422) from None
        parsed = space_store.get_manifest(device_id)
        return {"device_id": device_id, "manifest": parsed.to_dict(),
                "recommendation": recommend(parsed).to_dict()}

    @router.post("/api/space/devices/{device_id}/person")
    async def associate(device_id: str, person_id: str = Body(None, embed=True)):
        """Bind (or unbind, with a null person_id) a device to a person.

        Always recorded as `confirmed` — this route is reached by an
        administrator clicking a name. Inference never comes through here; it
        arrives as a Discovery the admin has to accept (§36).

        TWO records, on purpose, and both are required. `space_store` holds the
        Space's own view (what the Devices screen lists); `devices.person_id` is
        the column `auth._apply_person_cap` reads when it decides what a request
        may actually do. Writing only the first — which this route used to do —
        left every UI-linked device with a NULL enforcement column, so the person
        cap never fired and a demotion changed nothing. The Space said Sam owned
        that phone and authorization had never heard of her.
        """
        if person_id is None:
            out = {"device_id": device_id,
                   "disassociated": space_store.disassociate_device(device_id)}
            if devices is not None:
                # Clear the enforcement column too, or the device keeps being
                # capped by a person the Space no longer says owns it.
                devices.set_person(device_id, None)
            return out
        try:
            space_store.associate_device(device_id, person_id, origin="confirmed")
        except SpaceError as exc:
            raise _bad(exc, 422) from None
        if devices is not None:
            try:
                devices.set_person(device_id, person_id)
            except Exception as exc:      # noqa: BLE001
                # Undo rather than half-succeed. An association the Space reports
                # but authorization does not honour is worse than no association:
                # the screen would show the device as capped when it is not.
                with suppress(Exception):
                    space_store.disassociate_device(device_id)
                raise HTTPException(
                    status_code=500,
                    detail="could not link that device — nothing was changed",
                ) from exc
        return {"device_id": device_id, "person_id": person_id, "origin": "confirmed"}

    # -- Cores ---------------------------------------------------------------

    @router.get("/api/space/cores")
    async def list_cores():
        return cores.topology()

    @router.post("/api/space/cores/{core_id}/promote")
    async def promote(core_id: str):
        """Make a Core authoritative. Bumps the Space epoch first, so the new
        primary is fenced against every earlier decision."""
        try:
            core = cores.promote(core_id, space_store.bump_epoch())
        except (CoreRegistryError, SpaceError) as exc:
            raise _bad(exc, 422) from None
        return {"promoted": core.to_dict(), "topology": cores.topology()}

    @router.post("/api/space/cores/{core_id}/demote")
    async def demote(core_id: str):
        try:
            core = cores.demote(core_id)
        except CoreRegistryError as exc:
            raise _bad(exc, 422) from None
        return {"demoted": core.to_dict(), "topology": cores.topology()}

    @router.post("/api/space/cores/{core_id}/room")
    async def set_core_room(core_id: str, room: str = Body(..., embed=True)):
        """Re-home a portable Core. Exists because §31's alternative — silently
        continuing to attribute a moved Core's sensors to its old room — would
        make the map quietly wrong."""
        core = cores.get(core_id)
        if core is None:
            raise HTTPException(status_code=404, detail="unknown core")
        return cores.register(
            core.core_id, core.space_id, core.name, base_url=core.base_url,
            cert_fingerprint=core.cert_fingerprint, platform=core.platform,
            portable=core.portable, room=" ".join(str(room).split())[:64],
            is_self=core.is_self, health=core.health).to_dict()

    @router.delete("/api/space/cores/{core_id}")
    async def forget_core(core_id: str):
        try:
            gone = cores.forget(core_id)
        except CoreRegistryError as exc:
            raise _bad(exc, 422) from None
        if not gone:
            raise HTTPException(status_code=404, detail="unknown core")
        return {"forgotten": core_id, "topology": cores.topology()}

    return router


# -- Settings ----------------------------------------------------------------

def build_settings_router(settings, deps=None, is_local_fn=None) -> APIRouter:
    """`is_local_fn(request) -> bool` — is the caller at the Core itself?

    Four settings take Wavr OFF this machine (`local_only` in `SETTING_SPECS`):
    LAN access, the bind address, node intake and peer intake. Scope alone is the
    wrong fence for those, because `central` holds `admin` by default and every
    central device is on the very network the change would expose. Same
    reasoning `require_root` already carries for ARP blocking.

    Reading stays open to any admin — it is the WRITE that widens.
    """
    router = APIRouter(dependencies=deps if deps is not None else _deps_not_wired())

    @router.get("/api/settings")
    async def list_settings():
        """Every knob with its value, provenance and whether the environment is
        overriding it. `locked: true` means an operator's `.env` or the
        container environment wins and this UI cannot change it — shown rather
        than hidden, so a click that will not take effect is visibly disabled
        instead of silently ignored."""
        return {"settings": settings.describe(),
                "restart_required_note":
                    "Most settings take effect the next time Wavr starts."}

    def _refuse_remote(request, key: str) -> None:
        """A `local_only` key may only be written from the Core's own screen.

        Fails closed on purpose: with no `is_local_fn` wired there is no way to
        establish that the caller is local, so the answer is no. A router
        assembled without its gate must not be the permissive one.
        """
        spec = SPECS_BY_KEY.get(key)
        if spec is None or not spec.local_only:
            return
        if is_local_fn is not None and is_local_fn(request):
            return
        raise HTTPException(
            status_code=403,
            detail=("This switch can only be changed on the machine running "
                    "Wavr. It is the one that decides who else can reach it."))

    @router.put("/api/settings/{key}")
    async def set_setting(request: Request, key: str, value=Body(..., embed=True),
                          consent: str = Body(None, embed=True)):
        _refuse_remote(request, key)
        try:
            stored = settings.set(key, value, consent=consent)
        except SettingsError as exc:
            raise _bad(exc, 422) from None
        return {**settings.effective(key), "stored": stored}

    @router.delete("/api/settings/{key}")
    async def unset_setting(request: Request, key: str):
        # Clearing a stored value changes what takes effect at the next start,
        # so it is a write and carries the same fence.
        _refuse_remote(request, key)
        try:
            removed = settings.unset(key)
            return {**settings.effective(key), "removed": removed}
        except SettingsError as exc:
            raise _bad(exc, 404) from None

    return router


# -- Discovery Inbox ---------------------------------------------------------

def rtsp_url_with_credentials(masked: str, username: str, password: str) -> str:
    """Rebuild a usable RTSP URL from the probe's masked one.

    `sources.onvif._mask_rtsp` only redacts the PASSWORD -- scheme, host and path
    survive intact -- so the real URL is recoverable by substituting the
    credentials the operator just supplied. Reconstructing from the masked string
    rather than having the probe hand back a live credential means the plaintext
    exists only inside this one call.

    Returns "" for anything that is not a plausible rtsp URL, so a malformed
    probe result can never be stored as a camera."""
    # Match what `sources.onvif._rtsp_ok` already accepted upstream: both
    # schemes, case-insensitively. Being stricter here does not add safety (the
    # host was validated as a LAN literal before masking) -- it just rejects a
    # legitimate camera that answered with RTSP-TLS or an uppercase scheme.
    lower = masked.lower()
    for scheme in ("rtsp://", "rtsps://"):
        if lower.startswith(scheme):
            prefix, rest = masked[:len(scheme)], masked[len(scheme):]
            break
    else:
        return ""
    # Drop any existing userinfo: everything after the LAST "@" is the authority,
    # matching what the masker itself does.
    _creds, at, host_and_path = rest.rpartition("@")
    tail = host_and_path if at else rest
    if not tail or "/" not in tail and ":" not in tail:
        return ""
    if not username:
        return f"{prefix}{tail}"
    from urllib.parse import quote

    return (f"{prefix}{quote(username, safe='')}:"
            f"{quote(password or '', safe='')}@{tail}")


def build_discovery_router(inbox, deps=None, *, cameras=None, onvif_probe=None,
                           onvif_enabled=False) -> APIRouter:
    router = APIRouter(dependencies=deps if deps is not None else _deps_not_wired())

    @router.get("/api/discoveries")
    async def list_discoveries(status: str = Query("pending"),
                               kind: str = Query(None),
                               limit: int = Query(200, ge=1, le=1000)):
        try:
            items = inbox.list_items(
                status=None if status == "all" else status, kind=kind, limit=limit)
        except DiscoveryError as exc:
            raise _bad(exc, 422) from None
        return {"discoveries": [d.to_dict() for d in items],
                "counts": inbox.counts()}

    @router.post("/api/discoveries/{discovery_id}/accept")
    async def accept(discovery_id: str):
        """Mark an item handled. Accepting does NOT itself perform the action —
        adding the camera, approving the node and naming the person are separate
        calls to their own already-gated endpoints. Keeping them separate means
        the inbox can never become a way to reach a privileged operation that
        the operation's own gate would have refused."""
        try:
            return inbox.decide(discovery_id, STATUS_ACCEPTED).to_dict()
        except DiscoveryError as exc:
            raise _bad(exc, 404) from None

    @router.post("/api/discoveries/{discovery_id}/dismiss")
    async def dismiss(discovery_id: str):
        try:
            return inbox.decide(discovery_id, STATUS_DISMISSED).to_dict()
        except DiscoveryError as exc:
            raise _bad(exc, 404) from None

    @router.post("/api/discoveries/{discovery_id}/add-camera")
    async def add_camera(discovery_id: str, room: str = Body(...),
                         name: str = Body(None), username: str = Body(""),
                         password: str = Body(""),
                         confidence: float = Body(0.6)):
        """Turn a "this looks like a camera" card into a configured camera.

        The user supplies the camera's OWN username and password (the ones they
        set on it) and a room. Wavr asks the camera for its stream address over
        ONVIF -- nobody types an RTSP URL.

        Credentials are request-scoped: used to build the stream URL, never
        stored separately, never echoed, never logged. The camera is created
        DISABLED, because cameras boot off (ADR-0002) -- adding one is not
        turning one on."""
        item = inbox.get(discovery_id)
        if item is None:
            raise HTTPException(status_code=404, detail="unknown discovery")
        if item.kind != KIND_CAMERA_FOUND:
            raise HTTPException(status_code=400,
                                detail="that discovery is not a camera")
        if cameras is None or onvif_probe is None:
            raise HTTPException(status_code=503,
                                detail="camera setup is not available on this Core")
        if not onvif_enabled:
            # Honest, actionable, and names the switch rather than the env var.
            raise HTTPException(
                status_code=503,
                detail="Turn on 'Look for cameras' in Settings first — Wavr has "
                       "to ask the camera for its stream address.")

        ip = (item.detail or {}).get("ip")
        if not ip:
            raise HTTPException(status_code=422,
                                detail="that camera has no address on record")

        result = await onvif_probe(targets=[ip], username=username,
                                   password=password, timeout=4.0)
        found = [c for c in (result or {}).get("cameras", [])
                 if c.get("rtsp_url")]
        if not found:
            # NOT an error state: a camera that wants credentials is the normal
            # case, and the UI should ask for them rather than show a failure.
            return {"status": "needs_credentials" if not username else "unreachable",
                    "message": ("Wavr found the camera but it wants a username and "
                                "password — the ones set on the camera itself."
                                if not username else
                                "Wavr couldn't get a stream from that camera. Check "
                                "the username and password, and that it's on."),
                    "ip": ip}

        cam = found[0]
        url = rtsp_url_with_credentials(cam.get("rtsp_url", ""), username, password)
        if not url:
            raise HTTPException(status_code=502,
                                detail="the camera returned an address Wavr can't use")

        label = " ".join(str(name or cam.get("name") or "Camera").split())[:64]
        try:
            cameras.add(label, " ".join(str(room).split())[:64], url,
                        max(0.0, min(float(confidence), 1.0)))
        except Exception as exc:      # noqa: BLE001 -- duplicate name, bad url...
            raise _bad(exc, 422) from None
        # Bind the camera to its MAC so a DHCP address change can be detected
        # and re-bound later (camera_health's IP-drift check). Best effort: a
        # camera without a known MAC is still a working camera.
        mac = (item.detail or {}).get("mac")
        if mac:
            with suppress(Exception):
                cameras.set_mac(label, mac)
        inbox.decide(discovery_id, STATUS_ACCEPTED)
        return {"status": "added", "name": label, "room": room,
                "enabled": False,
                "note": "Added but switched OFF. Cameras never start themselves; "
                        "turn it on in Devices when you're ready."}

    return router
