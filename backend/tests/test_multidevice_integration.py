"""End-to-end integration of the multi-device auth wiring in create_app (ADR-0006).

Uses TestClient's `client=(host, port)` to forge a non-loopback LAN peer (same technique
as test_app.py), and monkeypatches `_local_ipv4` so a fixed "192.168.1.x" is in-subnet.
Exercises the REAL middleware + route guards, verifying the security-audit fixes:
C1 (device-route role gate), M2 (WS/HTTP subnet), M3 (pair reachability), revocation.
"""
import asyncio
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from wavr.app import create_app
from wavr.storage import Storage
from wavr.camera_store import CameraStore
from wavr.sources.simulated import SimulatedSource

CSRF = {"X-Wavr-Local": "1"}       # loopback root's CSRF header


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "md.db"))
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    # In-memory storage/cameras so only the device store touches the temp file (no lock);
    # sources registered but not started (no lifespan) — auth doesn't need them.
    return create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))


def _pair(app, role="user"):
    """Central (loopback root) mints a code; a forged in-subnet LAN peer redeems it."""
    central = TestClient(app)
    code = central.post("/api/pair-code", json={"role": role}, headers=CSRF).json()["code"]
    peer = TestClient(app, client=("192.168.1.50", 12345))
    token = peer.post("/api/pair", json={"code": code, "device_name": "phone"}).json()["token"]
    return peer, {"Authorization": f"Bearer {token}"}


def test_lan_peer_pairs_and_can_view(app):
    peer, auth = _pair(app, "user")
    assert peer.get("/api/state", headers=auth).status_code == 200


def test_lan_peer_without_token_is_forbidden(app):
    peer = TestClient(app, client=("192.168.1.50", 12345))
    assert peer.get("/api/state").status_code == 403


def test_pair_code_returns_live_cert_fingerprint(tmp_path, monkeypatch):
    # audit blocking #1: the loopback pair-code response carries the SHA-256 fingerprint
    # of the LIVE serving cert, so the operator can verify it out-of-band against the
    # phone's certificate warning and detect a pairing-time TLS MitM.
    from wavr.tls import cert_fingerprint, ensure_cert

    cert = str(tmp_path / "cert.pem")
    key = str(tmp_path / "key.pem")
    ensure_cert(cert, key, "192.168.1.1")
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "fp.db"))
    monkeypatch.setenv("WAVR_TLS_CERT", cert)
    monkeypatch.setenv("WAVR_TLS_KEY", key)
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    app = create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))
    central = TestClient(app)
    body = central.post("/api/pair-code", json={"role": "user"}, headers=CSRF).json()
    assert body["cert_fingerprint"] == cert_fingerprint(cert)
    assert len(body["cert_fingerprint"].split(":")) == 32
    # P2 self-contained QR: the response also carries a LAN-reachable base (derived from the
    # SAME _local_ip the peers-admin self_base_url uses), never a hardcoded/real address --
    # here it's the monkeypatched _local_ipv4 fixture value, not a real home IP.
    assert body["lan_url"] == "https://192.168.1.1:8000"


def test_pair_code_response_includes_matching_verify6(tmp_path, monkeypatch):
    # Additive companion to the fingerprint assertion above: /api/pair-code also
    # carries `verify6`, the convenience-tier 6-digit derived from THIS response's
    # own cert_fingerprint + code (pinned derivation, wavr.tls.verification_code) --
    # never removes cert_fingerprint, which stays the strong out-of-band anchor.
    from wavr.tls import cert_fingerprint, ensure_cert, verification_code

    cert = str(tmp_path / "cert.pem")
    key = str(tmp_path / "key.pem")
    ensure_cert(cert, key, "192.168.1.1")
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "verify6.db"))
    monkeypatch.setenv("WAVR_TLS_CERT", cert)
    monkeypatch.setenv("WAVR_TLS_KEY", key)
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    app = create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"))
    central = TestClient(app)
    body = central.post("/api/pair-code", json={"role": "user"}, headers=CSRF).json()

    assert body["verify6"] == verification_code(cert_fingerprint(cert), body["code"])
    assert len(body["verify6"]) == 6 and body["verify6"].isdigit()
    # Response shape stays additive: the pre-existing keys are untouched.
    assert set(body.keys()) == {"code", "cert_fingerprint", "verify6", "lan_url"}


def test_user_role_cannot_change_state(app):
    peer, auth = _pair(app, "user")
    assert peer.post("/api/system/toggle", json={"on": True}, headers=auth).status_code == 403


def test_user_role_cannot_manage_devices(app):   # audit C1
    peer, auth = _pair(app, "user")
    assert peer.get("/api/devices", headers=auth).status_code == 403
    assert peer.delete("/api/devices/anything", headers=auth).status_code == 403


def test_central_role_can_change_state(app):
    peer, auth = _pair(app, "central")
    assert peer.post("/api/system/toggle", json={"on": False}, headers=auth).status_code == 200


def test_out_of_subnet_peer_is_forbidden(app):   # audit M2 / subnet
    outsider = TestClient(app, client=("10.0.0.5", 12345))
    assert outsider.get("/api/state").status_code == 403
    # /api/pair onboarding also requires in-subnet
    assert outsider.post("/api/pair", json={"code": "1", "device_name": "x"}).status_code == 403


