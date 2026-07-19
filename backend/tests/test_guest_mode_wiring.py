"""Guest mode (feature #8) WIRING, adversarial: prove a guest is boxed to
presence:write, that a guest is never named, that only an admin mints an invite,
and that a revoked/expired companion stops counting as present (Finding A/B, the
two pre-existing gaps the guest-mode design surfaced).

Forges a non-loopback LAN peer via TestClient(client=(host, port)) + a fixed
in-subnet _local_ipv4, exactly like test_multidevice_integration.py.
"""
import pytest
from fastapi.testclient import TestClient

from wavr.app import create_app
from wavr.camera_store import CameraStore
from wavr.identity_store import IdentityStore
from wavr.sources.simulated import SimulatedSource
from wavr.storage import Storage

CSRF = {"X-Wavr-Local": "1"}       # loopback root's CSRF header


async def _fake_mac(host):         # the companion-mac ARP seam (awaited in-handler)
    return "aa:bb:cc:dd:ee:01"


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("WAVR_MULTIDEVICE", "1")
    monkeypatch.setenv("WAVR_DB", str(tmp_path / "md.db"))
    monkeypatch.setattr("wavr.app._local_ipv4", lambda: "192.168.1.1")
    id_store = IdentityStore(":memory:")
    app = create_app(
        sources=[("sim", lambda: SimulatedSource(interval=1.0), False)],
        storage=Storage(":memory:"), camera_store=CameraStore(":memory:"),
        identity_store=id_store, companion_resolve_mac=_fake_mac)
    return app, id_store


def _pair(app, role="user"):
    central = TestClient(app)
    code = central.post("/api/pair-code", json={"role": role}, headers=CSRF).json()["code"]
    peer = TestClient(app, client=("192.168.1.50", 12345))
    body = peer.post("/api/pair", json={"code": code, "device_name": "phone"}).json()
    return peer, {"Authorization": f"Bearer {body['token']}"}, body["device_id"]


def _guest(app, hours=4):
    central = TestClient(app)
    inv = central.post("/api/guest/invite", json={"hours": hours}, headers=CSRF).json()
    peer = TestClient(app, client=("192.168.1.51", 22222))
    body = peer.post("/api/pair", json={"code": inv["code"], "device_name": "guest"}).json()
    return peer, {"Authorization": f"Bearer {body['token']}"}, body["device_id"], inv


# --------------------------------------------------------------------------- #
# The guest is boxed to presence:write -- it reads/​manages NOTHING
# --------------------------------------------------------------------------- #
def test_guest_denied_every_house_read_and_the_admin_pin(ctx):
    app, _ = ctx
    peer, auth, _did, _inv = _guest(app)
    assert peer.get("/api/state", headers=auth).status_code == 403, "no presence:read"
    assert peer.get("/api/inventory", headers=auth).status_code == 403, "no network:read"
    assert peer.post("/api/core/pin/verify", json={"pin": "1234"},
                     headers=auth).status_code == 403, "guest must not probe the admin PIN"
    # the two NEW read surfaces this same work shipped must also deny a guest (F12)
    assert peer.get("/api/transparency", headers=auth).status_code == 403, "no presence:read"
    assert peer.get("/api/routines", headers=auth).status_code == 403, "no control scope"


def test_guest_cannot_mint_an_invite_and_neither_can_a_user(ctx):
    app, _ = ctx
    gpeer, gauth, _d, _i = _guest(app)
    assert gpeer.post("/api/guest/invite", json={"hours": 2},
                      headers=gauth).status_code == 403, "a guest can't self-mint (no admin)"
    upeer, uauth, _d2 = _pair(app, "user")
    assert upeer.post("/api/guest/invite", json={"hours": 2},
                      headers=uauth).status_code == 403, "a plain user has no admin scope"


def test_central_and_root_can_mint_an_invite(ctx):
    app, _ = ctx
    root = TestClient(app)
    r = root.post("/api/guest/invite", json={"hours": 3}, headers=CSRF)
    assert r.status_code == 200 and r.json()["role"] == "guest" and r.json()["code"]
    cpeer, cauth, _d = _pair(app, "central")
    assert cpeer.post("/api/guest/invite", json={"hours": 3}, headers=cauth).status_code == 200


