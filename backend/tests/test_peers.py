import pytest
from wavr.peers import PeerStore


def _store(tmp_path):
    return PeerStore(str(tmp_path / "peers.db"))


def test_add_returns_peer_id_and_is_listed(tmp_path):
    store = _store(tmp_path)
    peer_id = store.add(name="Core-G9", base_url="https://192.168.1.57:8000",
                         cert_fingerprint="AB:CD:EF", local_device_id="dev123",
                         token="secret-token-abc")
    assert peer_id
    peers = store.list()
    assert len(peers) == 1
    assert peers[0].peer_id == peer_id
    assert peers[0].name == "Core-G9"
    assert peers[0].base_url == "https://192.168.1.57:8000"
    assert peers[0].cert_fingerprint == "AB:CD:EF"
    assert peers[0].room_map == {}
    assert peers[0].revoked is False


def test_token_for_returns_the_stored_token(tmp_path):
    store = _store(tmp_path)
    peer_id = store.add("Core-G9", "https://x:8000", "FP", "dev1", "tok-xyz")
    assert store.token_for(peer_id) == "tok-xyz"


def test_token_for_unknown_peer_is_none(tmp_path):
    store = _store(tmp_path)
    assert store.token_for("nope") is None


def test_list_never_includes_token(tmp_path):
    store = _store(tmp_path)
    store.add("Core-G9", "https://x:8000", "FP", "dev1", "tok-xyz")
    peer = store.list()[0]
    assert not hasattr(peer, "token")


def test_get_by_id(tmp_path):
    store = _store(tmp_path)
    peer_id = store.add("Core-G9", "https://x:8000", "FP", "dev1", "tok")
    assert store.get(peer_id).name == "Core-G9"
    assert store.get("nope") is None


def test_set_room_map_persists(tmp_path):
    store = _store(tmp_path)
    peer_id = store.add("Core-G9", "https://x:8000", "FP", "dev1", "tok")
    assert store.set_room_map(peer_id, {"sala": "living_room"}) is True
    assert store.get(peer_id).room_map == {"sala": "living_room"}


def test_set_room_map_unknown_peer_returns_false(tmp_path):
    store = _store(tmp_path)
    assert store.set_room_map("nope", {"a": "b"}) is False


def test_revoke_marks_revoked_and_clears_token(tmp_path):
    store = _store(tmp_path)
    peer_id = store.add("Core-G9", "https://x:8000", "FP", "dev1", "tok")
    assert store.revoke(peer_id) is True
    assert store.get(peer_id).revoked is True
    assert store.token_for(peer_id) is None  # revoked = unusable, not just flagged


def test_revoke_unknown_peer_returns_false(tmp_path):
    store = _store(tmp_path)
    assert store.revoke("nope") is False


def test_revoke_is_idempotent(tmp_path):
    store = _store(tmp_path)
    peer_id = store.add("Core-G9", "https://x:8000", "FP", "dev1", "tok")
    assert store.revoke(peer_id) is True
    assert store.revoke(peer_id) is True  # second revoke still True, not an error


# --------------------------------------------------------------------------
# api_peers.py router-level tests. TWO router factories -- the public
# (unauthenticated, in-subnet) redeem entry point and the loopback-root
# discovered/observe/confirm/link-back/list/unpair surface -- mirroring
# api_devices.py's split so app.py can attach different auth gates per group.
# Here every endpoint is exercised directly with no gates (admin_deps/
# linkback_deps default to []); the auth wiring is proven in the create_app
# tests below.
# --------------------------------------------------------------------------
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient

from wavr.api_peers import build_peers_public_router, build_peers_admin_router
from wavr.devices import DeviceStore
from wavr.pairing import PairingManager


def _app(tmp_path, self_base_url="https://192.168.1.10:8000", self_name="Desktop",
         local_ip="192.168.1.1"):
    devices = DeviceStore(str(tmp_path / "devices.db"))
    peers = PeerStore(str(tmp_path / "peers.db"))
    pairing = PairingManager(devices)
    cfg = types.SimpleNamespace(tls_cert="")  # resolved_cert_path("") -> default; absent -> ""
    app = FastAPI()
    app.include_router(build_peers_public_router(peers, pairing, cfg))
    app.include_router(build_peers_admin_router(peers, pairing, devices, cfg,
                                                self_name, self_base_url, local_ip))
    return app, devices, peers, pairing


