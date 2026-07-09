"""FastAPI routers for cross-instance peer pairing (2026-07-09 design spec,
Phase 1). See the protocol walkthrough at the top of
docs/superpowers/plans/2026-07-09-wavr-peer-discovery-pairing.md for the full
two-leg handshake this implements.

Access model (mirrors api_devices.py's existing split -- TWO router factories,
so app.py can attach a DIFFERENT auth gate to each group; this module wires the
gates as per-route `dependencies=`, it does NOT reimplement them):

  * build_peers_public_router -- the deliberately-UNAUTHENTICATED, in-subnet-
    bounded entry points a REMOTE peer calls (same bound as POST /api/pair):
      - POST /api/peers/exchange  stash the requester's half + hand back OUR
        OWN fresh central code and OUR live serving-cert fingerprint.
      - POST /api/peers/redeem    consume a code -> create a role=central
        Device. This IS /api/pair with the role hardcoded, kept as its own
        endpoint for peer-specific auditability (see design spec).
    No deps baked in -- app.py bounds these the same in-subnet way it bounds
    /api/pair; the pairing code's ~2-min one-time window is the real limit.

  * build_peers_admin_router -- the local-admin + peer-authenticated surface:
      - GET  /api/peers/discovered  (admin gate) this instance's mDNS browse,
        no network write.
      - POST /api/peers/confirm     (admin gate) the human-in-the-loop step
        after the admin visually confirms the fingerprint /exchange returned;
        orchestrates BOTH legs (calls the peer's /redeem, then its /finish).
      - POST /api/peers/finish      (PEER-token gate -- a DIFFERENT gate than
        the loopback-admin ones) the reverse-leg completion, callable only by
        a peer that JUST authenticated as central from the SAME exchange.
      - GET  /api/peers             (admin gate) list.
      - DELETE /api/peers/{id}      (admin gate) unpair (revoke peer + device).

`admin_deps` guard discovered/confirm/list/unpair; `finish_deps` guard /finish
ONLY (its gate is authenticated-by-a-peer-token, not loopback-admin). Both
default None -> [] so the Task-6 router tests hit every endpoint directly with
no gates. `cfg`, `self_base_url`, `pairing` and `peer_store` are threaded into
both factories for symmetry and Task-7 wiring even where an individual endpoint
body does not consume them.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from wavr import mdns_peers
from wavr.peer_client import PeerClientError, post_json
from wavr.tls import cert_fingerprint, resolved_cert_path


def build_peers_public_router(peer_store, exchange_mgr, pairing, cfg, self_name) -> APIRouter:
    """UNAUTHENTICATED, in-subnet-bounded entry points a remote peer calls.
    No deps baked in (app.py bounds these like /api/pair). `peer_store` is
    accepted for symmetry with the admin factory; the two legs here go through
    `exchange_mgr` + `pairing`."""
    router = APIRouter()

    @router.post("/api/peers/exchange")
    async def exchange(requester_name: str = Body(...),
                       requester_base_url: str = Body(...),
                       requester_code: str = Body(...),
                       requester_fingerprint: str = Body(...)):
        # Stash the requester's half (their code, so WE can redeem it once they
        # come back authenticated at /finish) and hand back OUR OWN fresh
        # central code + OUR live serving-cert fingerprint in one response --
        # protocol step 3. The fingerprint is read the SAME way POST
        # /api/pair-code reads it (resolved_cert_path -> cert_fingerprint): a
        # missing/unreadable cert yields None -> "" (never a crash).
        exchange_id = exchange_mgr.stash(requester_name, requester_base_url,
                                         requester_code, requester_fingerprint)
        own_code = pairing.mint_code("central")
        return {
            "exchange_id": exchange_id,
            "code": own_code,
            "fingerprint": cert_fingerprint(resolved_cert_path(cfg.tls_cert)) or "",
            "name": self_name,
        }

    @router.post("/api/peers/redeem")
    async def redeem(code: str = Body(...), requester_name: str = Body(...)):
        result = pairing.redeem(code, requester_name)
        if result is None:
            raise HTTPException(status_code=403, detail="invalid or expired pairing code")
        device_id, token = result
        # Peer pairing is ALWAYS central: PairingManager.redeem honors the role
        # the code was minted with, and every peer code is minted "central"
        # (in /exchange here and in the operator's own local mint), so no extra
        # role assignment is needed.
        return {"device_id": device_id, "token": token}

    return router


def build_peers_admin_router(peer_store, exchange_mgr, pairing, device_store,
                             cfg, self_name, self_base_url,
                             admin_deps=None, finish_deps=None) -> APIRouter:
    """Local-admin + peer-authenticated surface. `admin_deps` (loopback-admin
    gate) wrap discovered/confirm/list/unpair; `finish_deps` (the DIFFERENT
    peer-token gate) wrap /finish only. Both default None -> [] so the Task-6
    tests call every route ungated. `cfg`/`self_base_url`/`pairing` are carried
    for symmetry + Task-7 wiring."""
    router = APIRouter()
    admin_deps = list(admin_deps or [])
    finish_deps = list(finish_deps or [])

    @router.get("/api/peers/discovered", dependencies=admin_deps)
    async def discovered():
        # Module-qualified call (not a bare imported name) so a test can
        # monkeypatch wavr.mdns_peers.browse_wavr_peers and have THIS see it.
        found = mdns_peers.browse_wavr_peers()
        return [{"name": p.name, "host": p.host, "port": p.port, "role": p.role}
                for p in found]

    @router.post("/api/peers/confirm", dependencies=admin_deps)
    async def confirm(exchange_id: str = Body(...), peer_code: str = Body(...),
                      peer_fingerprint: str = Body(...), peer_base_url: str = Body(...),
                      peer_name: str = Body(...)):
        # The admin has visually confirmed peer_fingerprint (surfaced by the
        # /exchange call the frontend already made) matches the peer's own
        # on-screen display. Leg (a): redeem the peer's code -> get OUR
        # credential (the token THEY issue us) to reach them, and persist the
        # peer relationship in PeerStore.
        try:
            redeemed = post_json(peer_base_url, "/api/peers/redeem",
                                 {"code": peer_code, "requester_name": self_name},
                                 pinned_fingerprint=peer_fingerprint)
        except PeerClientError as exc:
            raise HTTPException(status_code=502, detail=f"could not reach peer: {exc}") from exc
        our_token_for_them = redeemed["token"]
        peer_id = peer_store.add(peer_name, peer_base_url, peer_fingerprint,
                                 redeemed["device_id"], our_token_for_them)
        # Leg (b): tell them to /finish, authenticated with the token we JUST
        # received so THEIR /finish sees us as an already-central peer. Leg (a)
        # is durably stored; if leg (b) fails it is REPORTED, not rolled back
        # (this instance CAN reach the peer -- the admin retries /finish or
        # re-pairs from the other side; same "no silent rollback" rule as the
        # bulk config push in the design spec).
        try:
            post_json(peer_base_url, "/api/peers/finish", {"exchange_id": exchange_id},
                      token=our_token_for_them, pinned_fingerprint=peer_fingerprint)
        except PeerClientError as exc:
            return {"peer_id": peer_id, "reverse_leg_ok": False, "error": str(exc)}
        return {"peer_id": peer_id, "reverse_leg_ok": True}

    @router.post("/api/peers/finish", dependencies=finish_deps)
    async def finish(exchange_id: str = Body(..., embed=True)):
        # AUTHENTICATED by the peer-token gate (finish_deps) -- the caller is
        # already a recognized central device by the time this fires. Complete
        # the reverse leg: pop the exchange WE stashed in /exchange, then redeem
        # THEIR code against THEM (we become a Device in their store, just as
        # they became one in ours via /confirm's leg (a)). `embed=True` because
        # this single body param is sent as {"exchange_id": ...} by /confirm.
        pending = exchange_mgr.pop(exchange_id)
        if pending is None:
            raise HTTPException(status_code=404, detail="unknown or expired exchange")
        try:
            redeemed = post_json(pending.requester_base_url, "/api/peers/redeem",
                                 {"code": pending.requester_code, "requester_name": self_name},
                                 pinned_fingerprint=pending.requester_fingerprint)
        except PeerClientError as exc:
            raise HTTPException(status_code=502,
                                detail=f"could not complete reverse pairing: {exc}") from exc
        peer_id = peer_store.add(pending.requester_name, pending.requester_base_url,
                                 pending.requester_fingerprint, redeemed["device_id"],
                                 redeemed["token"])
        return {"peer_id": peer_id}

    @router.get("/api/peers", dependencies=admin_deps)
    async def list_peers():
        return [p.to_dict() for p in peer_store.list()]

    @router.delete("/api/peers/{peer_id}", dependencies=admin_deps)
    async def unpair(peer_id: str):
        peer = peer_store.get(peer_id)
        if peer is None:
            raise HTTPException(status_code=404, detail="unknown peer")
        # Revoke BOTH directions: PeerStore (how WE reach THEM -- clears our
        # outbound token) and the DeviceStore row (how THEY reach US -- kills
        # the token they present). Either alone would leave a half-open link.
        peer_store.revoke(peer_id)
        device_store.revoke(peer.local_device_id)
        return {"ok": True}

    return router
