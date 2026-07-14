"""FastAPI routers for cross-instance peer pairing (2026-07-09 C1-fix design:
`docs/superpowers/specs/2026-07-09-wavr-peer-pairing-c1-fix-design.md`).

Reshaped pairing (closes C1/C2/M1/I1 + the adversarial-sweep §B/§D/§E hardenings).
The old unauthenticated `/exchange` (which network-VENDED a live central code) and
`PeerExchangeManager` are DELETED; pairing now mirrors the Mobile-to-Core ceremony:
the operator reads the target's 8-digit code off its trusted screen and types it
into the initiator, and the reverse credential bootstraps over the now-authenticated,
fingerprint-pinned channel.

Two router factories (mirrors api_devices.py's split so app.py attaches a DIFFERENT
auth gate to each group; the gates are wired as per-route `dependencies=`, defined
in app.py, NOT here):

  * build_peers_public_router -- the ONE deliberately-UNAUTHENTICATED, in-subnet-
    bounded entry point a REMOTE peer calls (same bound as POST /api/pair):
      - POST /api/peers/redeem  consume a screen-displayed code -> create a
        role=central Device. Safe for the SAME reason /api/pair is: it only ever
        consumes a code minted on a trusted loopback screen (POST /api/pair-code),
        never one vended over the network. Failed attempts are rate-limited PER
        SOURCE IP (§C).

  * build_peers_admin_router -- the LOOPBACK-ROOT control plane + the ONE peer-
    reachable reverse-leg endpoint:
      - GET  /api/peers/discovered  (root) mDNS browse, no network write.
      - POST /api/peers/observe     (root) observe the peer's LIVE cert fingerprint
        (wires the previously-dead `remote_cert_fingerprint`) so the UI shows the
        OBSERVED value, not a self-reported one (M1).
      - POST /api/peers/confirm     (root) orchestrates the forward leg (redeem the
        peer's code over a pinned channel) + the auto reverse bootstrap.
      - POST /api/peers/link-back   (require_central) the reverse-leg completion,
        the ONLY peer-reachable route: an already-authenticated central peer hands
        this instance the credential it will use to call back.
      - GET  /api/peers             (root) list.
      - DELETE /api/peers/{id}      (root) unpair (revoke peer + our device for them).

`admin_deps` (loopback-root-only in app.py: require_local + require_root) guard
discovered/observe/confirm/list/unpair; `linkback_deps` (require_central) guard
link-back only. Both FAIL CLOSED if omitted (see `_admin_deps_not_wired` /
`_linkback_deps_not_wired`) rather than the old `None -> []` (open) default; a test
that wants every endpoint ungated must explicitly pass its own allow-dependency.
`local_ip` bounds the §D SSRF guard on the operator-supplied `peer_base_url`.
"""
from __future__ import annotations

import asyncio
import logging
import urllib.parse

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from wavr import mdns_peers
from wavr.auth import in_subnet, parse_bearer
from wavr.netaddr import is_lan_ip
from wavr.peer_client import PeerClientError, post_json
from wavr.tls import cert_fingerprint, remote_cert_fingerprint, resolved_cert_path


def _validate_peer_url(peer_base_url: str, local_ip: str) -> tuple[str, int]:
    """§D SSRF guard. `peer_base_url` (operator-supplied) must be an `https://` URL
    whose host is an IN-SUBNET private-LAN IP LITERAL -- reusing the existing
    `is_lan_ip` (literal-only, cloud-metadata denylist, IPv4-mapped normalization)
    AND `in_subnet` (same /24 as this node). That combination rejects loopback,
    link-local, multicast, the cloud-metadata endpoint, DNS hostnames, and any
    off-subnet host, BEFORE any socket is opened. Returns (host, port); raises 400.

    Even though `/confirm`/`/observe` are loopback-root-only (a remote peer can never
    reach them), the local operator's own input must not be able to coerce the server
    into dialing an arbitrary internal host."""
    parts = urllib.parse.urlsplit(peer_base_url or "")
    if parts.scheme != "https":
        raise HTTPException(status_code=400, detail="peer_base_url must use https")
    host = parts.hostname
    if not host or not is_lan_ip(host) or not in_subnet(host, local_ip):
        raise HTTPException(status_code=400,
                            detail="peer_base_url must be an in-subnet LAN address")
    return host, parts.port or 443