def test_discovered_lists_mdns_results(tmp_path, monkeypatch):
    app, *_ = _app(tmp_path)
    from wavr import mdns_peers
    monkeypatch.setattr(mdns_peers, "browse_wavr_peers",
                        lambda **k: [mdns_peers.DiscoveredPeer("Core", "1.2.3.4", 8000, "core")])
    client = TestClient(app)
    r = client.get("/api/peers/discovered")
    assert r.status_code == 200
    assert r.json() == [{"name": "Core", "host": "1.2.3.4", "port": 8000, "role": "core"}]


def test_discovered_degrades_to_empty_list_when_zeroconf_missing(tmp_path, monkeypatch):
    # `zeroconf` is the optional [mdns] extra -- a base/test install lacks it, so the
    # real browse_wavr_peers's lazy `from zeroconf import ...` raises ModuleNotFoundError.
    # /api/peers/discovered must degrade to [] (mirroring the startup self-advertise
    # path), never bubble that into an unhandled 500.
    app, *_ = _app(tmp_path)
    from wavr import mdns_peers

    def _raise(**k):
        raise ModuleNotFoundError("No module named 'zeroconf'")

    monkeypatch.setattr(mdns_peers, "browse_wavr_peers", _raise)
    client = TestClient(app)
    r = client.get("/api/peers/discovered")
    assert r.status_code == 200
    assert r.json() == []


def test_no_exchange_endpoint_exists(tmp_path):
    # C1 root cause deleted: nothing may network-vend a central code. (405, not 404,
    # because the path collides with the DELETE /api/peers/{peer_id} route pattern --
    # either way POST cannot reach a code-vending handler, because there is none.)
    app, *_ = _app(tmp_path)
    client = TestClient(app)
    r = client.post("/api/peers/exchange", json={})
    assert r.status_code in (404, 405) and "code" not in r.json()


def test_redeem_creates_central_device(tmp_path):
    app, devices, peers, pairing = _app(tmp_path)
    code = pairing.mint_code("central")
    client = TestClient(app)
    r = client.post("/api/peers/redeem", json={"code": code, "requester_name": "Desktop"})
    assert r.status_code == 200
    body = r.json()
    assert "device_id" in body and "token" in body
    dev = devices.get(body["device_id"])
    assert dev.role == "central"


def test_redeem_rejects_bad_code(tmp_path):
    app, *_ = _app(tmp_path)
    client = TestClient(app)
    r = client.post("/api/peers/redeem", json={"code": "00000000", "requester_name": "X"})
    assert r.status_code == 403


def test_observe_returns_observed_fingerprint(tmp_path, monkeypatch):
    # M1: /observe returns the OBSERVED live cert fingerprint (wires remote_cert_fingerprint).
    app, *_ = _app(tmp_path)
    import wavr.api_peers as api_peers
    monkeypatch.setattr(api_peers, "remote_cert_fingerprint", lambda host, port: "OB:SE:RV:ED")
    client = TestClient(app)
    r = client.post("/api/peers/observe", json={"peer_base_url": "https://192.168.1.20:8000"})
    assert r.status_code == 200 and r.json()["fingerprint"] == "OB:SE:RV:ED"


def test_observe_unreachable_peer_is_generic_502(tmp_path, monkeypatch):
    # §B: an unreachable peer yields a flat 502 with no leaked detail.
    app, *_ = _app(tmp_path)
    import wavr.api_peers as api_peers
    monkeypatch.setattr(api_peers, "remote_cert_fingerprint", lambda host, port: None)
    client = TestClient(app)
    r = client.post("/api/peers/observe", json={"peer_base_url": "https://192.168.1.20:8000"})
    assert r.status_code == 502


def test_observe_ssrf_guard_rejects_bad_urls(tmp_path):
    # §D: https-only, in-subnet LAN literal only. No socket is opened for a rejected URL.
    app, *_ = _app(tmp_path)   # local_ip 192.168.1.1
    client = TestClient(app)
    for url in ("http://192.168.1.20:8000",       # not https
                "https://10.0.0.5:8000",           # off-subnet private
                "https://127.0.0.1:8000",          # loopback
                "https://169.254.169.254:8000",    # cloud metadata / link-local
                "https://8.8.8.8:8000",            # public
                "https://evil.example.com:8000"):  # DNS hostname
        assert client.post("/api/peers/observe",
                           json={"peer_base_url": url}).status_code == 400, url