def test_pwa_shell_loads_in_subnet_without_token(app):
    # A companion must be able to LOAD the page (+ install the PWA) before it has a token;
    # the static shell is exempt for in-subnet peers, but DATA still needs the token.
    peer = TestClient(app, client=("192.168.1.50", 12345))
    for path in ("/", "/index.html", "/manifest.webmanifest", "/sw.js", "/icon.svg"):
        assert peer.get(path).status_code == 200, path
    assert peer.get("/api/state").status_code == 403          # data still gated
    # out-of-subnet cannot even load the shell
    outsider = TestClient(app, client=("10.0.0.5", 12345))
    assert outsider.get("/").status_code == 403


def test_revoked_token_is_denied(app):
    peer, auth = _pair(app, "user")
    central = TestClient(app)
    devs = central.get("/api/devices", headers=CSRF).json()["devices"]
    central.delete(f"/api/devices/{devs[0]['device_id']}", headers=CSRF)
    assert peer.get("/api/state", headers=auth).status_code == 403


def test_loopback_delete_device_requires_csrf_header(app):   # audit fix: CSRF on device revoke
    peer, auth = _pair(app, "user")
    central = TestClient(app)
    device_id = central.get("/api/devices", headers=CSRF).json()["devices"][0]["device_id"]
    # loopback root WITHOUT the CSRF header is refused (same-origin browser drive-by
    # DELETE using just the operator's session, no header the page can't also send)
    assert central.delete(f"/api/devices/{device_id}").status_code == 403
    # loopback root WITH the header still works
    assert central.delete(f"/api/devices/{device_id}", headers=CSRF).status_code == 200
    assert peer.get("/api/state", headers=auth).status_code == 403   # actually revoked


def test_central_role_can_manage_devices_without_csrf_header(app):
    # A LAN 'central' companion is Bearer-token-authenticated, not cookie/session-based,
    # so it isn't subject to the loopback-root CSRF gate -- confirms the fix is scoped
    # to the loopback root and doesn't regress the authenticated central path.
    user_peer, user_auth = _pair(app, "user")
    central_peer, central_auth = _pair(app, "central")
    listed = central_peer.get("/api/devices", headers=central_auth)
    assert listed.status_code == 200
    target = next(d for d in listed.json()["devices"] if d["role"] == "user")
    assert central_peer.delete(f"/api/devices/{target['device_id']}", headers=central_auth).status_code == 200
    assert user_peer.get("/api/state", headers=user_auth).status_code == 403   # revoked


# --- /ws/live ticket path (audit M1 revoke re-check + M2 subnet) ---

def _ticket(peer, auth):
    return peer.post("/api/ws-ticket", headers=auth).json()["ticket"]


def test_ws_lan_peer_with_valid_ticket_connects(app):
    peer, auth = _pair(app, "user")
    with peer.websocket_connect(f"/ws/live?ticket={_ticket(peer, auth)}"):
        pass   # accepted (no 1008 close on handshake)


def test_ws_without_ticket_is_closed(app):
    peer, _ = _pair(app, "user")
    with pytest.raises(WebSocketDisconnect):
        with peer.websocket_connect("/ws/live"):
            pass


def test_ws_out_of_subnet_is_closed(app):   # audit M2
    outsider = TestClient(app, client=("10.0.0.5", 12345))
    with pytest.raises(WebSocketDisconnect):
        with outsider.websocket_connect("/ws/live?ticket=anything"):
            pass


def test_ws_ticket_is_single_use(app):
    peer, auth = _pair(app, "user")
    t = _ticket(peer, auth)
    with peer.websocket_connect(f"/ws/live?ticket={t}"):
        pass
    with pytest.raises(WebSocketDisconnect):     # reused ticket rejected
        with peer.websocket_connect(f"/ws/live?ticket={t}"):
            pass


# --- revoke-latency fix: _stream_live drops a revoked companion on a wall-clock cadence ---
# Driven on a REAL event loop via asyncio.run, NOT the TestClient: Starlette's WS test
# transport steps the app in lockstep with client I/O, so a free-running server timer
# (asyncio.wait_for) never advances there and the quiet-hub path can't be exercised. These
# unit tests hit _stream_live directly with a fake socket + a real asyncio.Queue.

class _FakeWS:
    def __init__(self): self.sent = 0
    async def send_json(self, item): self.sent += 1

class _Dev:
    def __init__(self, revoked): self.revoked = revoked


def test_stream_live_drops_on_quiet_hub_after_revoke():
    from wavr.app import _stream_live

    async def scenario():
        q = asyncio.Queue()                       # stays EMPTY -> a silent hub
        state = {"revoked": False}
        ws = _FakeWS()
        task = asyncio.create_task(
            _stream_live(ws, q, "dev1", lambda did: _Dev(state["revoked"]), 0.05))
        await asyncio.sleep(0.02)                 # loop parks on the empty queue
        state["revoked"] = True                   # revoke while zero frames flow
        await asyncio.wait_for(task, timeout=1.0) # must return on the wall-clock recheck
        return ws.sent

    # The old `n % 50` throttle parked on `await q.get()` forever here (0 frames) and would
    # hang; the wall-clock recheck severs the stream having sent nothing.
    assert asyncio.run(scenario()) == 0


def test_stream_live_loopback_root_ignores_revoke():
    from wavr.app import _stream_live

    async def scenario():
        q = asyncio.Queue()
        ws = _FakeWS()
        # did is None (loopback root): get_device would END the stream if ever called, so a
        # still-running task proves the revoke check is skipped for root, exactly as before.
        task = asyncio.create_task(_stream_live(ws, q, None, lambda did: None, 0.02))
        await asyncio.sleep(0.1)                  # several recheck intervals elapse
        alive = not task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return alive

    assert asyncio.run(scenario()) is True