def build_peers_public_router(peer_store, pairing, cfg) -> APIRouter:
    """The ONE deliberately-UNAUTHENTICATED, in-subnet-bounded entry point a remote
    peer calls. No deps baked in (app.py bounds it like /api/pair). `peer_store`/`cfg`
    are accepted for symmetry; the redeem goes through `pairing`."""
    router = APIRouter()

    @router.post("/api/peers/redeem")
    async def redeem(request: Request, code: str = Body(...), requester_name: str = Body(...)):
        # Safe post-reshape: this only ever consumes a code DISPLAYED on the peer's
        # trusted loopback screen (POST /api/pair-code), never one vended over the
        # network -- /api/peers/exchange, the endpoint that did that (C1), is deleted.
        # Per-IP rate-limiting (§C) means one host's junk guesses can't lock out others.
        source_ip = request.client.host if request.client else None
        result = pairing.redeem(code, requester_name, source_ip=source_ip)
        if result is None:
            raise HTTPException(status_code=403, detail="invalid or expired pairing code")
        device_id, token = result
        # Peer pairing is ALWAYS central: every peer code is minted "central" by the
        # operator's loopback /api/pair-code, and redeem honors the code's role.
        return {"device_id": device_id, "token": token}

    return router


def _admin_deps_not_wired() -> None:
    """FAIL-CLOSED default for `admin_deps` (mirrors api_nodes.py's
    `_admin_deps_not_wired`). Previously `admin_deps=None` -> `[]`, meaning if
    app.py's wiring ever forgot to pass `[Depends(require_local), Depends(require_root)]`,
    every admin route (discovered/observe/confirm/list/unpair) would run completely
    UNAUTHENTICATED. A forgotten argument must never silently open the loopback-root
    control plane, so the default now DENIES instead: every admin route 403s until
    the real gate is explicitly wired. Real callers (app.py) always pass admin_deps
    and never hit this; only an intentional override (e.g. a test standing up its
    own allow-dependency) can make these routes reachable."""
    raise HTTPException(status_code=403,
                        detail="peer admin routes have no auth gate wired")


def _linkback_deps_not_wired() -> None:
    """FAIL-CLOSED default for `linkback_deps` (same rationale as
    `_admin_deps_not_wired`). Previously `linkback_deps=None` -> `[]`, meaning if
    app.py's wiring ever forgot to pass `[Depends(require_central)]`, the ONE
    peer-reachable route (`link-back`) would accept any unauthenticated caller. The
    default now DENIES instead until the real gate is explicitly wired."""
    raise HTTPException(status_code=403,
                        detail="peer link-back route has no auth gate wired")