def test_confirm_ssrf_guard_rejects_before_dial(tmp_path):
    # §D: a bad peer_base_url is rejected 400 BEFORE any dial (no transport wired -> if it
    # dialed, it would blow up instead of returning a clean 400).
    app, *_ = _app(tmp_path)
    client = TestClient(app)
    r = client.post("/api/peers/confirm", json={
        "peer_base_url": "https://8.8.8.8:8000", "peer_name": "X",
        "peer_code": "1", "peer_fingerprint": "Z"})
    assert r.status_code == 400


def test_link_back_persists_our_device_id_from_bearer(tmp_path):
    # C2: link-back derives local_device_id from the AUTHENTICATED bearer token
    # (our-id-for-them), never a value the caller self-reports.
    app, devices, peers, pairing = _app(tmp_path)
    our_dev_id, caller_token = devices.add("Core", "central")  # as if the peer redeemed our code
    client = TestClient(app)
    r = client.post("/api/peers/link-back",
                    headers={"Authorization": f"Bearer {caller_token}"},
                    json={"token": "reverse-cred", "base_url": "https://192.168.1.20:8000",
                          "fingerprint": "CORE-FP", "name": "Core"})
    assert r.status_code == 200
    peer_id = r.json()["peer_id"]
    peer = peers.get(peer_id)
    assert peer.local_device_id == our_dev_id            # our-id-for-them, from the bearer
    assert peers.token_for(peer_id) == "reverse-cred"    # the credential WE present to them


def test_link_back_requires_authenticated_bearer(tmp_path):
    app, *_ = _app(tmp_path)
    client = TestClient(app)
    r = client.post("/api/peers/link-back", json={
        "token": "x", "base_url": "https://192.168.1.20:8000", "fingerprint": "F", "name": "N"})
    assert r.status_code == 401


def test_list_peers_empty_initially(tmp_path):
    app, *_ = _app(tmp_path)
    client = TestClient(app)
    r = client.get("/api/peers")
    assert r.status_code == 200
    assert r.json() == []


def test_unpair_revokes_peer_and_device(tmp_path):
    app, devices, peers, pairing = _app(tmp_path)
    device_id, token = devices.add("Core", "central")
    peer_id = peers.add("Core", "https://core:8000", "FP", device_id, "their-token")
    client = TestClient(app)
    r = client.delete(f"/api/peers/{peer_id}")
    assert r.status_code == 200
    assert peers.get(peer_id).revoked is True
    assert devices.get(device_id).revoked is True


# --------------------------------------------------------------------------
# The full two-instance handshake (PROTOCOL, via the bare _app() harness) +
# the create_app auth-gate wiring (real middleware + route deps).
# --------------------------------------------------------------------------
from wavr.app import create_app
from wavr.storage import Storage
from wavr.camera_store import CameraStore
from wavr.sources.simulated import SimulatedSource

_CSRF = {"X-Wavr-Local": "1"}       # loopback root's CSRF header
_D_URL = "https://192.168.1.10:8000"
_C_URL = "https://192.168.1.20:8000"


def _routed_transport(d_client, c_client, captured=None):
    """A fake peer_client transport that routes an outbound call to the OTHER app's
    TestClient (keyed by base_url) instead of the network, so the whole 2-leg
    handshake runs in-process with zero real sockets."""
    import json as _json

    def transport(method, url, headers, body, pinned_fingerprint, timeout):
        client = c_client if url.startswith(_C_URL) else d_client
        path = url.split(":8000", 1)[1]
        if captured is not None and path == "/api/peers/link-back":
            captured["reverse_token"] = _json.loads(body)["token"]
        resp = (client.post(path, json=_json.loads(body), headers=headers) if method == "POST"
                else client.get(path, headers=headers))
        return resp.content
    return transport