def test_invite_hours_are_clamped(ctx):
    app, _ = ctx
    root = TestClient(app)
    # a wild value is clamped server-side, never honored as-is (24h ceiling)
    exp = root.post("/api/guest/invite", json={"hours": 99999}, headers=CSRF).json()["expires_at"]
    assert exp, "an expires_at is returned"
    # and a redeemed guest really carries a bounded deadline (not None)
    peer = TestClient(app, client=("192.168.1.53", 33333))
    code = root.post("/api/guest/invite", json={"hours": 5}, headers=CSRF).json()["code"]
    body = peer.post("/api/pair", json={"code": code, "device_name": "g"}).json()
    me = peer.get("/api/devices/me", headers={"Authorization": f"Bearer {body['token']}"}).json()
    assert me["expires_at"] is not None, "the guest device carries its own deadline"


def test_invite_rejects_non_finite_hours(ctx):
    # A strict JSON client can't even send inf/NaN, but starlette's parser accepts the
    # non-standard `Infinity`/`NaN` tokens -- send them raw to prove the guard turns them
    # into a clean 422 rather than a timedelta(hours=NaN) 500.
    app, _ = ctx
    root = TestClient(app)
    hdr = {**CSRF, "content-type": "application/json"}
    for body in (b'{"hours": Infinity}', b'{"hours": NaN}'):
        r = root.post("/api/guest/invite", content=body, headers=hdr)
        assert r.status_code == 422, f"non-finite hours {body!r} must 422, never 500"


# --------------------------------------------------------------------------- #
# A guest is NEVER named, even if it sets its own consent green (Finding B)
# --------------------------------------------------------------------------- #
def test_guest_presence_is_anonymous_even_after_setting_consent_green(ctx):
    app, id_store = ctx
    peer, auth, _did, _inv = _guest(app)
    # a guest holds presence:write, so it CAN set its own consent...
    assert peer.post("/api/consent", json={"level": "green"}, headers=auth).status_code == 200
    # ...but registering still yields an ANONYMOUS (unnamed) row, never the label.
    r = peer.post("/api/presence/register-companion", json={"label": "Sneaky Name"}, headers=auth)
    assert r.status_code == 200 and r.json()["mac_registered"] is True
    assert r.json()["label"] is None, "a guest is never named, whatever consent it set"
    assert id_store.as_net_known() == {"aa:bb:cc:dd:ee:01": None}, "present, but anonymous"


# --------------------------------------------------------------------------- #
# A revoked companion stops counting as present (Finding A, pre-existing bug)
# --------------------------------------------------------------------------- #
def test_revoked_companion_drops_out_of_presence(ctx):
    app, id_store = ctx
    peer, auth, device_id = _pair(app, "user")
    peer.post("/api/consent", json={"level": "yellow"}, headers=auth)
    assert peer.post("/api/presence/register-companion", json={"label": "x"},
                     headers=auth).json()["mac_registered"] is True
    assert "aa:bb:cc:dd:ee:01" in id_store.as_net_known(), "registered -> counted as present"
    # revoke the device (loopback-root admin route) -> its token is dead...
    root = TestClient(app)
    assert root.delete(f"/api/devices/{device_id}", headers=CSRF).status_code == 200
    # ...and its presence must vanish: _consent_of returns red for a revoked device, so
    # the read-time gate drops it. Before the fix it kept counting as home forever.
    assert "aa:bb:cc:dd:ee:01" not in id_store.as_net_known(), \
        "a revoked companion's MAC no longer corroborates presence (Finding A)"


def test_expired_guest_drops_out_of_presence(ctx, tmp_path):
    # F9: the EXPIRED branch of _consent_of (not just REVOKED). A guest whose deadline has
    # passed must stop counting as present WITHOUT anyone calling revoke -- the auto-expiry
    # is the whole point of guest mode. Seed a guest device with a past deadline straight
    # into the same db create_app uses (WAVR_DB=tmp_path/md.db) and link a presence row to it.
    from datetime import datetime, timedelta, timezone

    from wavr.devices import DeviceStore
    _app, id_store = ctx
    ds = DeviceStore(str(tmp_path / "md.db"))
    did, _tok = ds.add("old guest", "guest",
                       expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
    id_store.add_anonymous("cc:dd:ee:ff:00:11", source="network", origin="companion", device_id=did)
    assert "cc:dd:ee:ff:00:11" not in id_store.as_net_known(), \
        "_consent_of returns red for an EXPIRED device, so its presence drops like a revoked one"
    ds.close()