def build_peers_admin_router(peer_store, pairing, device_store, cfg, self_name,
                             self_base_url, local_ip,
                             admin_deps=None, linkback_deps=None) -> APIRouter:
    """Loopback-root control plane + the single peer-reachable reverse-leg route.
    `admin_deps` (loopback-root-only) wrap discovered/observe/confirm/list/unpair;
    `linkback_deps` (require_central) wrap link-back only. Both FAIL CLOSED if
    omitted/empty -- see `_admin_deps_not_wired` / `_linkback_deps_not_wired` --
    rather than the old `None -> []` (open) default."""
    router = APIRouter()
    admin_deps = list(admin_deps) if admin_deps else [Depends(_admin_deps_not_wired)]
    linkback_deps = list(linkback_deps) if linkback_deps else [Depends(_linkback_deps_not_wired)]

    @router.get("/api/peers/discovered", dependencies=admin_deps)
    async def discovered():
        # Module-qualified call (not a bare imported name) so a test can monkeypatch
        # wavr.mdns_peers.browse_wavr_peers and have THIS see it. Off-loop via
        # to_thread: the real browse blocks ~3s (time.sleep) waiting for mDNS.
        # `zeroconf` is the optional [mdns] extra (base/test installs lack it) --
        # mirror the startup self-advertise path's graceful degrade (app.py's
        # lifespan: missing dep logs + continues, never crashes) instead of letting
        # the lazy `from zeroconf import ...` ModuleNotFoundError bubble into a 500.
        try:
            found = await asyncio.to_thread(mdns_peers.browse_wavr_peers)
        except Exception:
            logging.warning("peer mDNS browse unavailable "
                            "(zeroconf missing or browse failed)", exc_info=True)
            return []
        return [{"name": p.name, "host": p.host, "port": p.port, "role": p.role}
                for p in found]

    @router.post("/api/peers/observe", dependencies=admin_deps)
    async def observe(peer_base_url: str = Body(..., embed=True)):
        # M1: return the OBSERVED live cert fingerprint of the peer (wires the
        # previously-dead remote_cert_fingerprint) so the operator confirms the cert
        # the pinned TLS socket really presents, never a self-reported JSON value.
        host, port = _validate_peer_url(peer_base_url, local_ip)   # §D
        fp = await asyncio.to_thread(remote_cert_fingerprint, host, port)   # §E off-loop
        if fp is None:
            # §B: generic -- never leak whether/why the dial failed, no exfil oracle.
            raise HTTPException(status_code=502, detail="could not reach peer")
        return {"fingerprint": fp}

    @router.post("/api/peers/confirm", dependencies=admin_deps)
    async def confirm(peer_base_url: str = Body(...), peer_name: str = Body(...),
                      peer_code: str = Body(...), peer_fingerprint: str = Body(...)):
        # The operator has (via /observe) confirmed peer_fingerprint matches the value
        # on B's screen and typed B's on-screen code. peer_fingerprint is PINNED on
        # every outbound call below -- I1 verifies it before any credential is sent.
        _validate_peer_url(peer_base_url, local_ip)   # §D SSRF guard before any dial

        # -- Forward leg: A redeems B's code over the pinned channel, receiving OUR
        #    credential to call B (the token B issues us). --
        try:
            redeemed = await asyncio.to_thread(   # §E: never block the event loop
                post_json, peer_base_url, "/api/peers/redeem",
                {"code": peer_code, "requester_name": self_name},
                None, peer_fingerprint)           # token=None, pinned=peer_fingerprint
        except PeerClientError:
            # §B: flat 502 -- never echo the observed fingerprint or the peer's body.
            raise HTTPException(status_code=502, detail="could not reach peer")
        our_token_for_them = redeemed.get("token") if isinstance(redeemed, dict) else None
        if not our_token_for_them:
            raise HTTPException(status_code=502,
                                detail="peer returned malformed pairing response")
        # C2: we DELIBERATELY IGNORE redeemed["device_id"] -- the peer's self-reported
        # echo of its own store id. Persisting it would make our unpair revoke a row
        # that does not exist in OUR store (a no-op that leaves their token alive).

        # -- Reverse bootstrap: A mints B's INBOUND credential in A's OWN store and
        #    pushes it to B over the authenticated + pinned channel. --
        a_did_for_b, b_token_for_a = device_store.add(peer_name, "central")
        # local_device_id = a_did_for_b == OUR id for them: the exact device our unpair
        # must revoke to kill B's inbound token (C2). Derived locally, never forgeable.
        peer_id = peer_store.add(peer_name, peer_base_url, peer_fingerprint,
                                 a_did_for_b, our_token_for_them)
        fp_self = cert_fingerprint(resolved_cert_path(cfg.tls_cert)) or ""
        try:
            await asyncio.to_thread(
                post_json, peer_base_url, "/api/peers/link-back",
                {"token": b_token_for_a, "base_url": self_base_url,
                 "fingerprint": fp_self, "name": self_name},
                our_token_for_them, peer_fingerprint)   # auth A->B as central + pinned
        except PeerClientError:
            # Reverse leg failed (B unreachable at link-back): revoke the just-minted
            # device -- B never received the matching token, so it is dead weight
            # (hygiene). NO silent rollback of the forward leg: A CAN reach B, the
            # operator retries. Honest half-state, surfaced as reverse_leg_ok:false.
            device_store.revoke(a_did_for_b)
            return {"peer_id": peer_id, "reverse_leg_ok": False}
        return {"peer_id": peer_id, "reverse_leg_ok": True}

    @router.post("/api/peers/link-back", dependencies=linkback_deps)
    async def link_back(request: Request, token: str = Body(...), base_url: str = Body(...),
                        fingerprint: str = Body(...), name: str = Body(...)):
        # The reverse-leg completion + the ONLY peer-reachable route (gated
        # require_central by linkback_deps). The caller (A) already authenticated as
        # central with the token THIS instance issued it. Derive OUR-id-for-them from
        # that AUTHENTICATED bearer token via device_store.verify -- the
        # cryptographically-proven id of the caller's device in OUR store, NOT a value
        # A self-reports (C2). `token` is the credential WE will present when WE call A.
        bearer = parse_bearer(request.headers.get("authorization"))
        caller = device_store.verify(bearer) if bearer else None
        if caller is None:
            raise HTTPException(status_code=401,
                                detail="link-back requires an authenticated central peer")
        # §D SSRF guard: `base_url` is peer-supplied (the caller's own callback
        # address). Without this, an authenticated-but-malicious peer could hand us
        # an off-subnet/non-LAN base_url that we would later dial (e.g. on unpair's
        # future re-confirm flows or any other consumer of PeerStore rows) -- the
        # same class of landmine _validate_peer_url already closes on /confirm and
        # /observe's operator-supplied peer_base_url (#14).
        _validate_peer_url(base_url, local_ip)
        peer_id = peer_store.add(name, base_url, fingerprint, caller.device_id, token)
        return {"peer_id": peer_id}

    @router.get("/api/peers", dependencies=admin_deps)
    async def list_peers():
        return [p.to_dict() for p in peer_store.list()]

    @router.delete("/api/peers/{peer_id}", dependencies=admin_deps)
    async def unpair(peer_id: str):
        peer = peer_store.get(peer_id)
        if peer is None:
            raise HTTPException(status_code=404, detail="unknown peer")
        # Revoke BOTH directions: PeerStore (how WE reach THEM -- clears our outbound
        # token) and the DeviceStore row named by local_device_id (how THEY reach US).
        # Post-C2, local_device_id is OUR id for them, so this revoke actually kills the
        # peer's INBOUND token (their next call 401s). Either alone leaves a half-open link.
        peer_store.revoke(peer_id)
        device_store.revoke(peer.local_device_id)
        return {"ok": True}

    return router