def test_full_bidirectional_pairing_two_instances(tmp_path):
    """The reshaped end-to-end protocol on the bare harness: Desktop (A) drives
    /confirm against Core (B) -- forward manual-code redeem + auto reverse link-back
    -- and both sides end with a PeerStore row + a role=central DeviceStore row,
    with local_device_id = OUR-id-for-them on each side (C2)."""
    (tmp_path / "d").mkdir()
    (tmp_path / "c").mkdir()
    d_app, d_devices, d_peers, d_pairing = _app(
        tmp_path / "d", self_base_url=_D_URL, self_name="Desktop")
    c_app, c_devices, c_peers, c_pairing = _app(
        tmp_path / "c", self_base_url=_C_URL, self_name="Core")
    d_client, c_client = TestClient(d_app), TestClient(c_app)

    import wavr.peer_client as peer_client
    orig = peer_client._default_transport
    peer_client._default_transport = _routed_transport(d_client, c_client)
    try:
        # Precondition (human, out-of-band): Core displays its own central code on its
        # trusted screen -- here it just mints it (the operator would read it off B).
        c_code = c_pairing.mint_code("central")
        # Operator drives Desktop's /confirm (forward redeem + auto reverse link-back).
        result = d_client.post("/api/peers/confirm", json={
            "peer_base_url": _C_URL, "peer_name": "Core",
            "peer_code": c_code, "peer_fingerprint": "CORE-FP"}).json()
        assert result["reverse_leg_ok"] is True

        # PeerStore row on BOTH sides.
        assert len(d_peers.list()) == 1 and d_peers.list()[0].name == "Core"
        assert len(c_peers.list()) == 1 and c_peers.list()[0].name == "Desktop"
        # role=central Device on BOTH sides.
        assert d_devices.list()[0].role == "central"   # D's own device for C (a_did_for_b)
        assert c_devices.list()[0].role == "central"   # C's device for D (from the redeem)
        # C2: each side's local_device_id names a device in ITS OWN store.
        assert d_devices.get(d_peers.list()[0].local_device_id) is not None
        assert c_devices.get(c_peers.list()[0].local_device_id) is not None
    finally:
        peer_client._default_transport = orig


def test_confirm_reverse_leg_failure_is_reported_not_rolled_back(tmp_path):
    # §9 residual-risk 2: forward leg succeeds, reverse link-back fails -> reported
    # (reverse_leg_ok:false), the just-minted local device is revoked (hygiene), and
    # the forward leg is NOT rolled back (A can reach B; the operator retries).
    (tmp_path / "d").mkdir()
    (tmp_path / "c").mkdir()
    d_app, d_devices, d_peers, d_pairing = _app(tmp_path / "d", self_base_url=_D_URL, self_name="Desktop")
    c_app, c_devices, c_peers, c_pairing = _app(tmp_path / "c", self_base_url=_C_URL, self_name="Core")
    d_client, c_client = TestClient(d_app), TestClient(c_app)

    import wavr.peer_client as peer_client
    from wavr.peer_client import PeerClientError
    orig = peer_client._default_transport
    import json as _json

    def transport(method, url, headers, body, pin, timeout):
        path = url.split(":8000", 1)[1]
        if path == "/api/peers/link-back":
            raise PeerClientError("reverse leg unreachable")   # B down at link-back
        resp = c_client.post(path, json=_json.loads(body), headers=headers)
        return resp.content

    peer_client._default_transport = transport
    try:
        c_code = c_pairing.mint_code("central")
        result = d_client.post("/api/peers/confirm", json={
            "peer_base_url": _C_URL, "peer_name": "Core",
            "peer_code": c_code, "peer_fingerprint": "CORE-FP"}).json()
        assert result["reverse_leg_ok"] is False
        # Forward leg persisted (NOT rolled back): D still holds its PeerStore row for C.
        assert len(d_peers.list()) == 1
        # Hygiene: the just-minted local device was revoked (B never got its token).
        assert d_devices.list()[0].revoked is True
    finally:
        peer_client._default_transport = orig


def _peers_app(tmp_path, monkeypatch, peers="1", multidevice="1"):
    """A REAL create_app with peers (and multidevice) toggled, a forged fixed
    LAN IP so an in-subnet peer can be simulated, and in-memory storage/cameras
    so only the device+peer store touch the temp db file."""
    if multidevice is None:
        monkeypatch.delenv("WAVR_MULTIDEVICE", raising=False)
    else:
        monkeypatch.setenv("WAVR_MULTIDEVICE", multidevice)
    monkeypatch.setenv("WAVR_PEERS_ENABLED", peers)
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "peers-app.db"))
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    return create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))


def test_peers_enabled_requires_multidevice(tmp_path, monkeypatch):
    # Prerequisite validation: peers ON without multidevice must fail fast.
    monkeypatch.setenv("WAVR_PEERS_ENABLED", "1")
    monkeypatch.delenv("WAVR_MULTIDEVICE", raising=False)
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "x.db"))
    with pytest.raises(RuntimeError, match="requires WAVR_MULTIDEVICE"):
        create_app(sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
                   storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))


def test_admin_peer_routes_gated_but_public_redeem_open(tmp_path, monkeypatch):
    app = _peers_app(tmp_path, monkeypatch)
    peer = TestClient(app, client=("192.168.1.50", 12345))  # forged in-subnet LAN peer
    # Admin surface is gated: an unauthenticated LAN peer is refused (middleware, no token).
    assert peer.get("/api/peers").status_code == 403
    assert peer.get("/api/peers/discovered").status_code == 403
    assert peer.post("/api/peers/observe", json={"peer_base_url": _C_URL}).status_code == 403
    assert peer.post("/api/peers/confirm", json={
        "peer_base_url": _C_URL, "peer_code": "y", "peer_fingerprint": "z",
        "peer_name": "Core"}).status_code == 403
    # /api/peers/exchange no longer exists -- an unauth peer cannot vend a code (403).
    assert peer.post("/api/peers/exchange", json={}).status_code != 200
    # ...but the deliberately-unauthenticated public /redeem IS reachable without a token
    # (exactly like /api/pair). A bad code reaches the HANDLER (its 403 detail, not the
    # middleware's) -- proving it was not middleware-blocked.
    r = peer.post("/api/peers/redeem", json={"code": "00000000", "requester_name": "X"})
    assert r.status_code == 403 and r.json()["detail"] == "invalid or expired pairing code"


def test_admin_peer_route_needs_csrf_on_loopback_root(tmp_path, monkeypatch):
    # require_local is attached to the admin router: loopback root WITHOUT the CSRF
    # header is refused; WITH it, the list works.
    app = _peers_app(tmp_path, monkeypatch)
    root = TestClient(app)
    assert root.get("/api/peers").status_code == 403           # missing X-Wavr-Local
    ok = root.get("/api/peers", headers=_CSRF)
    assert ok.status_code == 200 and ok.json() == []


def test_out_of_subnet_peer_cannot_reach_public_redeem(tmp_path, monkeypatch):
    # The public exemption is in-subnet-bounded, same as /api/pair: an out-of-/24 peer
    # is still forbidden even on /api/peers/redeem.
    app = _peers_app(tmp_path, monkeypatch)
    outsider = TestClient(app, client=("10.0.0.5", 12345))
    assert outsider.post("/api/peers/redeem", json={
        "code": "1", "requester_name": "X"}).status_code == 403


def test_peer_routes_absent_when_flag_off(tmp_path, monkeypatch):
    # Default-off wiring: multidevice ON but peers OFF -> routers not mounted -> 404.
    app = _peers_app(tmp_path, monkeypatch, peers="0")
    root = TestClient(app)
    assert root.get("/api/peers", headers=_CSRF).status_code == 404


def test_lifespan_boots_with_peers_enabled_without_zeroconf(tmp_path, monkeypatch):
    # zeroconf is NOT installed; the lifespan mDNS self-advertise must fail soft.
    app = _peers_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert client.get("/api/status").status_code == 200


# --------------------------------------------------------------------------
# ACCEPTANCE (C1-fix wave): two REAL create_app instances wired to call each
# other via a routed transport, proving (a)-(e) with real middleware + gates.
# --------------------------------------------------------------------------
def _two_real_apps(tmp_path, monkeypatch):
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_PEERS_ENABLED", "1")

    def _mk(sub, name):
        monkeypatch.setenv("WAVR_DB", str(tmp_path / f"{sub}.db"))
        monkeypatch.setenv("WAVR_INSTANCE_NAME", name)
        return create_app(sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
                          storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))

    return _mk("d", "Desktop"), _mk("c", "Core")


def test_c1_closed_in_subnet_host_cannot_obtain_central_token(tmp_path, monkeypatch):
    # (a) A bare in-subnet host can NO LONGER obtain a central token: there is no
    # /exchange to vend a code, and /redeem needs a code that only comes from the
    # trusted screen (a guess is rejected + per-IP rate-limited).
    app = _peers_app(tmp_path, monkeypatch)
    attacker = TestClient(app, client=("192.168.1.66", 40000))
    ex = attacker.post("/api/peers/exchange", json={
        "requester_name": "e", "requester_base_url": _D_URL,
        "requester_code": "1", "requester_fingerprint": "f"})
    assert ex.status_code != 200 and "code" not in ex.json()   # no code-vending endpoint
    assert attacker.post("/api/peers/redeem", json={
        "code": "00000000", "requester_name": "e"}).status_code == 403


def test_acceptance_pairing_revocation_and_root_gates(tmp_path, monkeypatch):
    # (b)+(c)+(d)+(e) in one real two-instance run.
    d_app, c_app = _two_real_apps(tmp_path, monkeypatch)
    d_root = TestClient(d_app)                                # loopback root operator on D
    c_root = TestClient(c_app)                                # loopback root operator on C
    c_from_d = TestClient(c_app, client=("192.168.1.10", 12345))   # D as seen by C

    captured = {}
    import json as _json

    def transport(method, url, headers, body, pin, timeout):
        # Every outbound call in this flow is D -> C.
        path = url.split(":8000", 1)[1]
        if path == "/api/peers/link-back":
            captured["reverse_token"] = _json.loads(body)["token"]
        resp = (c_from_d.post(path, json=_json.loads(body), headers=headers) if method == "POST"
                else c_from_d.get(path, headers=headers))
        return resp.content

    import wavr.peer_client as peer_client
    orig = peer_client._default_transport
    peer_client._default_transport = transport
    try:
        # Precondition: Core mints + displays its central code on its trusted screen.
        c_code = c_root.post("/api/pair-code", json={"role": "central"},
                             headers=_CSRF).json()["code"]
        # (e) Operator drives D's /confirm -> forward redeem + auto reverse link-back.
        result = d_root.post("/api/peers/confirm", headers=_CSRF, json={
            "peer_base_url": _C_URL, "peer_name": "Core",
            "peer_code": c_code, "peer_fingerprint": "CORE-FP"}).json()
        assert result["reverse_leg_ok"] is True             # (d) link-back accepted the central peer

        d_peers = d_root.get("/api/peers", headers=_CSRF).json()
        c_peers = c_root.get("/api/peers", headers=_CSRF).json()
        assert len(d_peers) == 1 and d_peers[0]["name"] == "Core"     # (e) PeerStore both sides
        assert len(c_peers) == 1 and c_peers[0]["name"] == "Desktop"
        d_devs = d_root.get("/api/devices", headers=_CSRF).json()["devices"]
        c_devs = c_root.get("/api/devices", headers=_CSRF).json()["devices"]
        assert any(x["role"] == "central" for x in d_devs)            # (e) central Device both sides
        assert any(x["role"] == "central" for x in c_devs)

        # C's inbound credential to D (reverse_token = b_token_for_a): valid central token in D's store.
        c_tok = captured["reverse_token"]
        c_as_peer = TestClient(d_app, client=("192.168.1.20", 999))   # C as seen by D
        auth = {"Authorization": f"Bearer {c_tok}"}
        # Before unpair: this central peer CAN hit a require_central route on D.
        assert c_as_peer.get("/api/devices", headers=auth).status_code == 200

        # (c) ...but the peer control plane is loopback-ROOT only -- a remote central
        # peer is REJECTED on every admin route (require_root).
        assert c_as_peer.get("/api/peers", headers=auth).status_code == 403
        assert c_as_peer.get("/api/peers/discovered", headers=auth).status_code == 403
        assert c_as_peer.post("/api/peers/observe", headers=auth,
                              json={"peer_base_url": _C_URL}).status_code == 403
        assert c_as_peer.post("/api/peers/confirm", headers=auth, json={
            "peer_base_url": _C_URL, "peer_name": "X",
            "peer_code": "1", "peer_fingerprint": "Z"}).status_code == 403
        assert c_as_peer.delete(f"/api/peers/{d_peers[0]['peer_id']}",
                                headers=auth).status_code == 403

        # (b) D unpairs Core -> device named by local_device_id (our-id-for-them) is
        # revoked -> C's inbound token is now rejected (C2 closed).
        assert d_root.delete(f"/api/peers/{d_peers[0]['peer_id']}",
                             headers=_CSRF).status_code == 200
        assert c_as_peer.get("/api/devices", headers=auth).status_code == 403
    finally:
        peer_client._default_transport = orig
